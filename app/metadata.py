"""Now-playing detection for music stations.

Two sources per stream:
  icy    - reads Shoutcast/Icecast "StreamTitle" metadata from the stream itself
  shazam - fingerprints the last few seconds of the current recording chunk
           (useful for HLS streams such as Double J that carry no ICY tags)
Artwork comes from Shazam when available, otherwise the iTunes Search API.
"""
import asyncio
import datetime as dt
import logging
import os
import tempfile
import time

import httpx

from . import audio, db
from .icy import IcyParser

log = logging.getLogger("radiologger.meta")
UA = "RadioLogger2000/1.0"


def split_title(raw: str) -> tuple[str, str]:
    for sep in (" - ", " – ", " — ", " | "):
        if sep in raw:
            a, t = raw.split(sep, 1)
            return a.strip(), t.strip()
    return "", raw.strip()


async def itunes_artwork(artist: str, title: str) -> tuple[str | None, str | None]:
    term = f"{artist} {title}".strip()
    if not term:
        return None, None
    try:
        async with httpx.AsyncClient(timeout=8, headers={"User-Agent": UA}) as c:
            r = await c.get("https://itunes.apple.com/search",
                            params={"term": term, "entity": "song", "limit": 1})
            res = r.json().get("results") or []
            if res:
                art = res[0].get("artworkUrl100", "")
                return (art.replace("100x100bb", "600x600bb") or None), res[0].get("collectionName")
    except Exception as e:  # network errors shouldn't stop logging
        log.debug("itunes lookup failed: %s", e)
    return None, None


async def record_track(stream_id: int, artist: str, title: str, raw: str,
                       artwork: str | None = None, album: str | None = None,
                       ts: float | None = None) -> None:
    last = db.last_track(stream_id)
    if last and (last["artist"] or "").lower() == artist.lower() and (last["title"] or "").lower() == title.lower():
        return
    if not artwork:
        artwork, album2 = await itunes_artwork(artist, title)
        album = album or album2
    db.add_track(stream_id, artist, title, album, artwork, raw, ts)
    log.info("[%s] now playing: %s - %s", stream_id, artist, title)


# Per-stream detection status, shown on the Admin page so failures aren't silent.
STATUS: dict[int, dict] = {}


def set_status(sid: int, source: str, state: str, message: str) -> None:
    st = STATUS.setdefault(sid, {"last_track": None})
    st.update(source=source, state=state, message=message, updated=time.time())
    if state == "error":
        log.warning("[%s] %s: %s", sid, source, message)


async def detect_loop(stream: dict) -> None:
    """Entry point. 'icy' tries stream titles first and falls back to Shazam when the
    stream has none (e.g. HLS) or the connection keeps failing; 'shazam' goes straight to it."""
    if stream["meta_source"] == "shazam":
        return await shazam_loop(stream)
    ok = await icy_loop(stream)
    if not ok:
        await shazam_loop(stream, fallback=True)


async def icy_loop(stream: dict) -> bool:
    """Parse StreamTitle blocks forever. Returns False if this stream can't provide ICY titles."""
    sid, url = stream["id"], stream["url"]
    if ".m3u8" in url.lower():
        set_status(sid, "Stream titles", "info", "HLS streams carry no ICY titles; using Shazam")
        return False
    backoff, failures, ever_worked = 5, 0, False
    while True:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20, read=60), follow_redirects=True,
                                         headers={"Icy-MetaData": "1", "User-Agent": UA}) as c:
                async with c.stream("GET", url) as r:
                    ctype = r.headers.get("content-type", "")
                    if "mpegurl" in ctype.lower():
                        set_status(sid, "Stream titles", "info", "HLS stream (no ICY titles); using Shazam")
                        return False
                    metaint = int(r.headers.get("icy-metaint", "0") or 0)
                    if not metaint:
                        set_status(sid, "Stream titles", "info", "Stream sends no title metadata; using Shazam")
                        return False
                    ever_worked, failures, backoff = True, 0, 5
                    set_status(sid, "Stream titles", "ok", "Listening for title changes")
                    parser = IcyParser(metaint)
                    async for chunk in r.aiter_raw():
                        for raw in parser.feed(chunk):
                            artist, title = split_title(raw)
                            await record_track(sid, artist, title, raw)
                            STATUS[sid]["last_track"] = time.time()
                            set_status(sid, "Stream titles", "ok", f"Last title: {raw[:80]}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            failures += 1
            set_status(sid, "Stream titles", "error", f"{type(e).__name__}: {e}")
            if failures >= 3 and not ever_worked:
                set_status(sid, "Stream titles", "info", "Couldn't read stream titles; using Shazam")
                return False
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 120)


async def shazam_loop(stream: dict, fallback: bool = False) -> None:
    sid, slug = stream["id"], stream["slug"]
    label = "Shazam (fallback)" if fallback else "Shazam"
    try:
        from shazamio import Shazam
    except ImportError:
        set_status(sid, label, "error", "shazamio isn't installed in this image")
        return
    shazam = Shazam()
    set_status(sid, label, "waiting", "Waiting for the first check")
    while True:
        interval = max(30, db.setting_int("shazam_interval"))
        await asyncio.sleep(interval)
        tmp_path = None
        try:
            d = audio.day_dir(slug, dt.date.today())
            files = sorted(n for n in os.listdir(d) if audio.FILE_RE.match(n)) if os.path.isdir(d) else []
            # skip a chunk that has only just started; use the previous one's tail instead
            if files and os.path.getsize(os.path.join(d, files[-1])) < 300_000 and len(files) > 1:
                files = files[:-1]
            if not files:
                set_status(sid, label, "waiting", "No audio recorded yet today")
                continue
            src = os.path.join(d, files[-1])
            fd, tmp_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            p = await asyncio.create_subprocess_exec(
                "ffmpeg", "-v", "error", "-y", "-sseof", "-14", "-i", src, "-t", "12",
                "-ac", "1", "-ar", "16000", tmp_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await p.communicate()
            if p.returncode != 0 or os.path.getsize(tmp_path) < 50_000:
                set_status(sid, label, "error", "Couldn't extract audio: " + err.decode(errors="replace")[-200:])
                continue
            captured_at = time.time() - 8
            fn = getattr(shazam, "recognize", None) or getattr(shazam, "recognize_song")
            out = await fn(tmp_path)
            track = (out or {}).get("track")
            if not track:
                set_status(sid, label, "ok", f"No match at {time.strftime('%H:%M:%S')} (speech or unknown track)")
                continue
            artist, title = track.get("subtitle", ""), track.get("title", "")
            imgs = track.get("images") or {}
            art = imgs.get("coverarthq") or imgs.get("coverart")
            album = None
            for sec in track.get("sections", []):
                for md in sec.get("metadata", []) or []:
                    if md.get("title") == "Album":
                        album = md.get("text")
            await record_track(sid, artist, title, f"{artist} - {title}", art, album, captured_at)
            STATUS[sid]["last_track"] = time.time()
            set_status(sid, label, "ok", f"Matched {artist} - {title} at {time.strftime('%H:%M:%S')}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            set_status(sid, label, "error", f"{type(e).__name__}: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
