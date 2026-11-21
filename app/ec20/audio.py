"""USB audio (UAC) bridge for the EC20: ALSA capture/playback via arecord/aplay.

Using the alsa-utils binaries keeps the process small: no libasound bindings,
no C extension, ~4 MB RSS for the two tiny helpers. Frames are 20 ms of
8 kHz mono S16_LE (320 bytes).
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import time

LOG = logging.getLogger("ec20.audio")

FRAME_BYTES = 320  # 160 samples * 2 bytes


def detect_device(match="EC20"):
    """Return an ALSA device string (plughw:<card>,0) for the modem's UAC."""
    try:
        with open("/proc/asound/cards", "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    pattern = re.compile(r"^\s*(\d+)\s+\[([^\]]*)\]:.*$", re.M)
    for match_obj in pattern.finditer(text):
        card_index, card_name = match_obj.group(1), match_obj.group(2)
        if match.lower() in card_name.lower():
            return "plughw:%s,0" % card_index
    return ""


class AlsaAudio:
    def __init__(self, loop, device, rate=8000, channels=1, sample="S16_LE",
                 on_capture=None, enabled=True):
        self.loop = loop
        self.device = device
        self.rate = rate
        self.channels = channels
        self.sample = sample
        self.on_capture = on_capture or (lambda pcm: None)
        self.enabled = enabled
        self._rec_proc = None
        self._play_proc = None
        self._rec_task = None
        self._play_task = None
        self._queue = collections.deque(maxlen=6)
        self._running = False
        self.captured_frames = 0
        self.played_frames = 0
        self.dropped_frames = 0
        self.last_error = ""

    async def start(self):
        if not self.enabled:
            return False
        if not self.device:
            self.device = detect_device()
            if not self.device:
                self.last_error = "未找到 EC20 声卡"
                LOG.warning(self.last_error)
                return False
        self._running = True
        try:
            self._rec_proc = await asyncio.create_subprocess_exec(
                "arecord", "-q", "-D", self.device, "-f", self.sample, "-r", str(self.rate),
                "-c", str(self.channels), "-t", "raw",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except (OSError, FileNotFoundError) as exc:
            self.last_error = "arecord: %s" % exc
            LOG.warning(self.last_error)
            return False
        try:
            self._play_proc = await asyncio.create_subprocess_exec(
                "aplay", "-q", "-D", self.device, "-f", self.sample, "-r", str(self.rate),
                "-c", str(self.channels), "-t", "raw",
                stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except (OSError, FileNotFoundError) as exc:
            self.last_error = "aplay: %s" % exc
            LOG.warning(self.last_error)
            await self._stop_proc(self._rec_proc)
            self._rec_proc = None
            return False
        self._rec_task = self.loop.create_task(self._capture_loop())
        self._play_task = self.loop.create_task(self._playback_loop())
        LOG.info("USB audio started on %s (%d Hz)", self.device, self.rate)
        return True

    async def stop(self):
        self._running = False
        for task in (self._rec_task, self._play_task):
            if task:
                task.cancel()
        self._rec_task = None
        self._play_task = None
        self._queue.clear()
        for proc in (self._rec_proc, self._play_proc):
            await self._stop_proc(proc)
        self._rec_proc = None
        self._play_proc = None

    async def _stop_proc(self, proc):
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            await asyncio.wait_for(proc.wait(), 1.0)
        except (ProcessLookupError, asyncio.TimeoutError, BrokenPipeError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------- capture
    async def _capture_loop(self):
        proc = self._rec_proc
        if not proc or not proc.stdout:
            return
        try:
            while self._running:
                chunk = await proc.stdout.readexactly(FRAME_BYTES)
                self.captured_frames += 1
                try:
                    self.on_capture(chunk)
                except Exception:  # never let a bridge bug stop capture
                    LOG.exception("capture callback failed")
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception as exc:  # pragma: no cover
            self.last_error = str(exc)
            LOG.debug("capture ended: %s", exc)

    # ------------------------------------------------------------ playback
    def write(self, pcm):
        """Queue PCM coming from RTP for playback; drops oldest when backed up."""
        if not self._running or not pcm:
            return
        if len(self._queue) >= self._queue.maxlen:
            self._queue.popleft()
            self.dropped_frames += 1
        self._queue.append(bytes(pcm))

    async def _playback_loop(self):
        proc = self._play_proc
        if not proc or not proc.stdin:
            return
        interval = FRAME_BYTES / float(self.rate * 2)
        try:
            while self._running:
                if not self._queue:
                    await asyncio.sleep(interval)
                    continue
                frame = self._queue.popleft()
                proc.stdin.write(frame)
                self.played_frames += 1
                try:
                    await asyncio.wait_for(proc.stdin.drain(), 0.05)
                except asyncio.TimeoutError:
                    pass
        except (BrokenPipeError, asyncio.CancelledError):
            pass
        except Exception as exc:  # pragma: no cover
            self.last_error = str(exc)
            LOG.debug("playback ended: %s", exc)

    def stats(self):
        return {"device": self.device, "captured": self.captured_frames,
                "played": self.played_frames, "dropped": self.dropped_frames,
                "error": self.last_error}


class ToneAudio:
    """Development/simulator audio: silence in, 400 Hz tone out."""

    def __init__(self, loop, on_capture=None, enabled=True):
        self.loop = loop
        self.on_capture = on_capture or (lambda pcm: None)
        self.enabled = enabled
        self.device = "sim"
        self._task = None
        self._running = False
        self._phase = 0.0
        self.captured_frames = 0
        self.played_frames = 0
        self.last_error = ""

    async def start(self):
        self._running = True
        self._task = self.loop.create_task(self._generate())
        return True

    async def _generate(self):
        import math
        while self._running:
            await asyncio.sleep(0.02)
            if self.on_capture:
                frame = bytearray()
                for _ in range(160):
                    value = int(math.sin(self._phase) * 2000)
                    self._phase += 2 * math.pi * 440 / 8000
                    frame += value.to_bytes(2, "little", signed=True)
                try:
                    self.on_capture(bytes(frame))
                except Exception:
                    pass
            self.captured_frames += 1

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    def write(self, pcm):
        self.played_frames += 1

    def stats(self):
        return {"device": "sim", "captured": self.captured_frames,
                "played": self.played_frames, "dropped": 0, "error": ""}