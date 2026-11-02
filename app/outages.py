"""Outage log (silence / stream drop-outs) and email alarms."""
import asyncio
import datetime as dt
import logging
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from . import db

log = logging.getLogger("radiologger.outages")

DEFAULTS = {
    "outage_threshold_seconds": "30",   # silence / no audio longer than this is an outage
    "silence_db": "-50",                # audio quieter than this counts as silence
    "alarm_enabled": "0",
    "alarm_recipients": "",
    "alarm_delay_seconds": "30",        # only email once an outage has lasted this long
    "alarm_on_recovery": "1",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_security": "starttls",        # starttls | ssl | none
    "smtp_username": "",
    "smtp_password": "",
    "smtp_from": "",
}
CAUSES = {"silence": "Silence", "no_audio": "Stream down"}
KEEP_DAYS = 365
EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")

STATE = {"last_sent_at": None, "last_error": "", "last_error_at": None}
_tasks: set[asyncio.Task] = set()


def init_db() -> None:
    with db._lock:
        c = db.conn()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS outages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_id INTEGER REFERENCES streams(id) ON DELETE SET NULL,
                stream_name TEXT NOT NULL,
                start_ts REAL NOT NULL,
                end_ts REAL,
                cause TEXT NOT NULL,
                alerted INTEGER NOT NULL DEFAULT 0,
                recovery_alerted INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_outages_start ON outages(start_ts);
            CREATE INDEX IF NOT EXISTS idx_outages_stream ON outages(stream_id, start_ts);
            """
        )
        for k, v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        c.commit()


def cfg() -> dict:
    s = db.get_settings()
    return {k: s.get(k, v) for k, v in DEFAULTS.items()}


def threshold() -> int:
    try:
        return max(10, int(cfg()["outage_threshold_seconds"]))
    except ValueError:
        return 30


def recipients(text: str) -> list[str]:
    return [a for a in re.split(r"[\s,;]+", text or "") if a]


# ---- the log ---------------------------------------------------------------------

def open_for(stream_id: int) -> dict | None:
    return db.q1("SELECT * FROM outages WHERE stream_id=? AND end_ts IS NULL ORDER BY start_ts DESC LIMIT 1",
                 (stream_id,))


def open_outage(stream: dict, cause: str, start: float) -> int:
    oid = db.ex("INSERT INTO outages(stream_id, stream_name, start_ts, cause) VALUES (?,?,?,?)",
                (stream["id"], stream["name"], start, cause))
    log.warning("[%s] outage started: %s since %s", stream["name"], CAUSES.get(cause, cause),
                time.strftime("%H:%M:%S", time.localtime(start)))
    return oid


def close_outage(oid: int, end: float) -> dict | None:
    db.ex("UPDATE outages SET end_ts=? WHERE id=? AND end_ts IS NULL", (end, oid))
    o = db.q1("SELECT * FROM outages WHERE id=?", (oid,))
    if o:
        log.warning("[%s] outage ended after %s", o["stream_name"], fmt_duration(end - o["start_ts"]))
    return o


def close_all_for(stream_id: int, end: float | None = None) -> None:
    """Used when a stream is disabled or deleted: we stop watching it, so close what's open."""
    db.ex("UPDATE outages SET end_ts=MAX(start_ts, ?) WHERE stream_id=? AND end_ts IS NULL",
          (end or time.time(), stream_id))


def prune() -> None:
    db.ex("DELETE FROM outages WHERE end_ts IS NOT NULL AND start_ts < ?", (time.time() - KEEP_DAYS * 86400,))


def listing(stream_id: int | None, start: float | None, end: float | None, limit: int = 1000) -> list[dict]:
    sql = "SELECT * FROM outages WHERE 1=1"
    args: list = []
    if stream_id:
        sql += " AND stream_id=?"; args.append(stream_id)
    if start is not None:  # anything overlapping the window, including outages still open
        sql += " AND COALESCE(end_ts, ?) >= ?"; args += [time.time(), start]
    if end is not None:
        sql += " AND start_ts < ?"; args.append(end)
    rows = db.q(sql + " ORDER BY start_ts DESC LIMIT ?", (*args, limit))
    now = time.time()
    for r in rows:
        r["ongoing"] = r["end_ts"] is None
        r["duration"] = (r["end_ts"] or now) - r["start_ts"]
        r["cause_label"] = CAUSES.get(r["cause"], r["cause"])
        # replay link: start a few seconds before the audio dropped
        at = dt.datetime.fromtimestamp(max(0, r["start_ts"] - 5))
        r["replay_date"], r["replay_time"] = at.strftime("%Y-%m-%d"), at.strftime("%H:%M:%S")
    return rows


def downtime(stream_id: int, start: float, end: float) -> float:
    """Seconds of outage for a stream that overlap [start, end)."""
    now = time.time()
    total = 0.0
    for r in db.q("SELECT start_ts, end_ts FROM outages WHERE stream_id=? AND start_ts < ? "
                  "AND COALESCE(end_ts, ?) > ?", (stream_id, end, now, start)):
        total += max(0.0, min(r["end_ts"] or now, end) - max(r["start_ts"], start))
    return total


