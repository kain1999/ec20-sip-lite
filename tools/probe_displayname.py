"""Replay the exact REGISTER a phone sends when the display name has no space.

    python tools/probe_displayname.py --server 192.168.1.188:5060 --user 1003 --password 123456
"""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sip import digest, message as sipmsg  # noqa: E402


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

    async def recv(self, timeout=5.0):
        return await asyncio.wait_for(self.queue.get(), timeout)


def branch():
    return "z9hG4bK" + secrets.token_hex(6)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="192.168.1.188:5060")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--display", default="1002")
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
    call_id = "%s@probe" % secrets.token_hex(4)
    contact = "<sip:%s@%s:%d;transport=udp>" % (args.user, local_ip, client.port)
    # the shape that broke the registrar: display name, no space, then <sip:user@host>
    from_to = '"%s"<sip:%s@%s>' % (args.display, args.user, host)

    def request(cseq, extra=None):
        headers = [
            ("Via", "SIP/2.0/UDP %s:%d;branch=%s;rport" % (local_ip, client.port, branch())),
            ("Max-Forwards", "70"),
            ("From", from_to + ";tag=probe"),
            ("To", from_to),
            ("Call-ID", call_id), ("CSeq", "%d REGISTER" % cseq),
            ("Contact", contact), ("Expires", "300"),
            ("User-Agent", "probe_displayname/1.0"),
        ]
        headers.extend(extra or [])
        return sipmsg.build_request("REGISTER", uri, headers=headers)

    print("To/From header sent :", from_to)
    client.send(request(1), (host, port))
    reply = await client.recv()
    print("REGISTER #1 ->", reply.status, reply.reason, flush=True)
    if reply.status == 403:
        print("still rejected: the box did not parse the user part")
        return 1
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
        reply = await client.recv()
        print("REGISTER #2 ->", reply.status, reply.reason, flush=True)
    ok = reply.status == 200
    print("RESULT:", "OK - display-name header accepted" if ok else "FAILED")
    transport.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))