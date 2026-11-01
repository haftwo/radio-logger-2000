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


def list_day(slug: str, day: dt.date) -> list[dict]:
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
    return out


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
    db.ex("DELETE FROM tracks WHERE ts < ?", (cutoff,))
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
