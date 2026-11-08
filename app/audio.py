"""Recording file layout, day listings, waveform peaks and retention."""
import array
import asyncio
import datetime as dt
import json
import os
import re
import shutil
import time

from mutagen.mp3 import MP3

from . import db

FILE_RE = re.compile(r"^(\d{8})-(\d{6})\.mp3$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PEAKS_PER_SECOND = 20  # waveform resolution sent to the browser (enough for deep zoom)


def root() -> str:
    return db.get_settings()["recordings_path"]


def stream_dir(slug: str) -> str:
    return os.path.join(root(), slug)


def day_dir(slug: str, day: dt.date) -> str:
    return os.path.join(stream_dir(slug), f"{day:%Y}", f"{day:%m}", f"{day:%d}")


def segment_pattern(slug: str) -> str:
    """ffmpeg strftime pattern for chunk files."""
    return os.path.join(stream_dir(slug), "%Y", "%m", "%d", "%Y%m%d-%H%M%S.mp3")


def parse_date(s: str) -> dt.date:
    if not DATE_RE.match(s or ""):
        raise ValueError("bad date")
    return dt.date.fromisoformat(s)


def file_start(name: str) -> float | None:
    m = FILE_RE.match(name)
    if not m:
        return None
    return dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").timestamp()  # local TZ


def chunk_path(slug: str, date_str: str, name: str) -> str:
    day = parse_date(date_str)
    if not FILE_RE.match(name):
        raise ValueError("bad file")
    return os.path.join(day_dir(slug, day), name)


def duration(path: str) -> float:
    try:
        return float(MP3(path).info.length)
    except Exception:
        return 0.0


def is_hls(url: str) -> bool:
    return ".m3u8" in (url or "").lower()


# ---- recorder runs --------------------------------------------------------------------
# Each ffmpeg launch is logged so the day listing knows where one continuous recording
# ends and the next begins (a restart re-fetches buffered HLS audio, so its first chunk
# overlaps the previous run).

def _runs_path(slug: str) -> str:
    return os.path.join(stream_dir(slug), "runs.log")


def note_run_start(slug: str, ts: float | None = None) -> None:
    try:
        os.makedirs(stream_dir(slug), exist_ok=True)
        with open(_runs_path(slug), "a") as f:
            f.write(f"{ts or time.time():.3f}\n")
    except OSError:
        pass


def run_starts(slug: str) -> list[float]:
    try:
        with open(_runs_path(slug)) as f:
            return sorted(float(x) for x in f.read().split() if x.strip())
    except (OSError, ValueError):
        return []


def prune_runs(slug: str, cutoff: float) -> None:
    runs = run_starts(slug)
    keep = [r for r in runs if r >= cutoff]
    if len(keep) == len(runs):
        return
    try:
        with open(_runs_path(slug), "w") as f:
            f.writelines(f"{r:.3f}\n" for r in keep)
    except OSError:
        pass


# File names record the wall clock when ffmpeg opened each chunk. With HLS the audio arrives
# in segment-sized bursts, so that moment can land several seconds either side of where the
# audio really belongs, and a restart's first chunk also carries ~30 s of buffered audio from
# before its name. Within one continuous run the audio has no holes, so chunks are placed end
# to end from their real lengths, and the run as a whole is anchored to its file names.
HLS_SLACK = 15.0   # largest name jitter treated as the same continuous recording (HLS)
ICY_SLACK = 2.0    # Icecast/HTTP names are accurate; a bigger hole is real lost audio


def place_chunks(chunks: list[dict], runs: list[float], slack: float) -> list[dict]:
    """Set each chunk's "start" to where its audio really sits; "named" keeps the file time."""
    groups: list[list[dict]] = []
    for c in chunks:
        c["named"] = c["start"]
    prev = None
    for i, c in enumerate(chunks):
        nxt = chunks[i + 1] if i + 1 < len(chunks) else None
        # a run starts at a logged ffmpeg launch, or (older recordings) at a chunk whose buffered
        # audio runs well past the next chunk's start
        restarted = prev is not None and (
            any(prev["named"] < r <= c["named"] + 1 for r in runs)
            or (nxt is not None and c["named"] + (c["duration"] or 0) - nxt["named"] > slack))
        hole = prev is not None and c["named"] - (prev["named"] + prev["duration"]) > slack
        if prev is None or restarted or hole:
            groups.append([c])
        else:
            groups[-1].append(c)
        prev = c
    for g in groups:
        pos, offs = 0.0, []
        for c in g:
            c["_pos"] = pos
            offs.append(c["named"] - g[0]["named"] - pos)
            pos += c["duration"] or 0
        # the first chunk's name is skewed by buffered audio, so anchor on the rest
        offs = sorted(offs[1:]) if len(offs) > 1 else offs
        shift = offs[len(offs) // 2]
        for c in g:
            c["start"] = round(g[0]["named"] + shift + c.pop("_pos"), 3)
    return chunks


def list_day(slug: str, day: dt.date, hls: bool = False) -> list[dict]:
    d = day_dir(slug, day)
    if not os.path.isdir(d):
        return []
    names = sorted(n for n in os.listdir(d) if FILE_RE.match(n))
    out = []
    now = time.time()
    for i, n in enumerate(names):
        p = os.path.join(d, n)
        st = os.stat(p)
        start = file_start(n)
        in_progress = i == len(names) - 1 and now - st.st_mtime < 15
        out.append({
            "file": n,
            "start": start,
            "duration": duration(p),
            "size": st.st_size,
            "in_progress": in_progress,
        })
    return place_chunks(out, run_starts(slug), HLS_SLACK if hls else ICY_SLACK)


def available_days(slug: str) -> list[str]:
    base = stream_dir(slug)
    days = []
    if not os.path.isdir(base):
        return days
    for y in sorted(os.listdir(base)):
        yp = os.path.join(base, y)
        if not (y.isdigit() and os.path.isdir(yp)):
            continue
        for m in sorted(os.listdir(yp)):
            mp = os.path.join(yp, m)
            if not (m.isdigit() and os.path.isdir(mp)):
                continue
            for d in sorted(os.listdir(mp)):
                dp = os.path.join(mp, d)
                if d.isdigit() and os.path.isdir(dp) and any(FILE_RE.match(n) for n in os.listdir(dp)):
                    days.append(f"{y}-{m}-{d}")
    return days


async def compute_peaks(path: str, cache: bool) -> dict:
    cache_path = path[:-4] + ".peaks.json"
    if os.path.exists(cache_path) and os.path.getmtime(cache_path) >= os.path.getmtime(path):
        try:
            with open(cache_path) as f:
                cached = json.load(f)
            if cached.get("per_second") == PEAKS_PER_SECOND:
                return cached
        except (OSError, ValueError):
            pass
    rate = 8000  # keep enough bandwidth that the envelope reflects speech and music
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(rate), "-f", "s16le", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    raw, _ = await proc.communicate()
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % 2])
    step = max(1, rate // PEAKS_PER_SECOND)
    peaks = []
    for i in range(0, len(samples), step):
        seg = samples[i:i + step]
        peaks.append(round(max(max(seg), -min(seg)) / 32768, 3) if seg else 0)
    if len(peaks) > 0:  # keep the JSON compact
        peaks = [0 if v < 0.001 else v for v in peaks]
    data = {"per_second": PEAKS_PER_SECOND, "peaks": peaks}
    if cache:
        try:
            with open(cache_path, "w") as f:
                json.dump(data, f)
        except OSError:
            pass
    return data


def ensure_day_dirs(slug: str) -> None:
    """ffmpeg's segment muxer won't create folders, so pre-create today and tomorrow."""
    today = dt.date.today()
    for day in (today, today + dt.timedelta(days=1)):
        os.makedirs(day_dir(slug, day), exist_ok=True)


def apply_retention() -> int:
    days = db.setting_int("retention_days")
    cutoff = time.time() - days * 86400
    removed = 0
    base = root()
    if not os.path.isdir(base):
        return 0
    for dirpath, _dirs, files in os.walk(base, topdown=False):
        for n in files:
            m = re.match(r"^(\d{8}-\d{6})\.(mp3|peaks\.json)$", n)
            if not m:
                continue
            ts = dt.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").timestamp()
            if ts < cutoff:
                try:
                    os.remove(os.path.join(dirpath, n))
                    removed += n.endswith(".mp3")
                except OSError:
                    pass
        if dirpath != base and not os.listdir(dirpath):
            # only prune empty date folders older than today
            try:
                rel = os.path.relpath(dirpath, base).split(os.sep)
                if len(rel) == 4 and dt.date(int(rel[1]), int(rel[2]), int(rel[3])) < dt.date.today():
                    os.rmdir(dirpath)
                elif len(rel) in (2, 3):
                    os.rmdir(dirpath)
            except (OSError, ValueError):
                pass
    for slug in os.listdir(base):
        if os.path.isdir(os.path.join(base, slug)):
            prune_runs(slug, cutoff)
    db.ex("DELETE FROM tracks WHERE ts < ?", (cutoff,))
    try:
        from . import transcribe
        transcribe.purge_before(cutoff)
    except Exception:
        pass
    return removed


def disk_usage() -> dict:
    base = root()
    try:
        u = shutil.disk_usage(base)
        used_by_logger = 0
        for dirpath, _d, files in os.walk(base):
            for n in files:
                try:
                    used_by_logger += os.path.getsize(os.path.join(dirpath, n))
                except OSError:
                    pass
        return {"total": u.total, "free": u.free, "logger": used_by_logger}
    except OSError:
        return {"total": 0, "free": 0, "logger": 0}


CLIP_MAX_SECONDS = 4 * 3600  # longest clip a user can cut in one go


def clip_parts(slug: str, start: float, end: float, hls: bool = False) -> list[tuple[str, float, float]]:
    """Chunks covering [start, end) as (path, inpoint, outpoint) in seconds within each file."""
    found = []
    day = dt.date.fromtimestamp(start) - dt.timedelta(days=1)  # a chunk can begin before midnight
    last = dt.date.fromtimestamp(end)
    while day <= last:
        for c in list_day(slug, day, hls):
            c0, c1 = c["start"], c["start"] + (c["duration"] or 0)
            if c1 <= start or c0 >= end or c1 <= c0:
                continue
            found.append((c0, c1, os.path.join(day_dir(slug, day), c["file"])))
        day += dt.timedelta(days=1)
    found.sort()
    # after a restart the first chunk repeats audio the previous run already has: skip it
    parts, done = [], start
    for c0, c1, path in found:
        a, b = max(done, c0), min(c1, end)
        if b - a > 0.05:
            parts.append((path, a - c0, b - c0))
            done = b
    return parts


async def cut_clip(parts: list[tuple[str, float, float]], out_path: str, bitrate_kbps: int) -> None:
    """Join the chunk pieces with ffmpeg's concat demuxer and re-encode to one MP3."""
    list_path = out_path + ".txt"
    with open(list_path, "w") as f:
        for path, a, b in parts:
            safe = path.replace("'", "'\\''")
            f.write(f"file '{safe}'\ninpoint {a:.3f}\noutpoint {b:.3f}\n")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", list_path, "-vn", "-map", "0:a:0",
            "-c:a", "libmp3lame", "-b:a", f"{int(bitrate_kbps)}k", "-f", "mp3", out_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0 or not os.path.getsize(out_path):
            raise RuntimeError((err or b"").decode(errors="replace")[-300:] or "ffmpeg failed")
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass
