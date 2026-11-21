"""SIP smoke-test client: REGISTER / OPTIONS / INVITE against an ec20-sip-lite box.

Usage:
    python tools/sip_probe.py --server 192.168.1.188:5060 --user 1001 --password 1001pass
    python tools/sip_probe.py ... --invite 1002      # place an extension-to-extension call
    python tools/sip_probe.py ... --invite 910000    # dial out through the EC20
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
            self.queue.put_nowait(sipmsg.SipMessage.parse(data))
        except ValueError:
            pass

    def send(self, message, addr):
        self.transport.sendto(message.to_bytes(), addr)

    async def recv(self, timeout=4.0):
        return await asyncio.wait_for(self.queue.get(), timeout)


def branch():
    return "z9hG4bK" + secrets.token_hex(6)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="127.0.0.1:5060")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--invite", default="")
    parser.add_argument("--local-ip", default="")
    args = parser.parse_args()

    host, _, port_s = args.server.partition(":")
    port = int(port_s or 5060)
    server = (host, port)
    loop = asyncio.get_running_loop()
    transport, client = await loop.create_datagram_endpoint(Client, local_addr=("0.0.0.0", 0))
    local_ip = args.local_ip or transport.get_extra_info("sockname")[0]
    if local_ip.startswith("0.0.0.0"):
        local_ip = "127.0.0.1"
    uri = "sip:%s:%d" % (host, port)

    # ------------------------------------------------------------- REGISTER
    call_id = "%s@probe" % secrets.token_hex(4)
    contact = "<sip:%s@%s:%d;transport=udp>" % (args.user, local_ip, client.port)

    def register_request(cseq, extra=None, tag=None):
        headers = [
            ("Via", "SIP/2.0/UDP %s:%d;branch=%s;rport" % (local_ip, client.port, branch())),
            ("Max-Forwards", "70"),
            ("From", "<sip:%s@%s>;tag=%s" % (args.user, host, tag or "probe")),
            ("To", "<sip:%s@%s>" % (args.user, host)),
            ("Call-ID", call_id),
            ("CSeq", "%d REGISTER" % cseq),
            ("Contact", contact),
            ("Expires", "300"),
            ("User-Agent", "sip_probe/1.0"),
        ]
        headers.extend(extra or [])
        return sipmsg.build_request("REGISTER", uri, headers=headers)

    client.send(register_request(1), server)
    first = await client.recv()
    print("REGISTER #1 ->", first.status, first.reason)
    if first.status == 401:
        challenge = digest.parse_challenge(first.get("WWW-Authenticate").replace("Digest ", ""))
        params = dict(challenge)
        params.update({"nc": "00000001", "cnonce": secrets.token_hex(4)})
        response = digest.compute_response(args.user, args.password, "REGISTER", uri, params)
        header = ('Digest username="%s", realm="%s", nonce="%s", uri="%s", response="%s", '
                  'algorithm=MD5, qop=auth, nc=%s, cnonce=%s'
                  % (args.user, challenge["realm"], challenge["nonce"], uri, response,
                     params["nc"], params["cnonce"]))
        client.send(register_request(2, [("Authorization", header)]), server)
        second = await client.recv()
        print("REGISTER #2 ->", second.status, second.reason)
        if second.status != 200:
            print("registration failed")
            return 1

    # -------------------------------------------------------------- OPTIONS
    options = sipmsg.build_request("OPTIONS", uri, headers=[
        ("Via", "SIP/2.0/UDP %s:%d;branch=%s;rport" % (local_ip, client.port, branch())),
        ("From", "<sip:%s@%s>;tag=%s" % (args.user, host, secrets.token_hex(3))),
        ("To", "<sip:%s@%s>" % (host, host)),
        ("Call-ID", "%s@probe" % secrets.token_hex(4)), ("CSeq", "1 OPTIONS"),
    ])
    client.send(options, server)
    reply = await client.recv()
    print("OPTIONS ->", reply.status, reply.reason, "|", reply.get("Allow", ""))

    if not args.invite:
        transport.close()
        return 0

    # --------------------------------------------------------------- INVITE
    invite_call_id = "%s@probe" % secrets.token_hex(4)
    rtp_probe = 40000 + (client.port % 500)
    body = sdp.build_sdp(local_ip, rtp_probe)
    invite = sipmsg.build_request("INVITE", "sip:%s@%s:%d" % (args.invite, host, port), headers=[
        ("Via", "SIP/2.0/UDP %s:%d;branch=%s;rport" % (local_ip, client.port, branch())),
        ("Max-Forwards", "70"),
        ("From", "<sip:%s@%s>;tag=%s" % (args.user, host, secrets.token_hex(3))),
        ("To", "<sip:%s@%s>" % (args.invite, host)),
        ("Call-ID", invite_call_id), ("CSeq", "1 INVITE"),
        ("Contact", contact), ("Content-Type", "application/sdp"),
    ], body=body)
    client.send(invite, server)
    print("INVITE ->", args.invite)
    for _ in range(6):
        try:
            message = await client.recv(timeout=6.0)
        except asyncio.TimeoutError:
            print("no further response")
            break
        if message.is_request:
            print("  <- request", message.method)
            continue
        print("  <-", message.status, message.reason)
        if message.status >= 200:
            break
    client.send(sipmsg.build_request("BYE", "sip:%s@%s:%d" % (args.invite, host, port), headers=[
        ("Via", "SIP/2.0/UDP %s:%d;branch=%s" % (local_ip, client.port, branch())),
        ("From", "<sip:%s@%s>;tag=probe" % (args.user, host)),
        ("To", "<sip:%s@%s>;tag=probe" % (args.invite, host)),
        ("Call-ID", invite_call_id), ("CSeq", "2 BYE"),
    ]), server)
    try:
        bye = await client.recv(timeout=3)
        print("BYE ->", bye.status, bye.reason)
    except asyncio.TimeoutError:
        print("BYE -> no answer")
    transport.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))