def fmt_duration(sec: float) -> str:
    sec = int(round(sec))
    if sec < 60:
        return f"{sec} s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


# ---- email --------------------------------------------------------------------------

def send_email(c: dict, subject: str, body: str, to: list[str] | None = None) -> None:
    """Blocking SMTP send. Raises on failure."""
    to = to or recipients(c["alarm_recipients"])
    if not c["smtp_host"].strip():
        raise ValueError("Enter an SMTP server first")
    if not to:
        raise ValueError("Add at least one recipient")
    bad = [a for a in to if not EMAIL_RE.match(a)]
    if bad:
        raise ValueError(f"Not an email address: {', '.join(bad)}")
    sender = c["smtp_from"].strip() or c["smtp_username"].strip()
    if not EMAIL_RE.match(parseaddr(sender)[1]):
        raise ValueError("Set a valid From address (e.g. Radio Logger <alerts@example.com>)")

    msg = EmailMessage()
    msg["Subject"] = " ".join(subject.split())[:200]   # no CR/LF in headers
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="radio-logger")
    msg.set_content(body)

    host, port, sec = c["smtp_host"].strip(), int(c["smtp_port"] or 0), c["smtp_security"]
    ctx = ssl.create_default_context()
    if sec == "ssl":
        server = smtplib.SMTP_SSL(host, port or 465, timeout=20, context=ctx)
    else:
        server = smtplib.SMTP(host, port or 587, timeout=20)
    with server:
        server.ehlo()
        if sec == "starttls":
            server.starttls(context=ctx)
            server.ehlo()
        if c["smtp_username"].strip():
            server.login(c["smtp_username"].strip(), c["smtp_password"])
        server.send_message(msg)


def _public_url() -> str:
    return db.get_settings().get("sso_public_url", "").rstrip("/")


def _links(o: dict) -> str:
    base = _public_url()
    if not base:
        return ""
    at = dt.datetime.fromtimestamp(max(0, o["start_ts"] - 5))
    replay = f"{base}/?stream={o['stream_id']}&date={at:%Y-%m-%d}&time={at:%H:%M:%S}"
    return f"\nReplay the moment it dropped: {replay}\nOutage log: {base}/outages\n"


def _when(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%a %d %b %Y, %H:%M:%S")


def _spawn_send(subject: str, body: str, on_ok) -> None:
    c = cfg()

    async def run():
        try:
            await asyncio.to_thread(send_email, c, subject, body)
            STATE["last_sent_at"] = time.time()
            on_ok()
            log.info("alarm email sent: %s", subject)
        except Exception as e:
            STATE["last_error"], STATE["last_error_at"] = f"{type(e).__name__}: {e}", time.time()
            log.warning("alarm email failed: %s", e)

    t = asyncio.create_task(run())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def alarms_on() -> bool:
    c = cfg()
    return c["alarm_enabled"] == "1" and bool(c["smtp_host"].strip()) and bool(recipients(c["alarm_recipients"]))


def alarm_delay() -> int:
    try:
        return max(threshold(), int(cfg()["alarm_delay_seconds"]))
    except ValueError:
        return threshold()


def maybe_alert_start(oid: int) -> None:
    """Called by the recorder while an outage is open; emails once it has lasted the alarm delay."""
    o = db.q1("SELECT * FROM outages WHERE id=?", (oid,))
    if not o or o["alerted"] or not alarms_on() or time.time() - o["start_ts"] < alarm_delay():
        return
    db.ex("UPDATE outages SET alerted=1 WHERE id=?", (oid,))  # claim it first so we never double-send
    label = CAUSES.get(o["cause"], o["cause"]).upper()
    lasted = fmt_duration(time.time() - o["start_ts"])
    subject = f"[Radio Logger] {label}: {o['stream_name']} - off air for {lasted}"
    what = ("the stream is delivering silence" if o["cause"] == "silence"
            else "no audio is arriving from the stream (it has dropped or stalled)")
    body = (f"{o['stream_name']} has an outage: {what}.\n\n"
            f"Started:  {_when(o['start_ts'])}\n"
            f"Lasted so far: {lasted}\n"
            f"{_links(o)}")
    if cfg()["alarm_on_recovery"] == "1":
        body += "\nYou'll get another email when it recovers.\n"
    _spawn_send(subject, body, lambda: None)


def alert_end(o: dict) -> None:
    if not o or o["alerted"] != 1 or o["recovery_alerted"] or not alarms_on() or cfg()["alarm_on_recovery"] != "1":
        return
    db.ex("UPDATE outages SET recovery_alerted=1 WHERE id=?", (o["id"],))
    lasted = fmt_duration(o["end_ts"] - o["start_ts"])
    subject = f"[Radio Logger] RECOVERED: {o['stream_name']} - back after {lasted}"
    body = (f"{o['stream_name']} is back on air.\n\n"
            f"Cause:    {CAUSES.get(o['cause'], o['cause'])}\n"
            f"Started:  {_when(o['start_ts'])}\n"
            f"Ended:    {_when(o['end_ts'])}\n"
            f"Duration: {lasted}\n{_links(o)}")
    _spawn_send(subject, body, lambda: None)
