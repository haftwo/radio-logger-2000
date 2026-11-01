"""Transcripts for talk stations using a self-hosted Whisper server.

Supported server APIs (pick in Admin > Transcription):
  openai   - OpenAI-compatible /v1/audio/transcriptions (Speaches, faster-whisper-server, LocalAI ...)
  asr      - whisper-asr-webservice /asr (onerahmet/openai-whisper-asr-webservice)
  wyoming  - Wyoming protocol over TCP (linuxserver/faster-whisper, rhasspy/wyoming-whisper)

A single background worker transcribes finished chunks, newest first, then works back
through the backlog. Segments are stored with absolute timestamps and indexed for search.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import time
from collections import Counter

import httpx

from . import audio, db

log = logging.getLogger("radiologger.transcribe")

DEFAULTS = {
    "whisper_enabled": "0",
    "whisper_api": "openai",
    "whisper_url": "",
    "whisper_model": "Systran/faster-whisper-small",
    "whisper_language": "en",
    "whisper_api_key": "",
    "whisper_backfill_hours": "24",
    "whisper_keywords": "",          # always-highlight watch list, comma separated
    "whisper_timeout": "900",
}
WYOMING_WINDOW = 15  # seconds per request when the server returns no timestamps

STATE = {"busy": None, "last_error": "", "last_error_at": None, "done": 0, "last_ok_at": None,
         "queue": 0, "last_seconds": None}


def cfg() -> dict:
    s = db.get_settings()
    return {k: s.get(k, v) for k, v in DEFAULTS.items()}


def enabled() -> bool:
    c = cfg()
    return c["whisper_enabled"] == "1" and bool(c["whisper_url"].strip())


# ---------------------------------------------------------------- storage

def init_db() -> None:
    with db._lock:
        c = db.conn()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS transcript_chunks (
                stream_id INTEGER NOT NULL,
                chunk_start REAL NOT NULL,
                file TEXT NOT NULL,
                status TEXT NOT NULL,          -- done | error | empty
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER,
                PRIMARY KEY (stream_id, chunk_start)
            );
            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_id INTEGER NOT NULL,
                ts REAL NOT NULL,
                end_ts REAL NOT NULL,
                text TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_segments_stream_ts ON segments(stream_id, ts);
            """
        )
        try:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5("
                      "text, content='segments', content_rowid='id', tokenize='porter unicode61')")
            c.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
                  INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text); END;
                CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
                  INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text); END;
                """
            )
        except Exception as e:  # sqlite built without FTS5: search falls back to LIKE
            log.warning("FTS5 unavailable, using simple search: %s", e)
        c.commit()
        for k, v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        c.commit()


def has_fts() -> bool:
    return bool(db.q1("SELECT 1 FROM sqlite_master WHERE name='segments_fts'"))


def save_segments(stream_id: int, chunk_start: float, file: str, segs: list[dict], chunk_len: float) -> None:
    """Replace any earlier transcript of this chunk with the new segments."""
    with db._lock:
        c = db.conn()
        c.execute("DELETE FROM segments WHERE stream_id=? AND ts>=? AND ts<?",
                  (stream_id, chunk_start, chunk_start + chunk_len))
        for s in segs:
            if chunk_len and s["start"] >= chunk_len:  # never spill into the next chunk's time
                continue
            c.execute("INSERT INTO segments(stream_id, ts, end_ts, text) VALUES (?,?,?,?)",
                      (stream_id, chunk_start + s["start"], chunk_start + s["end"], s["text"]))
        c.execute("INSERT INTO transcript_chunks(stream_id, chunk_start, file, status, attempts, updated_at) "
                  "VALUES (?,?,?,?,1,?) ON CONFLICT(stream_id, chunk_start) DO UPDATE SET "
                  "status=excluded.status, error=NULL, updated_at=excluded.updated_at",
                  (stream_id, chunk_start, file, "done" if segs else "empty", int(time.time())))
        c.commit()


def _span(segs: list[dict]) -> float:
    return max((s["end"] for s in segs), default=0)


def mark_error(stream_id: int, chunk_start: float, file: str, err: str) -> None:
    db.ex("INSERT INTO transcript_chunks(stream_id, chunk_start, file, status, error, attempts, updated_at) "
          "VALUES (?,?,?,'error',?,1,?) ON CONFLICT(stream_id, chunk_start) DO UPDATE SET status='error', "
          "error=excluded.error, attempts=attempts+1, updated_at=excluded.updated_at",
          (stream_id, chunk_start, file, err[:500], int(time.time())))


def purge_before(cutoff: float) -> None:
    db.ex("DELETE FROM segments WHERE ts < ?", (cutoff,))
    db.ex("DELETE FROM transcript_chunks WHERE chunk_start < ?", (cutoff,))


# ---------------------------------------------------------------- whisper clients

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


async def transcribe_file(path: str, c: dict | None = None) -> list[dict]:
    """Return [{start, end, text}] relative to the start of the file."""
    c = c or cfg()
    api = c["whisper_api"]
    if api == "wyoming":
        return await _wyoming(path, c)
    url = c["whisper_url"].rstrip("/")
    timeout = httpx.Timeout(float(c["whisper_timeout"] or 900), connect=10)
    headers = {"Authorization": f"Bearer {c['whisper_api_key']}"} if c["whisper_api_key"] else {}
    with open(path, "rb") as f:
        audio_bytes = f.read()
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as cl:
        if api == "asr":
            params = {"task": "transcribe", "output": "json", "encode": "true", "vad_filter": "true"}
            if c["whisper_language"]:
                params["language"] = c["whisper_language"]
            r = await cl.post(url + "/asr", params=params,
                              files={"audio_file": (os.path.basename(path), audio_bytes, "audio/mpeg")})
        else:
            if not url.endswith("/v1"):
                url += "/v1"
            data = {"model": c["whisper_model"], "response_format": "verbose_json",
                    "timestamp_granularities[]": "segment"}
            if c["whisper_language"]:
                data["language"] = c["whisper_language"]
            r = await cl.post(url + "/audio/transcriptions", data=data,
                              files={"file": (os.path.basename(path), audio_bytes, "audio/mpeg")})
    if r.status_code >= 400:
        raise RuntimeError(f"Whisper server returned {r.status_code}: {r.text[:200]}")
    try:
        body = r.json()
    except ValueError:
        return [{"start": 0.0, "end": audio.duration(path), "text": _clean(r.text)}] if r.text.strip() else []
    segs = body.get("segments") or []
    out = []
    for s in segs:
        text = _clean(s.get("text"))
        if text and not _is_hallucination(text):
            out.append({"start": float(s.get("start", 0)), "end": float(s.get("end", 0)), "text": text})
    if not out and _clean(body.get("text")):
        out = [{"start": 0.0, "end": audio.duration(path), "text": _clean(body["text"])}]
    return out


_HALLUCINATIONS = re.compile(r"^(thanks? (you )?for watching|subtitles? by|\[?music\]?|♪+|you)[.!]*$", re.I)


def _is_hallucination(text: str) -> bool:
    """Whisper invents these during music/silence."""
    return bool(_HALLUCINATIONS.match(text.strip()))


async def _wyoming(path: str, c: dict) -> list[dict]:
    m = re.match(r"^(?:tcp://)?([^:/]+):(\d+)", c["whisper_url"].strip())
    if not m:
        raise RuntimeError("Wyoming address must look like tcp://192.168.1.10:10300")
    host, port = m.group(1), int(m.group(2))
    rate = 16000
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(rate), "-f", "s16le", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    pcm, _ = await proc.communicate()
    win = rate * 2 * WYOMING_WINDOW
    out = []
    for i in range(0, len(pcm), win):
        piece = pcm[i:i + win]
        if len(piece) < rate * 2:  # under a second
            continue
        text = await _wyoming_one(host, port, piece, rate, c["whisper_language"],
                                  float(c["whisper_timeout"] or 900))
        text = _clean(text)
        if text and not _is_hallucination(text):
            start = i / (rate * 2)
            out.append({"start": start, "end": start + len(piece) / (rate * 2), "text": text})
    return out


def _event(etype: str, data: dict | None = None, payload: bytes = b"") -> bytes:
    data_b = json.dumps(data or {}).encode()
    header = {"type": etype, "version": "1.5.0", "data_length": len(data_b), "payload_length": len(payload)}
    return json.dumps(header).encode() + b"\n" + data_b + payload


async def _wyoming_one(host, port, pcm: bytes, rate: int, language: str, timeout: float) -> str:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 10)
    try:
        fmt = {"rate": rate, "width": 2, "channels": 1}
        writer.write(_event("transcribe", {"language": language} if language else {}))
        writer.write(_event("audio-start", fmt))
        for j in range(0, len(pcm), 32000):
            writer.write(_event("audio-chunk", fmt, pcm[j:j + 32000]))
        writer.write(_event("audio-stop", {}))
        await writer.drain()
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = await asyncio.wait_for(reader.readline(), max(1, deadline - time.time()))
            if not line:
                break
            head = json.loads(line)
            data = head.get("data") or {}
            if head.get("data_length"):
                data.update(json.loads(await reader.readexactly(head["data_length"])))
            if head.get("payload_length"):
                await reader.readexactly(head["payload_length"])
            if head.get("type") == "transcript":
                return data.get("text", "")
        raise RuntimeError("Wyoming server didn't return a transcript")
    finally:
        writer.close()


async def test_connection(c: dict) -> dict:
    """Transcribe a 3-second test clip so Admin can confirm the server is reachable."""
    tmp = os.path.join(db.CONFIG_DIR, "whisper-test.mp3")
    p = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=300:duration=3",
        "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", tmp,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await p.wait()
    t0 = time.time()
    try:
        segs = await transcribe_file(tmp, c)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return {"ok": True, "seconds": round(time.time() - t0, 2),
            "text": " ".join(s["text"] for s in segs) or "(no speech in the test tone, as expected)"}


# ---------------------------------------------------------------- worker

def _pending(streams: list[dict]) -> list[tuple[dict, str, float, str]]:
    """Finished chunks without a transcript, newest first, within the backfill window."""
    c = cfg()
    horizon = time.time() - float(c["whisper_backfill_hours"] or 24) * 3600
    out = []
    today = dt.date.today()
    days = [today - dt.timedelta(days=i) for i in range(int(float(c["whisper_backfill_hours"] or 24) // 24) + 2)]
    for s in streams:
        done = {r["chunk_start"]: r for r in db.q(
            "SELECT chunk_start, status, attempts, updated_at FROM transcript_chunks WHERE stream_id=? AND chunk_start>=?",
            (s["id"], horizon))}
        for day in days:
            d = audio.day_dir(s["slug"], day)
            if not os.path.isdir(d):
                continue
            for n in os.listdir(d):
                start = audio.file_start(n)
                if start is None or start < horizon:
                    continue
                p = os.path.join(d, n)
                try:
                    if time.time() - os.path.getmtime(p) < 30:  # still being recorded
                        continue
                except OSError:
                    continue
                r = done.get(start)
                if r and (r["status"] != "error" or r["attempts"] >= 3
                          or time.time() - (r["updated_at"] or 0) < 600):
                    continue
                out.append((s, p, start, f"{day.isoformat()}/{n}"))
    out.sort(key=lambda x: -x[2])
    return out


async def worker() -> None:
    init_db()
    while True:
        try:
            if not enabled():
                STATE.update(busy=None, queue=0)
                await asyncio.sleep(20)
                continue
            streams = [s for s in db.streams(enabled_only=False) if not s["is_music"] and s.get("transcribe", 1)]
            todo = await asyncio.to_thread(_pending, streams)
            STATE["queue"] = len(todo)
            if not todo:
                STATE["busy"] = None
                await asyncio.sleep(30)
                continue
            s, path, start, label = todo[0]
            STATE["busy"] = f"{s['name']} · {label.split('/')[1][:15]}"
            t0 = time.time()
            try:
                segs = await transcribe_file(path)
                save_segments(s["id"], start, label, segs, await asyncio.to_thread(audio.duration, path))
                STATE.update(done=STATE["done"] + 1, last_ok_at=time.time(), last_seconds=round(time.time() - t0, 1))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                mark_error(s["id"], start, label, msg)
                STATE.update(last_error=msg, last_error_at=time.time())
                log.warning("transcription failed for %s: %s", label, msg)
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("transcription worker error")
            await asyncio.sleep(30)


# ---------------------------------------------------------------- reading, search, keywords

def day_segments(stream_id: int, start: float, end: float) -> list[dict]:
    return db.q("SELECT id, ts, end_ts, text FROM segments WHERE stream_id=? AND ts>=? AND ts<? ORDER BY ts",
                (stream_id, start, end))


def day_status(stream_id: int, start: float, end: float) -> dict:
    rows = db.q("SELECT status, COUNT(*) n FROM transcript_chunks WHERE stream_id=? AND chunk_start>=? "
                "AND chunk_start<? GROUP BY status", (stream_id, start, end))
    return {r["status"]: r["n"] for r in rows}


def search(stream_id: int, q: str, start: float | None = None, end: float | None = None,
           limit: int = 200) -> list[dict]:
    q = q.strip()
    if not q:
        return []
    rng, args = "", []
    if start is not None:
        rng, args = " AND s.ts>=? AND s.ts<?", [start, end]
    if has_fts():
        # quote each term so punctuation can't break the FTS query; "a b" phrases stay phrases
        terms = re.findall(r'"[^"]+"|\S+', q)
        fts_q = " ".join('"' + t.strip('"').replace('"', "") + '"' + ("" if t.startswith('"') else "*")
                         for t in terms if t.strip('"'))
        try:
            return db.q("SELECT s.id, s.ts, s.end_ts, s.text FROM segments_fts f JOIN segments s ON s.id=f.rowid "
                        f"WHERE segments_fts MATCH ? AND s.stream_id=?{rng} ORDER BY s.ts DESC LIMIT ?",
                        (fts_q, stream_id, *args, limit))
        except Exception as e:
            log.debug("fts query failed (%s), falling back", e)
    return db.q(f"SELECT s.id, s.ts, s.end_ts, s.text FROM segments s WHERE s.stream_id=? AND s.text LIKE ?{rng} "
                "ORDER BY s.ts DESC LIMIT ?", (stream_id, f"%{q}%", *args, limit))


STOP = set("""a about above after again against all also am an and any are aren't as at be because been before being
below between both but by can can't cannot could couldn't did didn't do does doesn't doing don't down during each
few for from further get gets getting got had hadn't has hasn't have haven't having he he'd he'll he's her here
here's hers herself him himself his how how's i i'd i'll i'm i've if in into is isn't it it's its itself just know
let's like look lot lots make many may maybe me mean more most much must mustn't my myself need no nor not now of
off oh ok okay on once one only or other ought our ours ourselves out over own pretty probably quite really right
said same say says see she she'd she'll she's should shouldn't so some something still such sure take tell than
thank thanks that that's the their theirs them themselves then there there's these they they'd they'll they're
they've thing things think this those though through time to today too um uh under until up us very want was
wasn't way we we'd we'll we're we've well were weren't what what's when when's where where's whether which while
who who's whom why why's will with won't would wouldn't yeah yes yet you you'd you'll you're you've your yours
yourself yourselves going gonna actually around back come came course across place remain remains visited visit along
across almost already always another anything because before behind being better called certain different
during either enough everyone everything getting given giving happen happened having important inside keep
later least leave left less might morning never often others outside people perhaps quite rather really saying
seems seen several should since someone sometimes started still taken taking these though thought three
through together toward under until using usually whatever where while whole within without would even every first good great got here's
kind last little long new next part put show talk talking talked thing went year years go got doing done made
week day days morning afternoon evening tonight minute minutes hour hours point bit sort per cent percent""".split())


def keywords(segments: list[dict], watch: list[str], top: int = 24, exclude: list[str] | None = None) -> dict:
    """Top terms for a set of segments: repeated proper-noun phrases and frequent content words.
    Watch-list words and the station's own name are left out of the automatic list."""
    phrases, words = Counter(), Counter()
    skip = {x.lower() for x in watch}
    for name in exclude or []:
        if name:
            skip.add(name.lower())
            skip.update(w.lower() for w in name.split())
    for s in segments:
        text = s["text"]
        # capitalised runs not at sentence start, e.g. "Prime Minister Anthony Albanese", "Melbourne"
        for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z'’]+(?:\s+[A-Z][a-z'’]+){0,3})\b", text):
            ph = m.group(1).strip()
            if ph.lower() not in STOP and ph.lower() not in skip and len(ph) > 2:
                phrases[ph] += 1
        for w in re.findall(r"[A-Za-z][A-Za-z'’-]{3,}", text):
            lw = w.lower().strip("'’-")
            if len(lw) >= 5 and lw not in STOP and lw not in skip and not lw.endswith(("'s", "’s")):
                words[lw] += 1
    picked, seen = [], set()
    for ph, n in phrases.most_common():
        if n < 2 or len(picked) >= top // 2:
            break
        picked.append({"term": ph, "count": n, "kind": "name"})
        seen.update(x.lower() for x in ph.split())
    for w, n in words.most_common():
        if len(picked) >= top or n < 3:
            break
        if w not in seen:
            picked.append({"term": w, "count": n, "kind": "topic"})
            seen.add(w)
    hits = []
    for term in watch:
        n = sum(len(re.findall(r"\b" + re.escape(term) + r"\b", s["text"], re.I)) for s in segments)
        if n:
            hits.append({"term": term, "count": n, "kind": "watch"})
    return {"watch": hits, "auto": picked}
