"""SQLite storage for users, streams, settings and the track log."""
import os
import re
import sqlite3
import threading
import time

CONFIG_DIR = os.environ.get("RL_CONFIG_DIR", "/config")
DB_PATH = os.path.join(CONFIG_DIR, "radiologger.db")

DEFAULT_SETTINGS = {
    "recordings_path": os.environ.get("RL_RECORDINGS_DIR", "/recordings"),
    "retention_days": "30",
    "chunk_minutes": "15",
    "bitrate_kbps": "128",
    "shazam_interval": "60",
}

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init() -> None:
    with _lock:
        c = conn()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS streams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                is_music INTEGER NOT NULL DEFAULT 0,
                meta_source TEXT NOT NULL DEFAULT 'icy',
                logo_file TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
                ts REAL NOT NULL,
                artist TEXT,
                title TEXT,
                album TEXT,
                artwork_url TEXT,
                raw TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tracks_stream_ts ON tracks(stream_id, ts);
            """
        )
        # migrations for databases created by earlier versions
        cols = {r[1] for r in c.execute("PRAGMA table_info(tracks)")}
        for col, ddl in (("tags", "TEXT"), ("duration", "REAL"), ("source", "TEXT")):
            if col not in cols:
                c.execute(f"ALTER TABLE tracks ADD COLUMN {col} {ddl}")
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        for col, ddl in (("auth", "TEXT NOT NULL DEFAULT 'local'"), ("sso_sub", "TEXT"), ("display_name", "TEXT"),
                         ("email", "TEXT"), ("last_login", "INTEGER")):
            if col not in cols:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_sso ON users(sso_sub) WHERE sso_sub IS NOT NULL")
        cols = {r[1] for r in c.execute("PRAGMA table_info(streams)")}
        for col in ("icy_name", "icy_genre", "icy_description", "icy_url"):
            if col not in cols:
                c.execute(f"ALTER TABLE streams ADD COLUMN {col} TEXT")
        if "transcribe" not in cols:
            c.execute("ALTER TABLE streams ADD COLUMN transcribe INTEGER NOT NULL DEFAULT 1")
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        c.commit()


def q(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql: str, args: tuple = ()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql: str, args: tuple = ()) -> int:
    with _lock:
        c = conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


# ---- settings -------------------------------------------------------------

def get_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in q("SELECT key, value FROM settings")})
    return s


def setting_int(key: str) -> int:
    return int(get_settings()[key])


def set_setting(key: str, value) -> None:
    ex("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, str(value)))


# ---- streams ----------------------------------------------------------------

def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "stream"
    slug, n = base, 2
    while q1("SELECT 1 FROM streams WHERE slug=?", (slug,)):
        slug, n = f"{base}-{n}", n + 1
    return slug


def streams(enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM streams" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY name"
    return q(sql)


def stream(stream_id: int) -> dict | None:
    return q1("SELECT * FROM streams WHERE id=?", (stream_id,))


# ---- tracks -----------------------------------------------------------------

def last_track(stream_id: int) -> dict | None:
    return q1("SELECT * FROM tracks WHERE stream_id=? ORDER BY ts DESC LIMIT 1", (stream_id,))


def add_track(stream_id: int, artist: str, title: str, album: str | None,
              artwork_url: str | None, raw: str, ts: float | None = None,
              tags: list[str] | None = None, duration: float | None = None, source: str = "icy") -> int:
    return ex("INSERT INTO tracks(stream_id, ts, artist, title, album, artwork_url, raw, tags, duration, source) "
              "VALUES (?,?,?,?,?,?,?,?,?,?)",
              (stream_id, ts or time.time(), artist, title, album, artwork_url, raw,
               join_tags(tags), duration, source))


def join_tags(tags: list[str] | None) -> str:
    seen, out = set(), []
    for t in tags or []:
        t = str(t).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t[:40])
    return ",".join(out[:12])


def track_out(r: dict) -> dict:
    r = dict(r)
    r["tags"] = [t for t in (r.get("tags") or "").split(",") if t]
    return r


def tracks_between(stream_id: int, start: float, end: float) -> list[dict]:
    return [track_out(r) for r in q(
        "SELECT id, ts, artist, title, album, artwork_url, tags, duration, source FROM tracks "
        "WHERE stream_id=? AND ts>=? AND ts<? ORDER BY ts", (stream_id, start, end))]


def top_tracks(stream_id: int, start: float, end: float, limit: int = 10) -> dict:
    """Most-played songs in [start, end), grouped case-insensitively by artist + title.

    Grouping is done in Python because SQLite's LOWER() only folds ASCII (BEYONCÉ != Beyoncé).
    """
    rows = q("SELECT ts, artist, title, artwork_url FROM tracks "
             "WHERE stream_id=? AND ts>=? AND ts<? AND COALESCE(title,'')<>'' ORDER BY ts", (stream_id, start, end))
    songs: dict[tuple, dict] = {}
    for r in rows:
        key = ((r["artist"] or "").strip().casefold(), r["title"].strip().casefold())
        s = songs.get(key)
        if not s:
            s = songs[key] = {"artist": r["artist"], "title": r["title"], "plays": 0,
                              "first_ts": r["ts"], "last_ts": r["ts"], "artwork_url": None, "_names": {}}
        s["plays"] += 1
        name = (r["artist"], r["title"])
        s["_names"][name] = s["_names"].get(name, 0) + 1
        s["last_ts"] = r["ts"]
        if r["artwork_url"]:
            s["artwork_url"] = r["artwork_url"]  # rows are in time order, so this keeps the latest artwork
    top = sorted(songs.values(), key=lambda s: (-s["plays"], -s["last_ts"]))[:limit]
    for s in top:  # show the spelling used most often
        s["artist"], s["title"] = max(s.pop("_names").items(), key=lambda kv: kv[1])[0]
    return {"start": start, "end": end, "plays": len(rows), "songs": len(songs), "top": top}


def tracks_for_export(stream_id: int, start: float | None = None, end: float | None = None) -> list[dict]:
    sql = "SELECT ts, artist, title, album, tags, duration, source FROM tracks WHERE stream_id=?"
    args: list = [stream_id]
    if start is not None:
        sql += " AND ts>=?"; args.append(start)
    if end is not None:
        sql += " AND ts<?"; args.append(end)
    return q(sql + " ORDER BY ts", tuple(args))
