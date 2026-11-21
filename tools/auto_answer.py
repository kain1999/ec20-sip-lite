"""Auto-answer UA (SIP/TCP): register, answer the first INVITE, hold, hang up.

    python tools/auto_answer.py --server 127.0.0.1:5060 --user 1002 --password 123456 --hold 8

Use together with tools/sip_probe.py --invite 1002 --hold 15 to verify that a
hangup on one side reaches the other.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sip import message as sipmsg, sdp  # noqa: E402
from tools.tcp_ua import Ua, branch         # noqa: E402


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="127.0.0.1:5060")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--hold", type=float, default=8.0)
    parser.add_argument("--contact-host", default="0.0.0.0")
    args = parser.parse_args()

    host, _, port_s = args.server.partition(":")
    port = int(port_s or 5060)
    ua = await Ua(args.user).connect(host, port)
    try:
        reply = await ua.register(host, port, args.user, args.password,
                                  contact_host=args.contact_host)
        print("REGISTER ->", reply.status, reply.reason, flush=True)
        if reply.status != 200:
            return 1

        print("waiting for INVITE...", flush=True)
        invite = None
        while invite is None:
            message = await ua.recv(timeout=180)
            if message.is_request and message.method == "INVITE":
                invite = message
        peer_sdp = sdp.parse_sdp(invite.body)
        print("INVITE from", invite.get("From"), "sdp:", peer_sdp, flush=True)

        ua.send(sipmsg.build_response(invite, 200, "OK", headers=[
            ("Contact", "<sip:%s@%s:%d;transport=tcp>" % (args.user, args.contact_host, ua.port)),
            ("Content-Type", "application/sdp")],
            body=sdp.build_sdp(args.contact_host, 41000),
            to_tag=secrets.token_hex(3)))
        print("answered (200 OK), holding %.0fs" % args.hold, flush=True)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + args.hold
        while loop.time() < deadline:
            try:
                message = await ua.recv(timeout=max(0.5, deadline - loop.time()))
            except asyncio.TimeoutError:
                break
            if message.is_request and message.method == "ACK":
                print("ACK received", flush=True)
            elif message.is_request and message.method == "BYE":
                ua.send(sipmsg.build_response(message, 200, "OK"))
                print("remote BYE, replied 200", flush=True)
                return 0

        ua.send(sipmsg.build_request("BYE", "sip:%s:%d" % (host, port), headers=[
            ("Via", "SIP/2.0/TCP %s:%d;branch=%s" % (args.contact_host, ua.port, branch())),
            ("Max-Forwards", "70"),
            ("From", invite.get("To")),
            ("To", invite.get("From")),
            ("Call-ID", invite.call_id),
            ("CSeq", "%d BYE" % (int(invite.cseq.split()[0]) + 1)),
            ("Content-Length", "0"),
        ]))
        print("sent BYE", flush=True)
        return 0
    finally:
        ua.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))