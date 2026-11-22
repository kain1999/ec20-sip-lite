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
    def __init__(self, loop, bind_ip="0.0.0.0", on_dtmf=None, allocator=None):
        self.loop = loop
        self.bind_ip = bind_ip
        self.on_dtmf = on_dtmf
        self.allocator = allocator
        self.codec = "PCMU"
        self.rtp = RtpSession(loop, bind_ip, on_pcm=self._on_pcm,
                              on_dtmf=on_dtmf, codec="PCMU", pt=0)
        self._inbox = collections.deque(maxlen=JITTER_FRAMES)

    async def open(self):
        port = self.allocator.reserve() if self.allocator else None
        try:
            return await self.rtp.open(preferred_port=port)
        finally:
            if port is not None and self.port != port:
                self.allocator.release(port)

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
        if self.allocator:
            self.allocator.release(self.rtp.local_port)


class PortAllocator:
    """Hands out a fixed list of RTP ports (needed when running behind frp).

    Without a fixed list every call would pick an ephemeral port that the frp
    tunnel does not forward, so audio would be one-way.
    """

    def __init__(self, ports=None):
        self.all_ports = [int(p) for p in (ports or []) if str(p).strip().isdigit()]
        self._used = set()

    @property
    def enabled(self):
        return bool(self.all_ports)

    def reserve(self):
        if not self.all_ports:
            return None
        for port in self.all_ports:
            if port not in self._used:
                self._used.add(port)
                return port
        LOG.warning("all configured RTP ports are in use (%s); using ephemeral",
                    self.all_ports)
        return None

    def release(self, port):
        self._used.discard(int(port or 0))

    def status(self):
        return {"ports": self.all_ports, "in_use": sorted(self._used)}

    def stats(self):
        return {"rx": self.rtp.rx_packets, "tx": self.rtp.tx_packets,
                "remote": self.rtp.remote, "port": self.port, "codec": self.codec}


class _RawRelayProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner):
        self.owner = owner
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.owner._on_datagram(data, addr)


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


class RawRelayPort:
    """Video (or any non-G.711) RTP relay:转发原始包，不解析、不转码。

    Zero CPU per packet: the datagram is forwarded with its original RTP header,
    so H.264/VP8/whatever the phones negotiated passes through untouched. This
    is the only sane way to support video in a 14 MB Python process.
    """

    def __init__(self, loop, bind_ip="0.0.0.0", allocator=None):
        self.loop = loop
        self.bind_ip = bind_ip
        self.allocator = allocator
        self.transport = None
        self.port = 0
        self.peer = None
        self.packets_in = 0
        self.packets_out = 0
        self._protocol = None
        self._reserved = None

    async def open(self):
        self._reserved = self.allocator.reserve() if self.allocator else None
        try:
            self.transport, self._protocol = await self.loop.create_datagram_endpoint(
                lambda: _RawRelayProtocol(self),
                local_addr=(self.bind_ip, int(self._reserved or 0)))
        except OSError as exc:
            LOG.warning("video rtp port %s unavailable (%s)", self._reserved, exc)
            self._reserved = None
            self.transport, self._protocol = await self.loop.create_datagram_endpoint(
                lambda: _RawRelayProtocol(self), local_addr=(self.bind_ip, 0))
        self.port = self.transport.get_extra_info("sockname")[1]
        return self.port

    def set_peer(self, ip, port):
        if ip and port:
            self.peer = (ip, int(port))

    def _on_datagram(self, data, addr):
        self.packets_in += 1
        if self.peer is None:
            # symmetric RTP: learn from the first packet
            self.peer = addr
        if self.transport is None:
            return
        try:
            self.transport.sendto(data, self.peer)
            self.packets_out += 1
        except OSError:
            pass

    def close(self):
        if self.transport:
            try:
                self.transport.close()
            except Exception:
                pass
            self.transport = None
        if self.allocator and self.port:
            self.allocator.release(self.port)
