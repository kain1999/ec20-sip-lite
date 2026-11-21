"""Small SIP/TCP user agent shared by the dev tools (no dependencies)."""
from __future__ import annotations

import asyncio
import secrets

from app.sip import digest, message as sipmsg


def branch():
    return "z9hG4bK" + secrets.token_hex(6)


class Ua:
    def __init__(self, label=""):
        self.label = label
        self.reader = None
        self.writer = None
        self.port = 0
        self.queue = asyncio.Queue()
        self._task = None

    async def connect(self, host, port):
        self.reader, self.writer = await asyncio.open_connection(host, port)
        self.port = self.writer.get_extra_info("sockname")[1]
        self._task = asyncio.get_event_loop().create_task(self._read())
        return self

    async def _read(self):
        try:
            while True:
                head = await self.reader.readuntil(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        try:
                            length = int(line.split(b":", 1)[1])
                        except ValueError:
                            length = 0
                body = await self.reader.readexactly(length) if length else b""
                try:
                    self.queue.put_nowait(sipmsg.SipMessage.parse(head + body))
                except ValueError:
                    pass
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.CancelledError):
            pass

    def send(self, message):
        if self.writer is None or self.writer.is_closing():
            raise RuntimeError("connection closed")
        self.writer.write(message.to_bytes())

    async def recv(self, timeout=6.0):
        return await asyncio.wait_for(self.queue.get(), timeout)

    def close(self):
        if self._task:
            self._task.cancel()
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass

    # ---------------------------------------------------------------- helpers
    async def register(self, host, port, user, password, expires=3600, contact_host=None):
        uri = "sip:%s:%d" % (host, port)
        call_id = "%s@ua" % secrets.token_hex(4)
        contact_host = contact_host or "0.0.0.0"
        contact = "<sip:%s@%s:%d;transport=tcp>" % (user, contact_host, self.port)

        def request(cseq, extra=None):
            headers = [
                ("Via", "SIP/2.0/TCP %s:%d;branch=%s" % (contact_host, self.port, branch())),
                ("Max-Forwards", "70"),
                ("From", "<sip:%s@%s>;tag=%s" % (user, host, secrets.token_hex(3))),
                ("To", "<sip:%s@%s>" % (user, host)),
                ("Call-ID", call_id), ("CSeq", "%d REGISTER" % cseq),
                ("Contact", contact), ("Expires", str(expires)),
                ("User-Agent", "ec20-sip-lite-tool/1.0"),
            ]
            headers.extend(extra or [])
            return sipmsg.build_request("REGISTER", uri, headers=headers)

        self.send(request(1))
        reply = await self.recv()
        if reply.status == 401:
            challenge = digest.parse_challenge(reply.get("WWW-Authenticate").replace("Digest ", ""))
            params = dict(challenge)
            params.update({"nc": "00000001", "cnonce": secrets.token_hex(4)})
            response = digest.compute_response(user, password, "REGISTER", uri, params)
            header = ('Digest username="%s", realm="%s", nonce="%s", uri="%s", response="%s", '
                      'algorithm=MD5, qop=auth, nc=%s, cnonce=%s'
                      % (user, challenge["realm"], challenge["nonce"], uri, response,
                         params["nc"], params["cnonce"]))
            self.send(request(2, [("Authorization", header)]))
            reply = await self.recv()
        return reply