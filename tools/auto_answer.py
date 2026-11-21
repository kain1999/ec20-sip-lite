"""Test UA: register, auto-answer an INVITE, hold, then hang up.

    python tools/auto_answer.py --server 127.0.0.1:5060 --user 1002 --password 1002pass --hold 8
"""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sip import digest, message as sipmsg, sdp  # noqa: E402


class Client(asyncio.DatagramProtocol):
    def __init__(self):
        self.queue = asyncio.Queue()
        self.transport = None
        self.port = 0

    def connection_made(self, transport):
        self.transport = transport
        self.port = transport.get_extra_info("sockname")[1]

    def datagram_received(self, data, addr):
        try:
            self.queue.put_nowait((sipmsg.SipMessage.parse(data), addr))
        except ValueError:
            pass

    def send(self, message, addr):
        self.transport.sendto(message.to_bytes(), addr)

    async def recv(self, timeout=10.0):
        return await asyncio.wait_for(self.queue.get(), timeout)


def branch():
    return "z9hG4bK" + secrets.token_hex(6)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="127.0.0.1:5060")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--hold", type=float, default=8.0)
    parser.add_argument("--local-ip", default="")
    args = parser.parse_args()

    host, _, port_s = args.server.partition(":")
    port = int(port_s or 5060)
    loop = asyncio.get_running_loop()
    transport, client = await loop.create_datagram_endpoint(Client, local_addr=("0.0.0.0", 0))
    local_ip = args.local_ip or transport.get_extra_info("sockname")[0]
    if local_ip.startswith("0.0.0.0"):
        local_ip = "127.0.0.1"
    uri = "sip:%s:%d" % (host, port)
    contact = "<sip:%s@%s:%d;transport=udp>" % (args.user, local_ip, client.port)

    call_id = "%s@ua" % secrets.token_hex(4)

    def request(cseq, extra=None):
        headers = [
            ("Via", "SIP/2.0/UDP %s:%d;branch=%s;rport" % (local_ip, client.port, branch())),
            ("Max-Forwards", "70"),
            ("From", "<sip:%s@%s>;tag=ua" % (args.user, host)),
            ("To", "<sip:%s@%s>" % (args.user, host)),
            ("Call-ID", call_id), ("CSeq", "%d REGISTER" % cseq),
            ("Contact", contact), ("Expires", "600"),
            ("User-Agent", "auto_answer/1.0"),
        ]
        headers.extend(extra or [])
        return sipmsg.build_request("REGISTER", uri, headers=headers)

    client.send(request(1), (host, port))
    reply, _ = await client.recv()
    if reply.status == 401:
        challenge = digest.parse_challenge(reply.get("WWW-Authenticate").replace("Digest ", ""))
        params = dict(challenge)
        params.update({"nc": "00000001", "cnonce": secrets.token_hex(4)})
        response = digest.compute_response(args.user, args.password, "REGISTER", uri, params)
        header = ('Digest username="%s", realm="%s", nonce="%s", uri="%s", response="%s", '
                  'algorithm=MD5, qop=auth, nc=%s, cnonce=%s'
                  % (args.user, challenge["realm"], challenge["nonce"], uri, response,
                     params["nc"], params["cnonce"]))
        client.send(request(2, [("Authorization", header)]), (host, port))
        reply, _ = await client.recv()
    print("REGISTER ->", reply.status, reply.reason, flush=True)
    if reply.status != 200:
        return 1

    print("waiting for INVITE...", flush=True)
    while True:
        message, addr = await client.recv(timeout=120)
        if message.is_request and message.method == "INVITE":
            break
    caller_sdp = sdp.parse_sdp(message.body)
    print("INVITE from", message.get("From"), "sdp:", caller_sdp, flush=True)
    ok = sipmsg.build_response(message, 200, "OK", headers=[
        ("Contact", contact), ("Content-Type", "application/sdp")],
        body=sdp.build_sdp(local_ip, 41000), to_tag=secrets.token_hex(3))
    client.send(ok, addr)
    print("answered (200 OK), holding %.0fs" % args.hold, flush=True)

    bye_sent = False
    deadline = loop.time() + args.hold
    while loop.time() < deadline:
        try:
            extra, extra_addr = await client.recv(timeout=deadline - loop.time())
        except asyncio.TimeoutError:
            break
        if extra.is_request and extra.method == "ACK":
            print("ACK received", flush=True)
        elif extra.is_request and extra.method == "BYE":
            client.send(sipmsg.build_response(extra, 200, "OK"), extra_addr)
            print("remote BYE, replied 200", flush=True)
            bye_sent = True
            break

    if not bye_sent:
        client.send(sipmsg.build_request("BYE", "sip:%s@%s:%d" % (args.user, host, port), headers=[
            ("Via", "SIP/2.0/UDP %s:%d;branch=%s" % (local_ip, client.port, branch())),
            ("From", "<sip:%s@%s>;tag=ua" % (args.user, host)),
            ("To", "<sip:%s@%s>" % (args.user, host)),
            ("Call-ID", message.call_id), ("CSeq", "3 BYE"),
        ]), addr)
        print("sent BYE", flush=True)
    transport.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))