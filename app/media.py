"""Media plane: RTP ports, a modem audio port and the 20 ms bridge clock."""
from __future__ import annotations

import asyncio
import collections
import logging
import time

from . import g711
from .rtp import RtpSession

LOG = logging.getLogger("media")
FRAME_BYTES = 320          # 20 ms of 8 kHz mono S16_LE
TICK = 0.02
JITTER_FRAMES = 6          # 120 ms cap: enough for Wi-Fi jitter, tiny in RAM


class RtpPort:
    def __init__(self, loop, bind_ip="0.0.0.0", on_dtmf=None):
        self.loop = loop
        self.bind_ip = bind_ip
        self.on_dtmf = on_dtmf
        self.codec = "PCMU"
        self.rtp = RtpSession(loop, bind_ip, on_pcm=self._on_pcm,
                              on_dtmf=on_dtmf, codec="PCMU", pt=0)
        self._inbox = collections.deque(maxlen=JITTER_FRAMES)

    async def open(self):
        return await self.rtp.open()

    @property
    def port(self):
        return self.rtp.local_port

    def set_remote(self, ip, sdp_port, codec="PCMU"):
        if codec in ("PCMU", "PCMA"):
            self.codec = codec
            self.rtp.codec = codec
            self.rtp.payload_type = 0 if codec == "PCMU" else 8
        self.rtp.set_remote(ip, sdp_port)

    def _on_pcm(self, payload, payload_type):
        if payload_type in (0, 8):
            self.codec = "PCMU" if payload_type == 0 else "PCMA"
        pcm = g711.decode(self.codec, payload)
        if len(self._inbox) >= self._inbox.maxlen:
            self._inbox.popleft()
        self._inbox.append(pcm)

    def read_pcm(self):
        if self._inbox:
            return self._inbox.popleft()
        return None

    def write_pcm(self, pcm):
        self.rtp.send_pcm(pcm)

    def send_dtmf(self, digit):
        self.rtp.send_dtmf(digit)

    def close(self):
        self.rtp.close()

    def stats(self):
        return {"rx": self.rtp.rx_packets, "tx": self.rtp.tx_packets,
                "remote": self.rtp.remote, "port": self.port, "codec": self.codec}


class ModemPort:
    """Bridges the EC20 USB-audio session into the media plane."""

    def __init__(self, loop, audio):
        self.loop = loop
        self.audio = audio
        self._inbox = collections.deque(maxlen=JITTER_FRAMES)
        audio.on_capture = self._on_capture

    def _on_capture(self, pcm):
        if len(self._inbox) >= self._inbox.maxlen:
            self._inbox.popleft()
        self._inbox.append(pcm)

    async def start(self):
        return await self.audio.start()

    async def stop(self):
        await self.audio.stop()

    def read_pcm(self):
        if self._inbox:
            return self._inbox.popleft()
        return None

    def write_pcm(self, pcm):
        self.audio.write(pcm)

    def stats(self):
        return self.audio.stats()


SILENCE = b"\x00\x00" * 160


class Bridge:
    """Two-way 20 ms PCM forwarding between two ports."""

    def __init__(self, loop, port_a, port_b):
        self.loop = loop
        self.a = port_a
        self.b = port_b
        self._task = None
        self.running = False
        self.frames = 0

    def start(self):
        self.running = True
        self._task = self.loop.create_task(self._run())
        return self

    async def _run(self):
        next_tick = time.monotonic()
        while self.running:
            frame_a = self.a.read_pcm() or SILENCE
            frame_b = self.b.read_pcm() or SILENCE
            try:
                self.b.write_pcm(frame_a)
                self.a.write_pcm(frame_b)
            except Exception:  # pragma: no cover
                LOG.exception("bridge write failed")
            self.frames += 1
            next_tick += TICK
            delay = next_tick - time.monotonic()
            if delay < -0.2:            # fell behind badly: resync instead of spinning
                next_tick = time.monotonic()
                delay = 0
            await asyncio.sleep(delay if delay > 0 else 0)

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None