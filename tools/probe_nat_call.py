"""Probe the frp/NAT case: register with an unreachable Contact and see whether
the server can INVITE us back.

    python tools/probe_nat_call.py --server 8.137.213.214:5060 --user 1003 --password 123456 \
        --caller-user 1001 --caller-password 123456
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
    def __init__(self, label):
        self.label = label
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

    async def recv(self, timeout=6.0):
        return await asyncio.wait_for(self.queue.get(), timeout)


def branch():
    return "z9hG4bK" + secrets.token_hex(6)


async def register(loop, host, port, user, password, contact_host):
    transport, client = await loop.create_datagram_endpoint(
        lambda: Client(user), local_addr=("0.0.0.0", 0))
    uri = "sip:%s:%d" % (host, port)
    call_id = "%s@nat" % secrets.token_hex(4)
    contact = "<sip:%s@%s:%d;transport=udp>" % (user, contact_host, client.port)

    def request(cseq, extra=None):
        headers = [
            ("Via", "SIP/2.0/UDP %s:%d;branch=%s;rport" % (contact_host, client.port, branch())),
            ("Max-Forwards", "70"),
            ("From", "<sip:%s@%s>;tag=%s" % (user, host, secrets.token_hex(3))),
            ("To", "<sip:%s@%s>" % (user, host)),
            ("Call-ID", call_id), ("CSeq", "%d REGISTER" % cseq),
            ("Contact", contact), ("Expires", "120"),
            ("User-Agent", "probe_nat/1.0"),
        ]
        headers.extend(extra or [])
        return sipmsg.build_request("REGISTER", uri, headers=headers)

    client.send(request(1), (host, port))
    reply, _ = await client.recv()
    if reply.status == 401:
        challenge = digest.parse_challenge(reply.get("WWW-Authenticate").replace("Digest ", ""))
        params = dict(challenge)
        params.update({"nc": "00000001", "cnonce": secrets.token_hex(4)})
        response = digest.compute_response(user, password, "REGISTER", uri, params)
        header = ('Digest username="%s", realm="%s", nonce="%s", uri="%s", response="%s", '
                  'algorithm=MD5, qop=auth, nc=%s, cnonce=%s'
                  % (user, challenge["realm"], challenge["nonce"], uri, response,
                     params["nc"], params["cnonce"]))
        client.send(request(2, [("Authorization", header)]), (host, port))
        reply, _ = await client.recv()
    print("  register %s -> %s %s (contact host %s)" % (user, reply.status, reply.reason, contact_host))
    return transport, client, call_id


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="8.137.213.214:5060")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--caller-user", required=True)
    parser.add_argument("--caller-password", required=True)
    parser.add_argument("--contact-host", default="0.0.0.0",
                        help="0.0.0.0 mimics a NATed phone: the registrar falls back to the UDP source")
    args = parser.parse_args()

    host, _, port_s = args.server.partition(":")
    port = int(port_s or 5060)
    loop = asyncio.get_running_loop()

    print("1) register the callee (NATed contact)")
    t_callee, callee, _ = await register(loop, host, port, args.user, args.password, args.contact_host)
    print("2) register the caller")
    t_caller, caller, _ = await register(loop, host, port, args.caller_user, args.caller_password,
                                         "127.0.0.1")
    await asyncio.sleep(1)

    print("3) caller dials %s ..." % args.user)
    call_id = "%s@nat" % secrets.token_hex(4)
    invite = sipmsg.build_request("INVITE", "sip:%s@%s:%d" % (args.user, host, port), headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1:%d;branch=%s;rport" % (caller.port, branch())),
        ("Max-Forwards", "70"),
        ("From", "<sip:%s@%s>;tag=%s" % (args.caller_user, host, secrets.token_hex(3))),
        ("To", "<sip:%s@%s>" % (args.user, host)),
        ("Call-ID", call_id), ("CSeq", "1 INVITE"),
        ("Contact", "<sip:%s@127.0.0.1:%d>" % (args.caller_user, caller.port)),
        ("Content-Type", "application/sdp"),
    ], body=sdp.build_sdp("127.0.0.1", 40000))
    caller.send(invite, (host, port))

    answer = None
    for _ in range(6):
        try:
            message = await caller.recv(timeout=5)
        except asyncio.TimeoutError:
            break
        msg, _addr = message
        if msg.is_request:
            print("   caller <- request", msg.method)
            continue
        print("   caller <-", msg.status, msg.reason)
        if msg.status >= 200:
            answer = msg.status
            break

    got_invite = False
    try:
        msg, _addr = await callee.recv(timeout=5)
        print("   callee received:", msg.method if msg.is_request else msg.status)
        got_invite = msg.is_request and msg.method == "INVITE"
    except asyncio.TimeoutError:
        print("   callee received: nothing")

    print()
    print("RESULT: server->callee reachable =", got_invite, "| caller final =", answer)
    t_callee.close()
    t_caller.close()
    return 0 if got_invite else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))