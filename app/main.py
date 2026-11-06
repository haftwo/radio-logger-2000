"""Radio Logger - 2000: web app entry point."""
import asyncio
import csv
import datetime as dt
import io
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from urllib.parse import urlencode, urlparse

from . import audio, auth, db, metadata, outages, sso, transcribe
from .recorder import check_writable, manager

logging.basicConfig(level=os.environ.get("RL_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
HERE = os.path.dirname(__file__)
STATIC = os.path.join(HERE, "static")
LOGO_DIR = os.path.join(db.CONFIG_DIR, "logos")
APP_NAME = "Radio Logger - 2000"

# Stamped into the image by the GitHub Actions build (see Dockerfile). "dev" when run from source.
VERSION = os.environ.get("RL_VERSION", "dev")
BUILD_SHA = os.environ.get("RL_BUILD_SHA", "")
BUILD_DATE = os.environ.get("RL_BUILD_DATE", "")


def _built_at() -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(BUILD_DATE.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def version_info() -> dict:
    built = _built_at()
    return {"version": VERSION, "sha": BUILD_SHA[:7], "built": built.isoformat() if built else None}


def _version_badge() -> tuple[str, str]:
    built = _built_at()
    label = f"v{VERSION}" if VERSION != "dev" else "dev build"
    if built:
        label += f" · {built.day} {built:%b}"
    title = " · ".join(filter(None, [
        f"Version {VERSION}",
        f"commit {BUILD_SHA[:7]}" if BUILD_SHA else "",
        f"built {built.day} {built:%b %Y, %H:%M}" if built else "",
    ]))
    return label, title


@asynccontextmanager
async def lifespan(_app):
    db.init()
    auth.ensure_admin()
    sso.seed_from_env()
    transcribe.init_db()
    outages.init_db()
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
ASSET_VERSION = (os.environ.get("RL_BUILD_SHA", "")[:7]
                 or str(int(max(os.path.getmtime(os.path.join(STATIC, f)) for f in os.listdir(STATIC)))))


def page(name: str) -> HTMLResponse:
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        html = f.read()
    for asset in ("app.css", "dashboard.js", "admin.js", "outages.js"):
        html = html.replace(f"/static/{asset}\"", f"/static/{asset}?v={ASSET_VERSION}\"")
    label, title = _version_badge()
    html = html.replace("{{VERSION_TITLE}}", title).replace("{{VERSION}}", label)
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


@app.get("/outages")
def outages_page(request: Request):
    if not auth.current_user(request):
        return RedirectResponse("/login", 303)
    return page("outages.html")


@app.get("/login")
def login_page():
    return page("login.html")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    u = db.q1("SELECT * FROM users WHERE username=? AND auth='local'", (username.strip(),))
    if not u or not u["password_hash"] or not auth.verify_password(password, u["password_hash"]):
        time.sleep(0.5)
        return RedirectResponse("/login?error=1", 303)
    if sso.enabled() and sso.config()["sso_password_login"] == "admins" and u["role"] != "admin":
        return RedirectResponse("/login?error=sso_only", 303)
    request.session.clear()
    request.session["uid"] = u["id"]
    db.ex("UPDATE users SET last_login=? WHERE id=?", (int(time.time()), u["id"]))
    return RedirectResponse("/", 303)


# ---- Microsoft Entra ID single sign-on ---------------------------------------------

@app.get("/auth/config")
def auth_config():
    """Public: tells the login page which sign-in options to show."""
    c = sso.config()
    return {"sso": sso.enabled(), "label": c["sso_button_label"] or "Sign in with Microsoft",
            "password_login": "admins" if sso.enabled() and c["sso_password_login"] == "admins" else "all"}


@app.get("/auth/login")
async def sso_login(request: Request):
    if not sso.enabled():
        return RedirectResponse("/login?error=sso_off", 303)
    try:
        return RedirectResponse(await sso.login_url(request), 303)
    except Exception as e:
        logging.getLogger("radiologger.sso").exception("SSO start failed")
        return RedirectResponse("/login?" + urlencode({"sso_error": f"Couldn't reach Microsoft: {e}"}), 303)


@app.get("/auth/callback")
async def sso_callback(request: Request, code: str = "", state: str = "", error: str = "",
                       error_description: str = ""):
    log = logging.getLogger("radiologger.sso")
    if error:
        msg = (error_description or error).split("\r\n")[0][:300]
        return RedirectResponse("/login?" + urlencode({"sso_error": msg}), 303)
    try:
        claims = await sso.complete(request, code, state)
    except sso.SSOError as e:
        return RedirectResponse("/login?" + urlencode({"sso_error": str(e)}), 303)
    except Exception as e:
        log.exception("SSO callback failed")
        return RedirectResponse("/login?" + urlencode({"sso_error": f"Sign-in failed: {e}"}), 303)
    if "_claim_names" in claims and "groups" in claims.get("_claim_names", {}):
        log.warning("Group overage for %s: too many groups for the token; use app roles or "
                    "'Groups assigned to the application'", claims.get("preferred_username"))
    role = sso.role_for(claims)
    who = claims.get("preferred_username") or claims.get("email") or "this account"
    if not role:
        return RedirectResponse("/login?" + urlencode(
            {"sso_error": f"{who} isn't assigned access to Radio Logger. Ask an administrator."}), 303)
    uid = sso.upsert_user(claims, role)
    request.session.clear()
    request.session["uid"] = uid
    log.info("SSO sign-in: %s as %s", who, role)
    return RedirectResponse("/", 303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


@app.get("/healthz")
def healthz():
    return {"ok": True, **version_info()}


# ---- viewer API ---------------------------------------------------------------

def stream_out(s: dict) -> dict:
    return {
        "id": s["id"], "name": s["name"], "slug": s["slug"], "enabled": bool(s["enabled"]),
        "is_music": bool(s["is_music"]), "meta_source": s["meta_source"],
        "transcribe": bool(s.get("transcribe", 1)),
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
    return {**user, "app": APP_NAME, "tz": time.tzname[0], "tz_name": os.environ.get("TZ", "UTC"), **version_info()}


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


def local_bounds(day: dt.date, days: int = 1) -> tuple[float, float]:
    """Local-midnight timestamps for `days` days ending on (and including) `day`."""
    first = day - dt.timedelta(days=days - 1)
    return (dt.datetime.combine(first, dt.time()).timestamp(),
            dt.datetime.combine(day + dt.timedelta(days=1), dt.time()).timestamp())


def parse_day_or_400(date: str) -> dt.date:
    try:
        return audio.parse_date(date)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")


@app.get("/api/streams/{stream_id}/charts")
def stream_charts(stream_id: int, date: str, limit: int = 10, user=Depends(auth.require_user)):
    """Top songs for the day and for the 7 days ending on that day."""
    get_stream_or_404(stream_id)
    day = parse_day_or_400(date)
    limit = max(1, min(limit, 50))
    return {"date": date,
            "day": db.top_tracks(stream_id, *local_bounds(day, 1), limit),
            "week": db.top_tracks(stream_id, *local_bounds(day, 7), limit)}


@app.get("/api/streams/{stream_id}/tracks.csv")
def export_tracks(stream_id: int, range: str = "day", date: str | None = None, user=Depends(auth.require_user)):
    """Download played songs as CSV. range = day | week (7 days ending on date) | all."""
    s = get_stream_or_404(stream_id)
    if range not in ("day", "week", "all"):
        raise HTTPException(400, "range must be day, week or all")
    if range == "all":
        rows, label = db.tracks_for_export(stream_id), "all"
    else:
        if not date:
            raise HTTPException(400, "date required")
        day = parse_day_or_400(date)
        rows = db.tracks_for_export(stream_id, *local_bounds(day, 7 if range == "week" else 1))
        label = date if range == "day" else f"week-to-{date}"

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Time", "Station", "Artist", "Title", "Album", "Tags", "Duration (s)", "Source"])
    for r in rows:
        when = dt.datetime.fromtimestamp(r["ts"])
        w.writerow([when.strftime("%Y-%m-%d"), when.strftime("%H:%M:%S"), s["name"], r["artist"] or "",
                    r["title"] or "", r["album"] or "", (r["tags"] or "").replace(",", "; "),
                    round(r["duration"]) if r["duration"] else "", r["source"] or ""])
    filename = f"{s['slug']}-played-{label}.csv"
    # BOM so Excel opens accented artist names correctly
    return StreamingResponse(iter(["﻿" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


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


_clip_slots = asyncio.Semaphore(2)  # clip cuts are CPU work on the Unraid box; don't run many at once


@app.get("/api/streams/{stream_id}/clip")
async def clip(stream_id: int, start: float, end: float, user=Depends(auth.require_user)):
    """Cut the audio between two moments (epoch seconds) into one MP3, across chunks if needed."""
    s = get_stream_or_404(stream_id)
    if not (end > start and end - start >= 0.5):
        raise HTTPException(400, "The out point must be after the in point")
    if end - start > audio.CLIP_MAX_SECONDS:
        raise HTTPException(400, f"Clips are limited to {audio.CLIP_MAX_SECONDS // 3600} hours")
    parts = audio.clip_parts(s["slug"], start, end)
    if not parts:
        raise HTTPException(404, "Nothing recorded in that range")
    if _clip_slots.locked():
        raise HTTPException(429, "Another clip is being cut — try again in a moment")
    fd, out = tempfile.mkstemp(prefix="rl-clip-", suffix=".mp3")
    os.close(fd)
    try:
        async with _clip_slots:
            await audio.cut_clip(parts, out, db.setting_int("bitrate_kbps"))
    except Exception as e:
        os.remove(out)
        logging.getLogger("radiologger.clip").warning("clip cut failed: %s", e)
        raise HTTPException(500, "Couldn't cut that clip")
    a, b = dt.datetime.fromtimestamp(start), dt.datetime.fromtimestamp(end)
    fname = f"{s['slug']}-{a:%Y%m%d-%H%M%S}-to-{b:%H%M%S}.mp3"
    return FileResponse(out, media_type="audio/mpeg", filename=fname,
                        background=BackgroundTask(os.remove, out))


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
    transcribe: bool = True


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
    sid = db.ex("INSERT INTO streams(name, slug, url, enabled, is_music, meta_source, transcribe, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (body.name.strip(), db.slugify(body.name), body.url.strip(), int(body.enabled),
                 int(body.is_music), body.meta_source, int(body.transcribe), int(time.time())))
    await manager.restart_stream(sid)
    return {"id": sid}


@app.put("/api/admin/streams/{stream_id}")
async def update_stream(stream_id: int, body: StreamIn, user=Depends(auth.require_admin)):
    old = get_stream_or_404(stream_id)
    if not body.name.strip():
        body.name = old["name"]
    db.ex("UPDATE streams SET name=?, url=?, enabled=?, is_music=?, meta_source=?, transcribe=? WHERE id=?",
          (body.name.strip(), body.url.strip(), int(body.enabled), int(body.is_music),
           body.meta_source, int(body.transcribe), stream_id))
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


class SSOIn(BaseModel):
    sso_enabled: bool = False
    sso_tenant_id: str = Field(default="", max_length=100)
    sso_client_id: str = Field(default="", max_length=100)
    sso_client_secret: str | None = Field(default=None, max_length=500)   # None/blank = keep current
    sso_admin_role: str = Field(default="Admin", max_length=100)
    sso_admin_group: str = Field(default="", max_length=100)
    sso_viewer_access: str = Field(default="tenant", pattern="^(tenant|role)$")
    sso_viewer_role: str = Field(default="Viewer", max_length=100)
    sso_password_login: str = Field(default="all", pattern="^(all|admins)$")
    sso_public_url: str = Field(default="", max_length=300)
    sso_button_label: str = Field(default="Sign in with Microsoft", max_length=60)


@app.get("/api/admin/sso")
def get_sso(request: Request, user=Depends(auth.require_admin)):
    c = sso.config()
    secret = c.pop("sso_client_secret")
    return {**c, "sso_enabled": c["sso_enabled"] == "1", "secret_set": bool(secret),
            "redirect_uri": sso.redirect_uri(request), "ready": sso.enabled(),
            "local_admins": db.q1("SELECT COUNT(*) n FROM users WHERE role='admin' AND auth='local' "
                                  "AND password_hash<>''")["n"]}


@app.put("/api/admin/sso")
async def put_sso(body: SSOIn, user=Depends(auth.require_admin)):
    data = body.model_dump()
    if data["sso_public_url"] and not data["sso_public_url"].startswith(("https://", "http://localhost")):
        raise HTTPException(400, "Public URL must start with https:// (Microsoft requires HTTPS redirects)")
    secret = (data.pop("sso_client_secret") or "").strip()
    if secret:
        db.set_setting("sso_client_secret", secret)
    data["sso_enabled"] = "1" if data["sso_enabled"] else "0"
    if data["sso_enabled"] == "1":
        if not (data["sso_tenant_id"] and data["sso_client_id"] and (secret or sso.config()["sso_client_secret"])):
            raise HTTPException(400, "Tenant ID, Client ID and Client secret are all needed to turn SSO on")
        try:
            await sso.discovery(data["sso_tenant_id"].strip())
        except Exception:
            raise HTTPException(400, f"Microsoft doesn't recognise tenant '{data['sso_tenant_id']}'")
    if data["sso_password_login"] == "admins" and data["sso_enabled"] == "1":
        if not db.q1("SELECT 1 FROM users WHERE role='admin' AND auth='local' AND password_hash<>''"):
            raise HTTPException(400, "Keep at least one local admin account before limiting password sign-in")
    for k, v in data.items():
        db.set_setting(k, str(v).strip())
    sso._discovery.clear()
    return {"ok": True}


@app.delete("/api/admin/sso/secret")
def clear_sso_secret(user=Depends(auth.require_admin)):
    db.set_setting("sso_client_secret", "")
    db.set_setting("sso_enabled", "0")
    return {"ok": True}


@app.get("/api/admin/users")
def list_users(user=Depends(auth.require_admin)):
    return db.q("SELECT id, username, role, created_at, auth, display_name, email, last_login "
                "FROM users ORDER BY auth, username")


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


def is_last_breakglass(target: dict) -> bool:
    """True if removing this account's local admin access would lock everyone out of password sign-in."""
    if not (sso.enabled() and sso.config()["sso_password_login"] == "admins"):
        return False
    if target["auth"] != "local" or target["role"] != "admin":
        return False
    return db.q1("SELECT COUNT(*) n FROM users WHERE role='admin' AND auth='local' AND password_hash<>''")["n"] <= 1


@app.put("/api/admin/users/{uid}")
def update_user(uid: int, body: UserIn, user=Depends(auth.require_admin)):
    target = db.q1("SELECT * FROM users WHERE id=?", (uid,))
    if not target:
        raise HTTPException(404)
    if target["role"] == "admin" and body.role != "admin" and admin_count() <= 1:
        raise HTTPException(400, "There must be at least one admin")
    if body.role != "admin" and is_last_breakglass(target):
        raise HTTPException(400, "This is the last local admin (your break-glass login while SSO is on)")
    if target["auth"] == "sso":
        if body.password:
            raise HTTPException(400, "Microsoft accounts sign in with SSO and can't have a password")
        db.ex("UPDATE users SET role=? WHERE id=?", (body.role, uid))  # refreshed from Entra at next sign-in
        return {"ok": True, "note": "Role is re-synced from Microsoft at this user's next sign-in."}
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
    target = db.q1("SELECT role, auth FROM users WHERE id=?", (uid,))
    if not target:
        raise HTTPException(404)
    if is_last_breakglass(target):
        raise HTTPException(400, "This is the last local admin (your break-glass login while SSO is on)")
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


# ---- outages (silence / drop-outs) ----------------------------------------------------

@app.get("/api/outages/status")
def outage_status(user=Depends(auth.require_user)):
    """Live health of every stream, plus availability over the last 24 hours and 7 days."""
    now = time.time()
    out = []
    for s in db.streams():
        st = manager.status(s["id"]) if s["enabled"] else None
        h = st or {"health": "disabled", "health_since": None, "quiet_for": 0, "last_audio": None}
        avail = {}
        for label, secs in (("day", 86400), ("week", 7 * 86400)):
            start = max(now - secs, s["created_at"])
            span = now - start
            avail[label] = round(100 * (1 - outages.downtime(s["id"], start, now) / span), 3) if span > 60 else None
        last = db.q1("SELECT start_ts, end_ts, cause FROM outages WHERE stream_id=? ORDER BY start_ts DESC LIMIT 1",
                     (s["id"],))
        out.append({
            "id": s["id"], "name": s["name"], "enabled": bool(s["enabled"]),
            "logo": f"/logos/{s['logo_file']}" if s["logo_file"] else None,
            "health": h["health"], "since": h["health_since"], "quiet_for": h["quiet_for"],
            "last_audio": h["last_audio"], "recorder": st["state"] if st else "disabled",
            "last_error": st["last_error"] if st else "",
            "availability": avail, "last_outage": last,
            "outages_24h": db.q1("SELECT COUNT(*) n FROM outages WHERE stream_id=? AND start_ts>=?",
                                 (s["id"], now - 86400))["n"],
        })
    return {"now": now, "threshold": outages.threshold(), "alarms": outages.alarms_on(), "streams": out}


def _outage_window(days: int) -> tuple[float | None, float | None]:
    if days <= 0:
        return None, None
    return time.time() - min(days, outages.KEEP_DAYS) * 86400, None


@app.get("/api/outages")
def list_outages(stream_id: int = 0, days: int = 7, user=Depends(auth.require_user)):
    rows = outages.listing(stream_id or None, *_outage_window(days))
    return {"outages": rows, "count": len(rows), "downtime": sum(r["duration"] for r in rows)}


def csv_cell(v) -> str:
    """Neutralise spreadsheet formulas (a station name like =HYPERLINK(...) stays plain text)."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


@app.get("/api/outages.csv")
def export_outages(stream_id: int = 0, days: int = 7, user=Depends(auth.require_user)):
    rows = outages.listing(stream_id or None, *_outage_window(days), limit=100000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Station", "Cause", "Started", "Ended", "Duration (s)", "Duration", "Alarm emailed"])
    fmt = lambda ts: dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "ongoing"
    for r in reversed(rows):
        w.writerow([csv_cell(r["stream_name"]), r["cause_label"], fmt(r["start_ts"]), fmt(r["end_ts"]),
                    round(r["duration"]), outages.fmt_duration(r["duration"]), "yes" if r["alerted"] == 1 else "no"])
    name = f"outages-{'all' if days <= 0 else f'{days}d'}-{dt.date.today().isoformat()}.csv"
    return StreamingResponse(iter(["\ufeff" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


class AlarmsIn(BaseModel):
    outage_threshold_seconds: int = Field(default=30, ge=10, le=3600)
    silence_db: int = Field(default=-50, ge=-90, le=-10)
    alarm_enabled: bool = False
    alarm_recipients: str = Field(default="", max_length=2000)
    alarm_delay_seconds: int = Field(default=30, ge=10, le=86400)
    alarm_on_recovery: bool = True
    smtp_host: str = Field(default="", max_length=200)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: str = Field(default="starttls", pattern="^(starttls|ssl|none)$")
    smtp_username: str = Field(default="", max_length=200)
    smtp_password: str | None = Field(default=None, max_length=500)   # None/blank = keep current
    smtp_from: str = Field(default="", max_length=200)


def _alarms_payload(body: AlarmsIn) -> dict:
    data = body.model_dump()
    for k in ("alarm_enabled", "alarm_on_recovery"):
        data[k] = "1" if data[k] else "0"
    pw = data.pop("smtp_password")
    data["smtp_password"] = pw if pw else outages.cfg()["smtp_password"]
    data["alarm_recipients"] = ", ".join(outages.recipients(data["alarm_recipients"]))
    data["alarm_delay_seconds"] = max(data["alarm_delay_seconds"], data["outage_threshold_seconds"])
    return {k: str(v).strip() if k != "smtp_password" else str(v) for k, v in data.items()}


@app.get("/api/admin/alarms")
def get_alarms(user=Depends(auth.require_admin)):
    c = outages.cfg()
    pw = c.pop("smtp_password")
    return {**c, "alarm_enabled": c["alarm_enabled"] == "1", "alarm_on_recovery": c["alarm_on_recovery"] == "1",
            "password_set": bool(pw), "public_url": db.get_settings().get("sso_public_url", ""),
            "state": outages.STATE}


@app.put("/api/admin/alarms")
async def put_alarms(body: AlarmsIn, user=Depends(auth.require_admin)):
    data = _alarms_payload(body)
    if data["alarm_enabled"] == "1":
        if not data["smtp_host"]:
            raise HTTPException(400, "Enter an SMTP server before turning email alarms on")
        if not outages.recipients(data["alarm_recipients"]):
            raise HTTPException(400, "Add at least one recipient before turning email alarms on")
    bad = [a for a in outages.recipients(data["alarm_recipients"]) if not outages.EMAIL_RE.match(a)]
    if bad:
        raise HTTPException(400, f"Not an email address: {', '.join(bad)}")
    old_db = outages.cfg()["silence_db"]
    for k, v in data.items():
        db.set_setting(k, v)
    restarted = str(old_db) != data["silence_db"]
    if restarted:  # the silence level is baked into each recorder's ffmpeg filter
        await manager.restart_all()
    return {"ok": True, "restarted": restarted}


@app.delete("/api/admin/alarms/password")
def clear_smtp_password(user=Depends(auth.require_admin)):
    db.set_setting("smtp_password", "")
    return {"ok": True}


@app.post("/api/admin/alarms/test")
async def test_alarms(body: AlarmsIn, user=Depends(auth.require_admin)):
    data = _alarms_payload(body)
    when = dt.datetime.now().strftime("%a %d %b %Y, %H:%M:%S")
    try:
        await asyncio.to_thread(outages.send_email, data, "[Radio Logger] Test alarm",
                                f"This is a test email from {APP_NAME}, sent by {user['username']} at {when}.\n\n"
                                "If you can read this, outage alarms will reach you.\n")
    except Exception as e:
        raise HTTPException(400, f"Couldn't send: {type(e).__name__}: {e}")
    return {"ok": True, "to": outages.recipients(data["alarm_recipients"])}


# ---- transcripts (talk stations) ---------------------------------------------------

def day_bounds(date: str) -> tuple[float, float]:
    try:
        day = audio.parse_date(date)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    return (dt.datetime.combine(day, dt.time()).timestamp(),
            dt.datetime.combine(day + dt.timedelta(days=1), dt.time()).timestamp())


@app.get("/api/streams/{stream_id}/transcript")
def get_transcript(stream_id: int, date: str, user=Depends(auth.require_user)):
    s = get_stream_or_404(stream_id)
    start, end = day_bounds(date)
    segs = transcribe.day_segments(stream_id, start, end)
    c = transcribe.cfg()
    watch = [w.strip() for w in c["whisper_keywords"].split(",") if w.strip()]
    return {
        "enabled": transcribe.enabled() and bool(s.get("transcribe", 1)),
        "segments": segs,
        "keywords": transcribe.keywords(segs, watch, exclude=[s["name"], s.get("icy_name") or ""]),
        "watch": watch,
        "status": transcribe.day_status(stream_id, start, end),
        "queue": transcribe.STATE["queue"],
        "busy": transcribe.STATE["busy"],
    }


@app.get("/api/streams/{stream_id}/transcript/search")
def search_transcript(stream_id: int, q: str, date: str = "", user=Depends(auth.require_user)):
    get_stream_or_404(stream_id)
    start = end = None
    if date:
        start, end = day_bounds(date)
    hits = transcribe.search(stream_id, q, start, end)
    for h in hits:
        h["date"] = dt.datetime.fromtimestamp(h["ts"]).date().isoformat()
    return {"q": q, "results": hits}


class WhisperIn(BaseModel):
    whisper_enabled: bool = False
    whisper_api: str = Field(default="openai", pattern="^(openai|asr|wyoming)$")
    whisper_url: str = Field(default="", max_length=300)
    whisper_model: str = Field(default="", max_length=200)
    whisper_language: str = Field(default="en", max_length=10)
    whisper_api_key: str | None = Field(default=None, max_length=300)
    whisper_backfill_hours: int = Field(default=24, ge=1, le=24 * 60)
    whisper_keywords: str = Field(default="", max_length=2000)
    whisper_timeout: int = Field(default=900, ge=30, le=7200)


def _whisper_payload(body: WhisperIn) -> dict:
    data = body.model_dump()
    data["whisper_enabled"] = "1" if data["whisper_enabled"] else "0"
    key = data.pop("whisper_api_key")
    data["whisper_api_key"] = key.strip() if key else transcribe.cfg()["whisper_api_key"]
    return {k: str(v).strip() for k, v in data.items()}


@app.get("/api/admin/transcription")
def get_whisper(user=Depends(auth.require_admin)):
    c = transcribe.cfg()
    key = c.pop("whisper_api_key")
    st = dict(transcribe.STATE)
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM transcript_chunks GROUP BY status")}
    talk = [x["name"] for x in db.streams() if not x["is_music"] and x.get("transcribe", 1)]
    return {**c, "whisper_enabled": c["whisper_enabled"] == "1", "key_set": bool(key),
            "state": st, "counts": counts, "talk_streams": talk,
            "segments": db.q1("SELECT COUNT(*) n FROM segments")["n"]}


@app.put("/api/admin/transcription")
def put_whisper(body: WhisperIn, user=Depends(auth.require_admin)):
    data = _whisper_payload(body)
    if data["whisper_enabled"] == "1" and not data["whisper_url"]:
        raise HTTPException(400, "Enter the Whisper server address first")
    for k, v in data.items():
        db.set_setting(k, v)
    return {"ok": True}


@app.post("/api/admin/transcription/test")
async def test_whisper(body: WhisperIn, user=Depends(auth.require_admin)):
    data = _whisper_payload(body)
    if not data["whisper_url"]:
        raise HTTPException(400, "Enter the Whisper server address first")
    try:
        return await transcribe.test_connection(data)
    except Exception as e:
        raise HTTPException(400, f"Couldn't transcribe with that server: {type(e).__name__}: {e}")


@app.post("/api/admin/transcription/retry")
def retry_whisper(user=Depends(auth.require_admin)):
    db.ex("DELETE FROM transcript_chunks WHERE status='error'")
    transcribe.STATE["last_error"] = ""
    return {"ok": True}
