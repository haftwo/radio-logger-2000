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


async def icy_loop(stream: dict) -> None:
    """Connect with Icy-MetaData and parse StreamTitle blocks forever."""
    sid, url = stream["id"], stream["url"]
    backoff = 5
    while True:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20, read=60), follow_redirects=True,
                                         headers={"Icy-MetaData": "1", "User-Agent": UA}) as c:
                async with c.stream("GET", url) as r:
                    metaint = int(r.headers.get("icy-metaint", "0"))
                    if not metaint:
                        log.warning("[%s] stream sends no ICY metadata; try the Shazam source", sid)
                        await asyncio.sleep(600)
                        continue
                    backoff = 5
                    parser = IcyParser(metaint)
                    async for chunk in r.aiter_raw():
                        for raw in parser.feed(chunk):
                            artist, title = split_title(raw)
                            await record_track(sid, artist, title, raw)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("[%s] ICY metadata error: %s", sid, e)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 120)


async def shazam_loop(stream: dict) -> None:
    try:
        from shazamio import Shazam
    except ImportError:
        log.error("shazamio not installed; Shazam metadata disabled")
        return
    shazam = Shazam()
    sid, slug = stream["id"], stream["slug"]
    while True:
        interval = max(30, db.setting_int("shazam_interval"))
        await asyncio.sleep(interval)
        try:
            d = audio.day_dir(slug, dt.date.today())
            files = sorted(n for n in os.listdir(d) if audio.FILE_RE.match(n)) if os.path.isdir(d) else []
            if not files:
                continue
            src = os.path.join(d, files[-1])
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
            p = await asyncio.create_subprocess_exec(
                "ffmpeg", "-v", "error", "-y", "-sseof", "-14", "-i", src, "-t", "12",
                "-ac", "1", "-ar", "16000", tmp_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await p.wait()
            captured_at = time.time() - 8
            try:
                fn = getattr(shazam, "recognize", None) or getattr(shazam, "recognize_song")
                out = await fn(tmp_path)
            finally:
                os.unlink(tmp_path)
            track = (out or {}).get("track")
            if not track:
                continue
            artist, title = track.get("subtitle", ""), track.get("title", "")
            art = (track.get("images") or {}).get("coverarthq") or (track.get("images") or {}).get("coverart")
            album = None
            for sec in track.get("sections", []):
                for md in sec.get("metadata", []) or []:
                    if md.get("title") == "Album":
                        album = md.get("text")
            await record_track(sid, artist, title, f"{artist} - {title}", art, album, captured_at)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("[%s] Shazam error: %s", sid, e)
