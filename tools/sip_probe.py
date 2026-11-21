"""SIP/TCP smoke test: REGISTER / OPTIONS / INVITE / hangup observation.

    python tools/sip_probe.py --server 8.137.213.214:5060 --user 1001 --password 123456
    python tools/sip_probe.py ... --invite 1002                 # place a call
    python tools/sip_probe.py ... --invite 1002 --hold 10       # wait for the BYE
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
    parser.add_argument("--invite", default="")
    parser.add_argument("--hold", type=float, default=0.0,
                        help="after the call is up, wait N seconds for the server's BYE")
    parser.add_argument("--contact-host", default="0.0.0.0")
    args = parser.parse_args()

    host, _, port_s = args.server.partition(":")
    port = int(port_s or 5060)
    ua = await Ua(args.user).connect(host, port)
    try:
        reply = await ua.register(host, port, args.user, args.password,
                                  contact_host=args.contact_host)
        print("REGISTER ->", reply.status, reply.reason)
        if reply.status != 200:
            print("registration failed")
            return 1

        ua.send(sipmsg.build_request("OPTIONS", "sip:%s:%d" % (host, port), headers=[
            ("Via", "SIP/2.0/TCP %s:%d;branch=%s" % (args.contact_host, ua.port, branch())),
            ("From", "<sip:%s@%s>;tag=%s" % (args.user, host, secrets.token_hex(3))),
            ("To", "<sip:%s@%s>" % (host, host)),
            ("Call-ID", "%s@probe" % secrets.token_hex(4)), ("CSeq", "1 OPTIONS"),
        ]))
        reply = await ua.recv()
        print("OPTIONS ->", reply.status, "|", reply.get("Allow", ""))

        if not args.invite:
            return 0

        call_id = "%s@probe" % secrets.token_hex(4)
        from_header = "<sip:%s@%s>;tag=%s" % (args.user, host, secrets.token_hex(3))
        ua.send(sipmsg.build_request("INVITE", "sip:%s@%s:%d" % (args.invite, host, port), headers=[
            ("Via", "SIP/2.0/TCP %s:%d;branch=%s" % (args.contact_host, ua.port, branch())),
            ("Max-Forwards", "70"),
            ("From", from_header),
            ("To", "<sip:%s@%s>" % (args.invite, host)),
            ("Call-ID", call_id), ("CSeq", "1 INVITE"),
            ("Contact", "<sip:%s@%s:%d;transport=tcp>" % (args.user, args.contact_host, ua.port)),
            ("Content-Type", "application/sdp"),
        ], body=sdp.build_sdp(args.contact_host, 40000)))
        print("INVITE ->", args.invite)

        final = None
        for _ in range(8):
            try:
                message = await ua.recv(timeout=6)
            except asyncio.TimeoutError:
                print("no further response")
                break
            if message.is_request:
                print("  <- request", message.method)
                continue
            print("  <-", message.status, message.reason)
            if message.status >= 200:
                final = message
                break

        if final is not None and final.status < 300:
            ua.send(sipmsg.build_request("ACK", "sip:%s@%s:%d" % (args.invite, host, port), headers=[
                ("Via", "SIP/2.0/TCP %s:%d;branch=%s" % (args.contact_host, ua.port, branch())),
                ("Max-Forwards", "70"),
                ("From", from_header),
                ("To", final.get("To")),
                ("Call-ID", call_id), ("CSeq", "1 ACK"), ("Content-Length", "0"),
            ]))
            if args.hold > 0:
                print("holding %.0fs, waiting for the server to hang up..." % args.hold)
                loop = asyncio.get_running_loop()
                deadline = loop.time() + args.hold
                got_bye = False
                while loop.time() < deadline:
                    try:
                        message = await ua.recv(timeout=deadline - loop.time())
                    except asyncio.TimeoutError:
                        break
                    if message.is_request and message.method == "BYE":
                        ua.send(sipmsg.build_response(message, 200, "OK"))
                        print("  <- BYE  [OK: the other side hung up]")
                        got_bye = True
                        break
                if not got_bye:
                    print("  no BYE received [the other side was not told]")
                return 0 if got_bye else 1

        ua.send(sipmsg.build_request("BYE", "sip:%s@%s:%d" % (args.invite, host, port), headers=[
            ("Via", "SIP/2.0/TCP %s:%d;branch=%s" % (args.contact_host, ua.port, branch())),
            ("From", from_header),
            ("To", "<sip:%s@%s>;tag=probe" % (args.invite, host)),
            ("Call-ID", call_id), ("CSeq", "2 BYE"),
        ]))
        try:
            bye = await ua.recv(timeout=3)
            print("BYE ->", bye.status, bye.reason)
        except asyncio.TimeoutError:
            print("BYE -> no answer")
        return 0
    finally:
        ua.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))