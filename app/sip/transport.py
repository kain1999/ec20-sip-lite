"""Transport peers for SIP: one abstraction over UDP datagrams and TCP streams.

SIP over TCP keeps a connection alive, so the server can push requests (INVITE,
BYE) towards a client that registered from behind NAT/frp. That is the main
reason this project speaks TCP: frp `type="udp"` tunnels are one-directional
(only the client may open a flow), while a TCP tunnel is a real socket.
"""
from __future__ import annotations

import asyncio
import logging

from . import message as sipmsg

LOG = logging.getLogger("sip.transport")

MAX_HEAD_BYTES = 65536


class Peer:
    """Something we can send a serialized SIP message to."""

    kind = "udp"

    def __init__(self, ip, port):
        self.ip = ip or ""
        self.port = int(port or 0)
        self.closed = False

    @property
    def addr(self):
        return (self.ip, self.port)

    def send(self, data):  # pragma: no cover - interface
        raise NotImplementedError

    def close(self):
        self.closed = True

    def __str__(self):
        return "%s:%d" % (self.ip, self.port)


class UdpPeer(Peer):
    kind = "udp"

    def __init__(self, transport, addr):
        super().__init__(addr[0], addr[1])
        self.transport = transport

    def send(self, data):
        if self.closed or self.transport is None:
            return
        try:
            self.transport.sendto(data, self.addr)
        except OSError as exc:  # pragma: no cover
            LOG.debug("udp send to %s failed: %s", self, exc)


class TcpPeer(Peer):
    kind = "tcp"

    def __init__(self, reader, writer, server=None):
        peer = writer.get_extra_info("peername") or ("0.0.0.0", 0)
        super().__init__(peer[0], peer[1])
        self.reader = reader
        self.writer = writer
        self.server = server
        self.user = ""          # authentication identity seen on this connection

    def send(self, data):
        if self.closed or self.writer is None:
            return
        try:
            self.writer.write(data)
        except (ConnectionResetError, BrokenPipeError, RuntimeError) as exc:
            LOG.debug("tcp send to %s failed: %s", self, exc)
            self.closed = True

    def close(self):
        self.closed = True
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:  # pragma: no cover
                pass


class TcpListener:
    """Accepts SIP/TCP connections and turns the byte stream into messages."""

    def __init__(self, server, bind, port):
        self.server = server
        self.bind = bind
        self.port = int(port)
        self._srv = None

    async def start(self):
        self._srv = await asyncio.start_server(self._handle, self.bind, self.port)
        LOG.info("SIP/TCP listening on %s:%d", self.bind, self.port)
        return self

    async def stop(self):
        if self._srv:
            self._srv.close()
            await self._srv.wait_closed()
            self._srv = None

    async def _handle(self, reader, writer):
        peer = TcpPeer(reader, writer, self.server)
        if self.server:
            self.server.register_peer(peer)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                if len(head) > MAX_HEAD_BYTES:
                    LOG.warning("oversized SIP header from %s", peer)
                    break
                length = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        try:
                            length = int(line.split(b":", 1)[1].strip())
                        except ValueError:
                            length = 0
                body = await reader.readexactly(length) if length else b""
                try:
                    msg = sipmsg.SipMessage.parse(head + body)
                except ValueError as exc:
                    LOG.debug("bad SIP frame from %s: %s", peer, exc)
                    continue
                try:
                    self.server.on_message(msg, peer)
                except Exception:  # pragma: no cover
                    LOG.exception("handler failed for %s", peer)
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.LimitOverrunError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover
            LOG.debug("tcp connection %s ended: %s", peer, exc)
        finally:
            if self.server:
                self.server.unregister_peer(peer)
            peer.close()


async def open_tcp_connection(host, port, server=None, timeout=6.0):
    """Outbound SIP/TCP connection (used when we call a registered extension)."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, int(port or 5060)), timeout=timeout)
    return TcpPeer(reader, writer, server)