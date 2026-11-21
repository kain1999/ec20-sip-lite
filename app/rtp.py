"""RTP endpoint: send/parse RTP, 20 ms G.711 framing, RFC 2833 DTMF."""
from __future__ import annotations

import asyncio
import logging
import random
import struct

from . import g711

LOG = logging.getLogger("rtp")

FRAME = 160  # samples per 20 ms at 8 kHz
RTP_FIXED = 12


def build_rtp(payload_type, seq, timestamp, ssrc, payload, marker=False):
    header = struct.pack(
        "!BBHII", 0x80, (0x80 if marker else 0) | (payload_type & 0x7F),
        seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF,
    )
    return header + payload


class RtpProtocol(asyncio.DatagramProtocol):
    def __init__(self, session):
        self.session = session
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.session._on_datagram(data, addr)

    def error_received(self, exc):  # pragma: no cover
        LOG.debug("rtp error: %s", exc)


class RtpSession:
    """One RTP socket. Created per SIP leg; media is pushed by the 20 ms clock."""

    def __init__(self, loop, bind_ip="0.0.0.0", on_pcm=None, on_dtmf=None, codec="PCMU", pt=None):
        self.loop = loop
        self.bind_ip = bind_ip
        self.on_pcm = on_pcm
        self.on_dtmf = on_dtmf
        self.codec = codec
        self.payload_type = pt if pt is not None else (0 if codec == "PCMU" else 8)
        self.ssrc = random.getrandbits(32)
        self.seq = random.getrandbits(16)
        self.timestamp = random.getrandbits(32)
        self.local_port = 0
        self.remote = None  # (ip, port)
        self.transport = None
        self._protocol = None
        self.rx_packets = 0
        self.tx_packets = 0

    # ---------------------------------------------------------------- life
    async def open(self, preferred_port=None):
        if preferred_port:
            try:
                self.transport, self._protocol = await self.loop.create_datagram_endpoint(
                    lambda: RtpProtocol(self), local_addr=(self.bind_ip, int(preferred_port))
                )
                self.local_port = self.transport.get_extra_info("sockname")[1]
                LOG.debug("rtp bound to configured port %d", self.local_port)
                return self.local_port
            except OSError as exc:
                LOG.warning("rtp port %s unavailable (%s), falling back to ephemeral",
                            preferred_port, exc)
        self.transport, self._protocol = await self.loop.create_datagram_endpoint(
            lambda: RtpProtocol(self), local_addr=(self.bind_ip, 0)
        )
        self.local_port = self.transport.get_extra_info("sockname")[1]
        LOG.debug("rtp listening on %d", self.local_port)
        return self.local_port

    def set_remote(self, ip, port):
        self.remote = (ip, int(port))
        # learn symmetric RTP: send a keepalive so NAT opens
        self.transport.sendto(b"", self.remote) if self.transport else None

    def close(self):
        if self.transport:
            try:
                self.transport.close()
            except Exception:  # pragma: no cover
                pass
        self.transport = None

    # ----------------------------------------------------------------- rx
    def _on_datagram(self, data, addr):
        if len(data) < RTP_FIXED:
            return
        if self.remote is None:
            self.remote = addr
        self.rx_packets += 1
        b0, b1, seq, ts, ssrc = struct.unpack("!BBHII", data[:RTP_FIXED])
        if (b0 >> 6) != 2:
            return
        cc = b0 & 0x0F
        offset = RTP_FIXED + 4 * cc
        if b0 & 0x10:
            payload_len = struct.unpack("!H", data[offset + 2:offset + 4])[0]
            offset += 4 + 4 * (payload_len & 0xFFFF)
        pt = b1 & 0x7F
        payload = data[offset:]
        if pt >= 96 and self.on_dtmf is not None and len(payload) >= 4:
            event = payload[0]
            end = payload[1] & 0x80
            self.on_dtmf(event, bool(end))
            return
        if self.on_pcm is not None and payload:
            self.on_pcm(payload, pt)

    # ----------------------------------------------------------------- tx
    def send_pcm(self, pcm, marker=False):
        """pcm: little-endian S16 mono at 8 kHz; length should be a multiple of 320 B."""
        if not self.transport or not self.remote:
            return
        for start in range(0, len(pcm), FRAME * 2):
            chunk = pcm[start:start + FRAME * 2]
            if len(chunk) < 2:
                continue
            payload = g711.encode(self.codec, chunk)
            packet = build_rtp(self.payload_type, self.seq, self.timestamp, self.ssrc, payload, marker)
            try:
                self.transport.sendto(packet, self.remote)
            except OSError:  # pragma: no cover
                return
            self.seq = (self.seq + 1) & 0xFFFF
            self.timestamp = (self.timestamp + len(chunk) // 2) & 0xFFFFFFFF
            self.tx_packets += 1
            marker = False

    def send_dtmf(self, digit, duration_ms=160):
        """RFC 2833 telephone-event packet."""
        if not self.transport or not self.remote:
            return
        events = {"0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
                  "*": 10, "#": 11, "A": 12, "B": 13, "C": 14, "D": 15}
        if digit not in events:
            return
        event = events[digit]
        samples = duration_ms * 8
        payload = struct.pack("!BBH", event, 0x0A, samples)
        packet = build_rtp(101, self.seq, self.timestamp, self.ssrc, payload, marker=True)
        try:
            self.transport.sendto(packet, self.remote)
        except OSError:  # pragma: no cover
            return
        self.seq = (self.seq + 1) & 0xFFFF
        self.timestamp = (self.timestamp + samples) & 0xFFFFFFFF
        end = struct.pack("!BBH", event, 0x8A, samples)
        packet = build_rtp(101, self.seq, self.timestamp, self.ssrc, end)
        try:
            self.transport.sendto(packet, self.remote)
        except OSError:  # pragma: no cover
            return
        self.seq = (self.seq + 1) & 0xFFFF
        self.timestamp = (self.timestamp + samples) & 0xFFFFFFFF