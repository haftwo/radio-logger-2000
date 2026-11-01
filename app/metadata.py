"""Now-playing detection for music stations.

Two sources per stream:
  icy    - reads Shoutcast/Icecast "StreamTitle" metadata from the stream itself
  shazam - fingerprints the last few seconds of the current recording chunk
           (useful for HLS streams such as Double J that carry no ICY tags)
Each song is tagged automatically with artist, title, album, genre and year (Shazam
metadata where available, topped up from the iTunes Search API, which also supplies
artwork and song length). Station name/genre/description come from the stream's ICY headers.
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


async def itunes_lookup(artist: str, title: str) -> dict:
    """Artwork, album, genre, year and length for a song, from the free iTunes Search API."""
    term = f"{artist} {title}".strip()
    if not term:
        return {}
    try:
        async with httpx.AsyncClient(timeout=8, headers={"User-Agent": UA}) as c:
            r = await c.get("https://itunes.apple.com/search",
                            params={"term": term, "entity": "song", "limit": 5, "country": "AU"})
            res = r.json().get("results") or []
    except Exception as e:  # network errors shouldn't stop logging
        log.debug("itunes lookup failed: %s", e)
        return {}
    if not res:
        return {}
    # prefer a result whose artist actually matches
    best = next((x for x in res if artist and artist.lower() in (x.get("artistName") or "").lower()), res[0])
    art = (best.get("artworkUrl100") or "").replace("100x100bb", "600x600bb") or None
    year = (best.get("releaseDate") or "")[:4]
    return {
        "artwork": art,
        "album": best.get("collectionName"),
        "genre": best.get("primaryGenreName"),
        "year": year if year.isdigit() else None,
        "duration": (best.get("trackTimeMillis") or 0) / 1000 or None,
    }


async def record_track(stream_id: int, artist: str, title: str, raw: str,
                       artwork: str | None = None, album: str | None = None,
                       ts: float | None = None, tags: list[str] | None = None,
                       duration: float | None = None, source: str = "icy") -> None:
    last = db.last_track(stream_id)
    if last and (last["artist"] or "").lower() == artist.lower() and (last["title"] or "").lower() == title.lower():
        return
    info = await itunes_lookup(artist, title)
    tags = list(tags or [])
    if info.get("genre"):
        tags.insert(0, info["genre"])
    if info.get("year") and not any(t.isdigit() and len(t) == 4 for t in tags):
        tags.append(info["year"])
    db.add_track(stream_id, artist, title, album or info.get("album"), artwork or info.get("artwork"),
                 raw, ts, tags, duration or info.get("duration"), source)
    log.info("[%s] now playing: %s - %s %s", stream_id, artist, title, tags)


def save_station_info(stream_id: int, headers) -> None:
    """Keep the station details the stream announces about itself (icy-name, icy-genre ...)."""
    def h(k):
        v = headers.get(k)
        if not v:
            return None
        try:  # ICY headers are often latin-1 mis-decoded UTF-8
            return v.encode("latin-1").decode("utf-8").strip() or None
        except (UnicodeError, AttributeError):
            return v.strip() or None
    vals = {k: h(k.replace("_", "-")) for k in ("icy_name", "icy_genre", "icy_description", "icy_url")}
    if any(vals.values()):
        db.ex("UPDATE streams SET icy_name=?, icy_genre=?, icy_description=?, icy_url=? WHERE id=?",
              (vals["icy_name"], vals["icy_genre"], vals["icy_description"], vals["icy_url"], stream_id))


async def probe_station(url: str) -> dict:
    """Ask the stream who it is: ICY headers first, then ffprobe's metadata (covers HLS/TS service names)."""
    out: dict = {}
    if ".m3u8" not in url.lower():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=10), follow_redirects=True,
                                         headers={"Icy-MetaData": "1", "User-Agent": UA}) as c:
                async with c.stream("GET", url) as r:
                    for k in ("icy-name", "icy-genre", "icy-description", "icy-url", "icy-br"):
                        if r.headers.get(k):
                            v = r.headers[k]
                            try:
                                v = v.encode("latin-1").decode("utf-8")
                            except UnicodeError:
                                pass
                            out[k.replace("icy-", "")] = v.strip()
                    out["content_type"] = r.headers.get("content-type")
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
    if not out.get("name"):
        try:
            p = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-rw_timeout", "10000000", "-show_entries",
                "format_tags:program_tags:stream_tags", "-of", "json", url,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            raw, _ = await asyncio.wait_for(p.communicate(), 25)
            import json
            data = json.loads(raw or b"{}")
            tags = {}
            for sec in [data.get("format", {})] + data.get("programs", []) + data.get("streams", []):
                tags.update({k.lower(): v for k, v in (sec.get("tags") or {}).items()})
            name = tags.get("icy-name") or tags.get("service_name")
            if name:
                out["name"] = name.strip()
            for k in ("genre", "description"):
                v = tags.get("icy-" + k) or tags.get(k)
                if v and not out.get(k):
                    out[k] = v.strip()
            if tags.get("service_provider"):
                out.setdefault("description", tags["service_provider"])
        except Exception as e:
            out.setdefault("error", f"ffprobe: {type(e).__name__}: {e}")
    return out


async def refresh_station_info(stream: dict) -> None:
    """Run once when a recorder starts so every stream (HLS included) gets its station details."""
    try:
        info = await probe_station(stream["url"])
    except Exception:
        return
    if info.get("name") or info.get("genre") or info.get("description"):
        db.ex("UPDATE streams SET icy_name=COALESCE(?, icy_name), icy_genre=COALESCE(?, icy_genre), "
              "icy_description=COALESCE(?, icy_description), icy_url=COALESCE(?, icy_url) WHERE id=?",
              (info.get("name"), info.get("genre"), info.get("description"), info.get("url"), stream["id"]))


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
                    save_station_info(sid, r.headers)
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
                            await record_track(sid, artist, title, raw, source="icy")
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
            album, tags = None, []
            genre = (track.get("genres") or {}).get("primary")
            if genre:
                tags.append(genre)
            for sec in track.get("sections", []):
                for md in sec.get("metadata", []) or []:
                    if md.get("title") == "Album":
                        album = md.get("text")
                    elif md.get("title") == "Released" and md.get("text"):
                        tags.append(md["text"][:4])
                    elif md.get("title") == "Label" and md.get("text"):
                        tags.append(md["text"])
            await record_track(sid, artist, title, f"{artist} - {title}", art, album, captured_at,
                               tags=tags, source="shazam")
            STATUS[sid]["last_track"] = time.time()
            set_status(sid, label, "ok", f"Matched {artist} - {title} at {time.strftime('%H:%M:%S')}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            set_status(sid, label, "error", f"{type(e).__name__}: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
