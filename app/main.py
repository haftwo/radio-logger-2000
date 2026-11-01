"""Radio Logger - 2000: web app entry point."""
import asyncio
import datetime as dt
import io
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from urllib.parse import urlparse

from . import audio, auth, db, metadata
from .recorder import check_writable, manager

logging.basicConfig(level=os.environ.get("RL_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
HERE = os.path.dirname(__file__)
STATIC = os.path.join(HERE, "static")
LOGO_DIR = os.path.join(db.CONFIG_DIR, "logos")
APP_NAME = "Radio Logger - 2000"


@asynccontextmanager
async def lifespan(_app):
    db.init()
    auth.ensure_admin()
    os.makedirs(LOGO_DIR, exist_ok=True)
    await manager.start()
    yield
    await manager.stop()


app = FastAPI(title=APP_NAME, lifespan=lifespan, docs_url=None, redoc_url=None)
db.init()
app.add_middleware(SessionMiddleware, secret_key=auth.secret_key(), session_cookie="rl2000",
                   max_age=60 * 60 * 24 * 30, same_site="lax")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# Cache-busting: stamp script/style URLs with a version that changes whenever the static files do,
# so browsers never mix a new page with an old cached dashboard.js after an update.
ASSET_VERSION = str(int(max(os.path.getmtime(os.path.join(STATIC, f)) for f in os.listdir(STATIC))))


def page(name: str) -> HTMLResponse:
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        html = f.read()
    for asset in ("app.css", "dashboard.js", "admin.js"):
        html = html.replace(f"/static/{asset}\"", f"/static/{asset}?v={ASSET_VERSION}\"")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


# ---- pages ----------------------------------------------------------------------

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(os.path.join(STATIC, "favicon.ico"))


@app.get("/")
def index(request: Request):
    if not auth.current_user(request):
        return RedirectResponse("/login", 303)
    return page("index.html")


@app.get("/admin")
def admin_page(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    if user["role"] != "admin":
        return RedirectResponse("/", 303)
    return page("admin.html")


@app.get("/login")
def login_page():
    return page("login.html")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    u = db.q1("SELECT * FROM users WHERE username=?", (username.strip(),))
    if not u or not auth.verify_password(password, u["password_hash"]):
        time.sleep(0.5)
        return RedirectResponse("/login?error=1", 303)
    request.session.clear()
    request.session["uid"] = u["id"]
    return RedirectResponse("/", 303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---- viewer API ---------------------------------------------------------------

def stream_out(s: dict) -> dict:
    return {
        "id": s["id"], "name": s["name"], "slug": s["slug"], "enabled": bool(s["enabled"]),
        "is_music": bool(s["is_music"]), "meta_source": s["meta_source"],
        "logo": f"/logos/{s['logo_file']}" if s["logo_file"] else None,
        "status": manager.status(s["id"])["state"] if s["enabled"] else "disabled",
        "station": {"name": s.get("icy_name"), "genre": s.get("icy_genre"),
                    "description": s.get("icy_description"), "url": s.get("icy_url")},
    }


def get_stream_or_404(stream_id: int) -> dict:
    s = db.stream(stream_id)
    if not s:
        raise HTTPException(404, "Stream not found")
    return s


@app.get("/api/me")
def me(user=Depends(auth.require_user)):
    return {**user, "app": APP_NAME, "tz": time.tzname[0], "tz_name": os.environ.get("TZ", "UTC")}


@app.get("/api/streams")
def list_streams(user=Depends(auth.require_user)):
    return [stream_out(s) for s in db.streams()]


@app.get("/api/streams/{stream_id}/days")
def stream_days(stream_id: int, user=Depends(auth.require_user)):
    s = get_stream_or_404(stream_id)
    return audio.available_days(s["slug"])


@app.get("/api/streams/{stream_id}/day")
def stream_day(stream_id: int, date: str, user=Depends(auth.require_user)):
    s = get_stream_or_404(stream_id)
    try:
        day = audio.parse_date(date)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    start = dt.datetime.combine(day, dt.time()).timestamp()
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time()).timestamp()
    return {
        "date": date, "day_start": start, "day_end": end, "now": time.time(),
        "chunks": audio.list_day(s["slug"], day),
        "tracks": db.tracks_between(stream_id, start, end),
    }


@app.get("/api/streams/{stream_id}/nowplaying")
def now_playing(stream_id: int, user=Depends(auth.require_user)):
    get_stream_or_404(stream_id)
    t = db.last_track(stream_id)
    return db.track_out(t) if t else {}


@app.get("/api/streams/{stream_id}/audio/{date}/{name}")
def chunk_audio(stream_id: int, date: str, name: str, download: int = 0, user=Depends(auth.require_user)):
    s = get_stream_or_404(stream_id)
    try:
        p = audio.chunk_path(s["slug"], date, name)
    except ValueError:
        raise HTTPException(400, "bad path")
    if not os.path.isfile(p):
        raise HTTPException(404, "Chunk not found")
    fname = f"{s['slug']}-{name}" if download else None
    return FileResponse(p, media_type="audio/mpeg", filename=fname,
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/streams/{stream_id}/peaks/{date}/{name}")
async def chunk_peaks(stream_id: int, date: str, name: str, user=Depends(auth.require_user)):
    s = get_stream_or_404(stream_id)
    try:
        p = audio.chunk_path(s["slug"], date, name)
    except ValueError:
        raise HTTPException(400, "bad path")
    if not os.path.isfile(p):
        raise HTTPException(404, "Chunk not found")
    complete = time.time() - os.path.getmtime(p) > 30
    return JSONResponse(await audio.compute_peaks(p, cache=complete))


@app.get("/api/streams/{stream_id}/live")
async def live(stream_id: int, request: Request, user=Depends(auth.require_user)):
    """Re-encode the source to MP3 so any stream type (Icecast, HLS, AAC) plays in any browser,
    and so an http:// stream still plays when this site is served over https."""
    s = get_stream_or_404(stream_id)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", s["url"], "-vn",
        "-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def gen():
        try:
            while True:
                data = await proc.stdout.read(8192)
                if not data or await request.is_disconnected():
                    break
                yield data
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    return StreamingResponse(gen(), media_type="audio/mpeg",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/logos/{name}")
def logo(name: str):
    if "/" in name or ".." in name:
        raise HTTPException(400)
    p = os.path.join(LOGO_DIR, name)
    if not os.path.isfile(p):
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "public, max-age=86400"})


# ---- admin API ----------------------------------------------------------------

class SettingsIn(BaseModel):
    recordings_path: str = Field(min_length=1)
    retention_days: int = Field(ge=1, le=3650)
    chunk_minutes: int = Field(ge=1, le=180)
    bitrate_kbps: int = Field(ge=32, le=320)
    shazam_interval: int = Field(ge=30, le=3600)


class StreamIn(BaseModel):
    name: str = Field(default="", max_length=80)
    url: str = Field(min_length=4)
    enabled: bool = True
    is_music: bool = False
    meta_source: str = Field(default="icy", pattern="^(icy|shazam|none)$")


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    password: str | None = None
    role: str = Field(default="viewer", pattern="^(admin|viewer)$")


@app.get("/api/admin/settings")
def get_settings(user=Depends(auth.require_admin)):
    s = db.get_settings()
    return {**s, "disk": audio.disk_usage()}


@app.put("/api/admin/settings")
async def put_settings(body: SettingsIn, user=Depends(auth.require_admin)):
    old = db.get_settings()
    path = os.path.abspath(body.recordings_path)
    err = check_writable(path)
    if err:
        raise HTTPException(400, f"Can't write to {path}: {err}")
    data = body.model_dump()
    data["recordings_path"] = path
    for k, v in data.items():
        db.set_setting(k, v)
    restart_keys = ("recordings_path", "chunk_minutes", "bitrate_kbps")
    restarted = any(str(old[k]) != str(data[k]) for k in restart_keys)
    if restarted:
        await manager.restart_all()
    if str(old["retention_days"]) != str(data["retention_days"]):
        await asyncio.to_thread(audio.apply_retention)
    return {"ok": True, "restarted": restarted}


@app.get("/api/admin/streams")
def admin_streams(user=Depends(auth.require_admin)):
    out = []
    for s in db.streams():
        out.append({**stream_out(s), "url": s["url"], "detail": manager.status(s["id"])})
    return out


@app.post("/api/admin/streams")
async def create_stream(body: StreamIn, user=Depends(auth.require_admin)):
    if not body.name.strip():  # no name given: ask the stream, else use the host name
        info = await metadata.probe_station(body.url.strip())
        body.name = (info.get("name") or urlparse(body.url).hostname or "Stream")[:80]
    sid = db.ex("INSERT INTO streams(name, slug, url, enabled, is_music, meta_source, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (body.name.strip(), db.slugify(body.name), body.url.strip(), int(body.enabled),
                 int(body.is_music), body.meta_source, int(time.time())))
    await manager.restart_stream(sid)
    return {"id": sid}


@app.put("/api/admin/streams/{stream_id}")
async def update_stream(stream_id: int, body: StreamIn, user=Depends(auth.require_admin)):
    old = get_stream_or_404(stream_id)
    if not body.name.strip():
        body.name = old["name"]
    db.ex("UPDATE streams SET name=?, url=?, enabled=?, is_music=?, meta_source=? WHERE id=?",
          (body.name.strip(), body.url.strip(), int(body.enabled), int(body.is_music),
           body.meta_source, stream_id))
    await manager.restart_stream(stream_id)
    return {"ok": True}


@app.get("/api/admin/probe")
async def probe(url: str, user=Depends(auth.require_admin)):
    """Read the station name/genre/description a stream announces, to pre-fill the Add stream form."""
    if not url.strip():
        raise HTTPException(400, "url required")
    return await metadata.probe_station(url.strip())


@app.post("/api/admin/streams/{stream_id}/restart")
async def restart_stream(stream_id: int, user=Depends(auth.require_admin)):
    get_stream_or_404(stream_id)
    await manager.restart_stream(stream_id)
    return {"ok": True}


@app.delete("/api/admin/streams/{stream_id}")
async def delete_stream(stream_id: int, user=Depends(auth.require_admin)):
    s = get_stream_or_404(stream_id)
    db.ex("DELETE FROM streams WHERE id=?", (stream_id,))
    await manager.restart_stream(stream_id)
    if s["logo_file"]:
        try:
            os.remove(os.path.join(LOGO_DIR, s["logo_file"]))
        except OSError:
            pass
    return {"ok": True, "note": "Recordings were left on disk."}


@app.post("/api/admin/streams/{stream_id}/logo")
async def upload_logo(stream_id: int, file: UploadFile, user=Depends(auth.require_admin)):
    s = get_stream_or_404(stream_id)
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(400, "Logo must be under 5 MB")
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        raise HTTPException(400, "That file isn't a PNG, JPEG, GIF or WebP image")
    im = im.convert("RGBA")
    im.thumbnail((400, 400))
    name = f"{s['slug']}-{int(time.time())}.png"
    im.save(os.path.join(LOGO_DIR, name), optimize=True)
    if s["logo_file"]:
        try:
            os.remove(os.path.join(LOGO_DIR, s["logo_file"]))
        except OSError:
            pass
    db.ex("UPDATE streams SET logo_file=? WHERE id=?", (name, stream_id))
    return {"logo": f"/logos/{name}"}


@app.get("/api/admin/users")
def list_users(user=Depends(auth.require_admin)):
    return db.q("SELECT id, username, role, created_at FROM users ORDER BY username")


@app.post("/api/admin/users")
def create_user(body: UserIn, user=Depends(auth.require_admin)):
    if not body.password or len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if db.q1("SELECT 1 FROM users WHERE username=?", (body.username.strip(),)):
        raise HTTPException(400, "That username is taken")
    uid = db.ex("INSERT INTO users(username, password_hash, role, created_at) VALUES (?,?,?,?)",
                (body.username.strip(), auth.hash_password(body.password), body.role, int(time.time())))
    return {"id": uid}


def admin_count() -> int:
    return db.q1("SELECT COUNT(*) n FROM users WHERE role='admin'")["n"]


@app.put("/api/admin/users/{uid}")
def update_user(uid: int, body: UserIn, user=Depends(auth.require_admin)):
    target = db.q1("SELECT * FROM users WHERE id=?", (uid,))
    if not target:
        raise HTTPException(404)
    if target["role"] == "admin" and body.role != "admin" and admin_count() <= 1:
        raise HTTPException(400, "There must be at least one admin")
    clash = db.q1("SELECT id FROM users WHERE username=? AND id<>?", (body.username.strip(), uid))
    if clash:
        raise HTTPException(400, "That username is taken")
    db.ex("UPDATE users SET username=?, role=? WHERE id=?", (body.username.strip(), body.role, uid))
    if body.password:
        if len(body.password) < 8:
            raise HTTPException(400, "Password must be at least 8 characters")
        db.ex("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(body.password), uid))
    return {"ok": True}


@app.delete("/api/admin/users/{uid}")
def delete_user(uid: int, user=Depends(auth.require_admin)):
    if uid == user["id"]:
        raise HTTPException(400, "You can't delete your own account")
    target = db.q1("SELECT role FROM users WHERE id=?", (uid,))
    if not target:
        raise HTTPException(404)
    if target["role"] == "admin" and admin_count() <= 1:
        raise HTTPException(400, "There must be at least one admin")
    db.ex("DELETE FROM users WHERE id=?", (uid,))
    return {"ok": True}


# ---- song tags (any signed-in user can correct or add them) ------------------------

class TrackIn(BaseModel):
    artist: str = Field(default="", max_length=200)
    title: str = Field(min_length=1, max_length=200)
    album: str | None = Field(default=None, max_length=200)
    tags: list[str] = []
    ts: float | None = None
    lookup: bool = True


@app.post("/api/streams/{stream_id}/tracks")
async def add_track(stream_id: int, body: TrackIn, user=Depends(auth.require_user)):
    get_stream_or_404(stream_id)
    if body.ts is None:
        raise HTTPException(400, "ts required")
    info = await metadata.itunes_lookup(body.artist, body.title) if body.lookup else {}
    tags = list(body.tags)
    if info.get("genre") and info["genre"].lower() not in (t.lower() for t in tags):
        tags.insert(0, info["genre"])
    if info.get("year") and info["year"] not in tags:
        tags.append(info["year"])
    tid = db.add_track(stream_id, body.artist.strip(), body.title.strip(), body.album or info.get("album"),
                       info.get("artwork"), f"{body.artist} - {body.title}", body.ts, tags,
                       info.get("duration"), source=f"manual:{user['username']}")
    return db.track_out(db.q1("SELECT * FROM tracks WHERE id=?", (tid,)))


@app.put("/api/tracks/{track_id}")
async def edit_track(track_id: int, body: TrackIn, user=Depends(auth.require_user)):
    t = db.q1("SELECT * FROM tracks WHERE id=?", (track_id,))
    if not t:
        raise HTTPException(404)
    changed_song = (body.artist.strip().lower(), body.title.strip().lower()) != \
        ((t["artist"] or "").lower(), (t["title"] or "").lower())
    art, album, dur = t["artwork_url"], body.album if body.album is not None else t["album"], t["duration"]
    if changed_song and body.lookup:  # a corrected match gets fresh artwork and details
        info = await metadata.itunes_lookup(body.artist, body.title)
        art, dur = info.get("artwork"), info.get("duration")
        album = body.album or info.get("album")
    db.ex("UPDATE tracks SET artist=?, title=?, album=?, tags=?, artwork_url=?, duration=?, ts=COALESCE(?, ts), "
          "source=? WHERE id=?",
          (body.artist.strip(), body.title.strip(), album, db.join_tags(body.tags), art, dur, body.ts,
           f"edited:{user['username']}" if changed_song else t["source"], track_id))
    return db.track_out(db.q1("SELECT * FROM tracks WHERE id=?", (track_id,)))


@app.delete("/api/tracks/{track_id}")
def delete_track(track_id: int, user=Depends(auth.require_user)):
    db.ex("DELETE FROM tracks WHERE id=?", (track_id,))
    return {"ok": True}
