"""Does the server reach a NATed extension through frp? (SIP over TCP version)

    python tools/probe_nat_call.py --server 8.137.213.214:5060 \
        --user 1003 --password 123456 --caller-user 1001 --caller-password 123456

Expects "server->callee reachable = True" once SIP/TCP is in use.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sip import digest, message as sipmsg, sdp  # noqa: E402


class Client:
    def __init__(self, label):
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
                self.queue.put_nowait(sipmsg.SipMessage.parse(head + body))
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.CancelledError):
            pass

    def send(self, message):
        self.writer.write(message.to_bytes())

    async def recv(self, timeout=8.0):
        return await asyncio.wait_for(self.queue.get(), timeout)

    def close(self):
        if self._task:
            self._task.cancel()
        if self.writer:
            self.writer.close()


def branch():
    return "z9hG4bK" + secrets.token_hex(6)


async def register(host, port, user, password):
    client = await Client(user).connect(host, port)
    uri = "sip:%s:%d" % (host, port)
    call_id = "%s@nat" % secrets.token_hex(4)
    contact = "<sip:%s@%s:%d;transport=tcp>" % (user, "0.0.0.0", client.port)

    def request(cseq, extra=None):
        headers = [
            ("Via", "SIP/2.0/TCP 0.0.0.0:%d;branch=%s" % (client.port, branch())),
            ("Max-Forwards", "70"),
            ("From", "<sip:%s@%s>;tag=%s" % (user, host, secrets.token_hex(3))),
            ("To", "<sip:%s@%s>" % (user, host)),
            ("Call-ID", call_id), ("CSeq", "%d REGISTER" % cseq),
            ("Contact", contact), ("Expires", "300"),
            ("User-Agent", "probe_nat/2.0"),
        ]
        headers.extend(extra or [])
        return sipmsg.build_request("REGISTER", uri, headers=headers)

    client.send(request(1))
    reply = await client.recv()
    if reply.status == 401:
        challenge = digest.parse_challenge(reply.get("WWW-Authenticate").replace("Digest ", ""))
        params = dict(challenge)
        params.update({"nc": "00000001", "cnonce": secrets.token_hex(4)})
        response = digest.compute_response(user, password, "REGISTER", uri, params)
        header = ('Digest username="%s", realm="%s", nonce="%s", uri="%s", response="%s", '
                  'algorithm=MD5, qop=auth, nc=%s, cnonce=%s'
                  % (user, challenge["realm"], challenge["nonce"], uri, response,
                     params["nc"], params["cnonce"]))
        client.send(request(2, [("Authorization", header)]))
        reply = await client.recv()
    print("  register %s -> %s %s" % (user, reply.status, reply.reason))
    return client, call_id


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="8.137.213.214:5060")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--caller-user", required=True)
    parser.add_argument("--caller-password", required=True)
    args = parser.parse_args()

    host, _, port_s = args.server.partition(":")
    port = int(port_s or 5060)

    print("1) register the callee (NATed)")
    callee, _ = await register(host, port, args.user, args.password)
    print("2) register the caller")
    caller, _ = await register(host, port, args.caller_user, args.caller_password)
    await asyncio.sleep(0.5)

    print("3) caller dials %s ..." % args.user)
    call_id = "%s@nat" % secrets.token_hex(4)
    caller.send(sipmsg.build_request("INVITE", "sip:%s@%s:%d" % (args.user, host, port), headers=[
        ("Via", "SIP/2.0/TCP 0.0.0.0:%d;branch=%s" % (caller.port, branch())),
        ("Max-Forwards", "70"),
        ("From", "<sip:%s@%s>;tag=%s" % (args.caller_user, host, secrets.token_hex(3))),
        ("To", "<sip:%s@%s>" % (args.user, host)),
        ("Call-ID", call_id), ("CSeq", "1 INVITE"),
        ("Contact", "<sip:%s@0.0.0.0:%d>" % (args.caller_user, caller.port)),
        ("Content-Type", "application/sdp"),
    ], body=sdp.build_sdp("0.0.0.0", 40000)))

    final = None
    for _ in range(6):
        try:
            message = await caller.recv(timeout=6)
        except asyncio.TimeoutError:
            break
        if message.is_request:
            print("   caller <- request", message.method)
            continue
        print("   caller <-", message.status, message.reason)
        if message.status >= 200:
            final = message.status
            break

    got_invite = False
    try:
        message = await callee.recv(timeout=6)
        if message.is_request:
            print("   callee received:", message.method, "from", message.get("From"))
            got_invite = message.method == "INVITE"
        else:
            print("   callee received:", message.status)
    except asyncio.TimeoutError:
        print("   callee received: nothing")

    print()
    print("RESULT: server->callee reachable =", got_invite, "| caller final =", final)
    callee.close()
    caller.close()
    return 0 if got_invite else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))