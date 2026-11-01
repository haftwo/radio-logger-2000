"""Supervises one ffmpeg recorder (plus optional metadata watcher) per enabled stream."""
import asyncio
import logging
import os
import time
from collections import deque

from . import audio, db, metadata

log = logging.getLogger("radiologger.recorder")


class StreamWorker:
    def __init__(self, stream: dict):
        self.stream = stream
        self.state = "starting"
        self.last_error = ""
        self.started_at: float | None = None
        self.restarts = 0
        self.log = deque(maxlen=20)
        self.proc: asyncio.subprocess.Process | None = None
        self.tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self.tasks.append(asyncio.create_task(self._record_loop()))
        if self.stream["is_music"]:
            src = self.stream["meta_source"]
            if src == "icy":
                self.tasks.append(asyncio.create_task(metadata.icy_loop(self.stream)))
            elif src == "shazam":
                self.tasks.append(asyncio.create_task(metadata.shazam_loop(self.stream)))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), 5)
            except (ProcessLookupError, asyncio.TimeoutError):
                self.proc.kill()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.state = "stopped"

    def _cmd(self) -> list[str]:
        s = db.get_settings()
        chunk = int(s["chunk_minutes"]) * 60
        url = self.stream["url"]
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning"]
        if url.startswith("http") and ".m3u8" not in url:
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_at_eof", "1",
                    "-reconnect_delay_max", "30"]
        cmd += ["-user_agent", metadata.UA, "-i", url, "-vn", "-map", "0:a:0",
                "-c:a", "libmp3lame", "-b:a", f"{int(s['bitrate_kbps'])}k",
                "-f", "segment", "-segment_time", str(chunk), "-segment_atclocktime", "1",
                "-reset_timestamps", "1", "-strftime", "1",
                audio.segment_pattern(self.stream["slug"])]
        return cmd

    async def _record_loop(self) -> None:
        backoff = 5
        while True:
            try:
                audio.ensure_day_dirs(self.stream["slug"])
                self.proc = await asyncio.create_subprocess_exec(
                    *self._cmd(), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                self.state, self.started_at = "recording", time.time()
                log.info("[%s] recording started", self.stream["name"])
                async for line in self.proc.stderr:
                    msg = line.decode(errors="replace").strip()
                    if msg:
                        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
                rc = await self.proc.wait()
                ran_for = time.time() - (self.started_at or time.time())
                self.state = "error"
                self.last_error = self.log[-1] if self.log else f"ffmpeg exited ({rc})"
                log.warning("[%s] ffmpeg exited rc=%s: %s", self.stream["name"], rc, self.last_error)
                if ran_for > 120:
                    backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.state, self.last_error = "error", str(e)
                log.exception("[%s] recorder failure", self.stream["name"])
            self.restarts += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    def status(self) -> dict:
        return {"state": self.state, "last_error": self.last_error, "started_at": self.started_at,
                "restarts": self.restarts, "log": list(self.log)}


class Manager:
    def __init__(self):
        self.workers: dict[int, StreamWorker] = {}
        self.housekeeping: asyncio.Task | None = None

    async def start(self) -> None:
        for s in db.streams(enabled_only=True):
            self._spawn(s)
        self.housekeeping = asyncio.create_task(self._housekeeping())

    def _spawn(self, s: dict) -> None:
        w = StreamWorker(s)
        self.workers[s["id"]] = w
        w.start()

    async def restart_stream(self, stream_id: int) -> None:
        w = self.workers.pop(stream_id, None)
        if w:
            await w.stop()
        s = db.stream(stream_id)
        if s and s["enabled"]:
            self._spawn(s)

    async def restart_all(self) -> None:
        for sid in list(self.workers):
            w = self.workers.pop(sid)
            await w.stop()
        for s in db.streams(enabled_only=True):
            self._spawn(s)

    async def stop(self) -> None:
        if self.housekeeping:
            self.housekeeping.cancel()
        for w in self.workers.values():
            await w.stop()

    def status(self, stream_id: int) -> dict:
        w = self.workers.get(stream_id)
        return w.status() if w else {"state": "stopped", "last_error": "", "started_at": None,
                                     "restarts": 0, "log": []}

    async def _housekeeping(self) -> None:
        last_retention = 0.0
        while True:
            try:
                for w in self.workers.values():
                    audio.ensure_day_dirs(w.stream["slug"])
                if time.time() - last_retention > 3600:
                    removed = await asyncio.to_thread(audio.apply_retention)
                    last_retention = time.time()
                    if removed:
                        log.info("retention removed %s chunk(s)", removed)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("housekeeping failed")
            await asyncio.sleep(60)


manager = Manager()


def check_writable(path: str) -> str | None:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".rl-write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return None
    except OSError as e:
        return str(e)
