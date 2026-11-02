"""Supervises one ffmpeg recorder (plus optional metadata watcher) per enabled stream."""
import asyncio
import logging
import os
import re
import time
from collections import deque

from . import audio, db, metadata, outages, transcribe

log = logging.getLogger("radiologger.recorder")

DETECT_SECONDS = 2      # silencedetect's own minimum; the outage threshold is applied in _check()
RECOVER_SECONDS = 5     # audio must be back this long before an outage is closed (stops flapping)
SILENCE_RE = re.compile(r"silence_(start|end):\s*(-?[\d.]+)")
LEVEL_RE = re.compile(r"\[(warning|error|fatal|panic)\]\s*")


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
        # outage detection
        self.born = time.time()
        self.last_audio: float | None = None     # wall time the encoder last moved forward
        self.silence_since: float | None = None  # set while silencedetect reports silence
        self.clear_since: float | None = None
        self.outage_id: int | None = None
        self.health, self.health_since = "starting", self.born

    def start(self) -> None:
        self.tasks.append(asyncio.create_task(self._record_loop()))
        self.tasks.append(asyncio.create_task(self._monitor()))
        self.tasks.append(asyncio.create_task(metadata.refresh_station_info(self.stream)))
        if self.stream["is_music"]:
            if self.stream["meta_source"] in ("icy", "shazam"):
                self.tasks.append(asyncio.create_task(metadata.detect_loop(self.stream)))

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
        # info level so silencedetect is heard; _read_log keeps only warnings and errors
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "level+info"]
        if url.startswith("http") and ".m3u8" not in url:
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_at_eof", "1",
                    "-reconnect_delay_max", "30"]
        try:
            noise = max(-90, min(-10, int(outages.cfg()["silence_db"])))
        except ValueError:
            noise = -50
        cmd += ["-user_agent", metadata.UA, "-i", url, "-vn", "-map", "0:a:0",
                "-af", f"silencedetect=noise={noise}dB:d={DETECT_SECONDS}",
                "-progress", "pipe:1", "-c:a", "libmp3lame", "-b:a", f"{int(s['bitrate_kbps'])}k",
                "-f", "segment", "-segment_time", str(chunk), "-segment_atclocktime", "1",
                "-reset_timestamps", "1", "-strftime", "1",
                audio.segment_pattern(self.stream["slug"])]
        return cmd

    async def _record_loop(self) -> None:
        backoff = 5
        while True:
            try:
                audio.ensure_day_dirs(self.stream["slug"])
                self.silence_since = None
                self.proc = await asyncio.create_subprocess_exec(
                    *self._cmd(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                self.state, self.started_at = "recording", time.time()
                log.info("[%s] recording started", self.stream["name"])
                await asyncio.gather(self._read_log(self.proc.stderr), self._read_progress(self.proc.stdout))
                rc = await self.proc.wait()
                self.silence_since = None
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

    async def _read_log(self, stream) -> None:
        async for line in stream:
            msg = line.decode(errors="replace").strip()
            if not msg:
                continue
            m = SILENCE_RE.search(msg)
            if m:
                if m.group(1) == "start":   # reported once the silence has lasted DETECT_SECONDS
                    self.silence_since = time.time() - DETECT_SECONDS
                else:
                    self.silence_since = None
                continue
            lv = LEVEL_RE.search(msg)
            if lv:  # drop ffmpeg's [context @ 0x..] prefix and level tag, keep the message
                self.log.append(f"{time.strftime('%H:%M:%S')} {msg[lv.end():]}")

    async def _read_progress(self, stream) -> None:
        """ffmpeg -progress writes out_time_us every ~0.5 s while audio is flowing through the encoder."""
        last = 0
        async for line in stream:
            if line.startswith(b"out_time_us="):
                try:
                    v = int(line[12:])
                except ValueError:   # "N/A" before the first frame
                    continue
                if v > last:
                    last, self.last_audio = v, time.time()

    # ---- outage detection ------------------------------------------------------------

    async def _monitor(self) -> None:
        o = outages.open_for(self.stream["id"])  # adopt one left open by a restart or crash
        if o:
            self.outage_id = o["id"]
        while True:
            try:
                self._check(time.time())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[%s] outage check failed", self.stream["name"])
            await asyncio.sleep(2)

    def _check(self, now: float) -> None:
        limit = outages.threshold()
        running = self.proc is not None and self.proc.returncode is None
        ref = self.last_audio or self.born
        gap = now - ref   # keeps growing while ffmpeg is down or backing off between retries
        cause = since = None
        if gap >= limit:
            cause, since = "no_audio", ref
        elif self.silence_since and now - self.silence_since >= limit:
            cause, since = "silence", self.silence_since

        # HLS arrives in 6-10 s bursts, so allow a little slack before calling it "not flowing"
        flowing = running and self.last_audio is not None and gap < 15 and not self.silence_since
        health = ("down" if cause == "no_audio" else "silent" if cause == "silence"
                  else "quiet" if self.silence_since else "healthy" if flowing else "starting")
        if health != self.health:
            self.health, self.health_since = health, now
        if cause:  # "silent for…" counts from when the audio actually stopped
            self.health_since = since

        if cause:
            self.clear_since = None
            if not self.outage_id:
                self.outage_id = outages.open_outage(self.stream, cause, since)
            outages.maybe_alert_start(self.outage_id)
        elif self.outage_id and flowing:
            self.clear_since = self.clear_since or now
            if now - self.clear_since >= RECOVER_SECONDS:
                o = outages.close_outage(self.outage_id, self.clear_since)
                self.outage_id, self.clear_since = None, None
                outages.alert_end(o)
        else:
            self.clear_since = None

    def health_status(self, now: float | None = None) -> dict:
        now = now or time.time()
        return {"health": self.health, "health_since": self.health_since,
                "quiet_for": round(now - self.silence_since) if self.silence_since else 0,
                "last_audio": self.last_audio, "outage_id": self.outage_id}

    def status(self) -> dict:
        return {"state": self.state, "last_error": self.last_error, "started_at": self.started_at,
                "restarts": self.restarts, "log": list(self.log), **self.health_status()}


class Manager:
    def __init__(self):
        self.workers: dict[int, StreamWorker] = {}
        self.housekeeping: asyncio.Task | None = None

    async def start(self) -> None:
        for s in db.streams(enabled_only=True):
            self._spawn(s)
        self.housekeeping = asyncio.create_task(self._housekeeping())
        self.transcriber = asyncio.create_task(transcribe.worker())

    def _spawn(self, s: dict) -> None:
        w = StreamWorker(s)
        self.workers[s["id"]] = w
        w.start()

    async def restart_stream(self, stream_id: int) -> None:
        metadata.STATUS.pop(stream_id, None)
        w = self.workers.pop(stream_id, None)
        if w:
            await w.stop()
        s = db.stream(stream_id)
        if s and s["enabled"]:
            self._spawn(s)
        else:  # disabled or deleted: nothing is watching it any more
            outages.close_all_for(stream_id)

    async def restart_all(self) -> None:
        for sid in list(self.workers):
            w = self.workers.pop(sid)
            await w.stop()
        for s in db.streams(enabled_only=True):
            self._spawn(s)

    async def stop(self) -> None:
        if self.housekeeping:
            self.housekeeping.cancel()
        if getattr(self, "transcriber", None):
            self.transcriber.cancel()
        for w in self.workers.values():
            await w.stop()

    def status(self, stream_id: int) -> dict:
        w = self.workers.get(stream_id)
        st = w.status() if w else {"state": "stopped", "last_error": "", "started_at": None,
                                   "restarts": 0, "log": [], "health": "disabled", "health_since": None,
                                   "quiet_for": 0, "last_audio": None, "outage_id": None}
        st["detect"] = metadata.STATUS.get(stream_id) if w and w.stream["is_music"] else None
        return st

    async def _housekeeping(self) -> None:
        last_retention = 0.0
        while True:
            try:
                for w in self.workers.values():
                    audio.ensure_day_dirs(w.stream["slug"])
                if time.time() - last_retention > 3600:
                    removed = await asyncio.to_thread(audio.apply_retention)
                    await asyncio.to_thread(outages.prune)
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
