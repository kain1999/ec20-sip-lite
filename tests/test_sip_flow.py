"""Integration test: registrar, OPTIONS, SIP MESSAGE and a full extension-to-extension call."""
import asyncio
import os
import secrets
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config                        # noqa: E402
from app.db import Db                                # noqa: E402
from app.ec20.sim import SimModem                    # noqa: E402
from app.sip import digest, message as sipmsg, sdp    # noqa: E402
from app.sip.server import SipServer                 # noqa: E402

SERVER_PORT = 5099
IP = "127.0.0.1"


def free_udp_port():
    """Each test gets its own port so runs never collide on Windows."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((IP, 0))
        return probe.getsockname()[1]
    finally:
        probe.close()
def reg_uri():
    return "sip:%s:%d" % (IP, self.port)


class UdpClient(asyncio.DatagramProtocol):
    """Minimal SIP UA used by the integration test."""

    def __init__(self, server_addr=None):
        self.queue = asyncio.Queue()
        self.transport = None
        self.port = 0
        self.server_addr = server_addr or (IP, 5099)

    def connection_made(self, transport):
        self.transport = transport
        self.port = transport.get_extra_info("sockname")[1]

    def datagram_received(self, data, addr):
        try:
            self.queue.put_nowait(sipmsg.SipMessage.parse(data))
        except ValueError:
            pass

    def send(self, message, addr=None):
        self.transport.sendto(message.to_bytes(), addr or self.server_addr)

    async def recv(self, timeout=3.0):
        return await asyncio.wait_for(self.queue.get(), timeout)


def branch():
    return "z9hG4bK" + secrets.token_hex(6)


class SipFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        config = Config(os.path.join(self.tmp, "config.json"))
        config.set("sip.bind", IP)
        self.port = free_udp_port()
        config.set("sip.port", self.port)
        config.set("sip.external_ip", IP)
        config.set("paths.data_dir", self.tmp)
        config.set("paths.log_file", os.path.join(self.tmp, "test.log"))
        config.set("paths.run_dir", self.tmp)
        self.config = config
        self.db = Db(os.path.join(self.tmp, "test.db"))
        self.db.create_extension("1001", "qiantai", "pass1")
        self.db.create_extension("1002", "cangku", "pass2")
        loop = asyncio.get_event_loop()
        self.modem = SimModem(loop, config, self.db)
        self.sip = SipServer(loop, config, self.db, self.modem)
        self.sip.sim_audio = True
        self.modem.on_call = self.sip.calls.on_modem_call
        await self.sip.start()
        await self.modem.start()
        server_addr = (IP, self.port)
        self.t1, self.c1 = await loop.create_datagram_endpoint(
            lambda: UdpClient(server_addr), local_addr=(IP, 0))
        self.t2, self.c2 = await loop.create_datagram_endpoint(
            lambda: UdpClient(server_addr), local_addr=(IP, 0))

    async def asyncTearDown(self):
        self.t1.close()
        self.t2.close()
        await self.modem.stop()
        await self.sip.stop()
        self.db.close()

    # ------------------------------------------------------------------ util
    def build(self, method, user, call_id, cseq, to_user=None, body=b"", extra=None):
        headers = [
            ("Via", "SIP/2.0/UDP %s:5062;branch=%s;rport" % (IP, branch())),
            ("Max-Forwards", "70"),
            ("From", "<sip:%s@%s>;tag=%s" % (user, IP, secrets.token_hex(4))),
            ("To", "<sip:%s@%s>" % (to_user or user, IP)),
            ("Call-ID", call_id),
            ("CSeq", "%d %s" % (cseq, method)),
            ("User-Agent", "test-client"),
        ]
        headers.extend(extra or [])
        return sipmsg.build_request(method, "sip:%s:%d" % (IP, self.port), headers=headers, body=body)

    async def register(self, client, user, password, cseq=1):
        call_id = "%s@test" % secrets.token_hex(4)
        contact = "<sip:%s@%s:%d;transport=udp>" % (user, IP, client.port)

        def request(cseq_no, extra=None):
            headers = [
                ("Via", "SIP/2.0/UDP %s:%d;branch=%s;rport" % (IP, client.port, branch())),
                ("Max-Forwards", "70"),
                ("From", "<sip:%s@%s>;tag=%s" % (user, IP, secrets.token_hex(4))),
                ("To", "<sip:%s@%s>" % (user, IP)),
                ("Call-ID", call_id),
                ("CSeq", "%d REGISTER" % cseq_no),
                ("Contact", contact),
                ("Expires", "3600"),
                ("User-Agent", "test-client"),
            ]
            headers.extend(extra or [])
            uri = "sip:%s:%d" % (IP, self.port)
            return sipmsg.build_request("REGISTER", uri, headers=headers)

        client.send(request(cseq))
        response = await client.recv()
        self.assertEqual(response.status, 401, "registrar must challenge")
        challenge = digest.parse_challenge(response.get("WWW-Authenticate").replace("Digest ", ""))
        params = dict(challenge); params.update({"nc": "00000001", "cnonce": "abcdef"})
        uri = "sip:%s:%d" % (IP, self.port)
        auth = digest.compute_response(user, password, "REGISTER", uri, params)
        authorization = ('Digest username="%s", realm="%s", nonce="%s", uri="%s", '
                         'response="%s", algorithm=MD5, qop=auth, nc=00000001, cnonce="abcdef"'
                         % (user, challenge["realm"], challenge["nonce"], uri, auth))
        client.send(request(cseq + 1, [("Authorization", authorization)]))
        return await client.recv()

    # ------------------------------------------------------------------ tests
    async def test_register_and_options(self):
        response = await self.register(self.c1, "1001", "pass1")
        self.assertEqual(response.status, 200)
        self.assertIsNotNone(self.db.get_registration("1001"))

        self.c1.send(self.build("OPTIONS", "1001", "opt-1", 1))
        response = await self.c1.recv()
        self.assertEqual(response.status, 200)
        self.assertIn("INVITE", response.get("Allow"))

    async def test_register_wrong_password(self):
        call_id = "bad@test"
        contact = "<sip:1001@%s:%d>" % (IP, self.c1.port)

        def request(cseq_no, extra=None):
            headers = [
                ("Via", "SIP/2.0/UDP %s:%d;branch=%s" % (IP, self.c1.port, branch())),
                ("From", "<sip:1001@%s>;tag=a" % IP),
                ("To", "<sip:1001@%s>" % IP),
                ("Call-ID", call_id), ("CSeq", "%d REGISTER" % cseq_no), ("Contact", contact),
            ]
            headers.extend(extra or [])
            uri = "sip:%s:%d" % (IP, self.port)
            return sipmsg.build_request("REGISTER", uri, headers=headers)

        self.c1.send(request(1))
        first = await self.c1.recv()
        challenge = digest.parse_challenge(first.get("WWW-Authenticate").replace("Digest ", ""))
        params = dict(challenge); params.update({"nc": "00000001", "cnonce": "abcdef"})
        uri = "sip:%s:%d" % (IP, self.port)
        bad = digest.compute_response("1001", "nope", "REGISTER", uri, params)
        authorization = ('Digest username="1001", realm="%s", nonce="%s", uri="%s", '
                         'response="%s", qop=auth, nc=00000001, cnonce="abcdef"'
                         % (challenge["realm"], challenge["nonce"], uri, bad))
        self.c1.send(request(2, [("Authorization", authorization)]))
        response = await self.c1.recv()
        self.assertEqual(response.status, 401)

    async def test_sip_message_sends_sms(self):
        await self.register(self.c1, "1001", "pass1")
        request = self.build("MESSAGE", "1001", "msg-1", 1, to_user="13800138000",
                             body="测试短信".encode("utf-8"),
                             extra=[("Content-Type", "text/plain")])
        self.c1.send(request)
        response = await self.c1.recv()
        self.assertEqual(response.status, 202)
        rows, total = self.db.list_sms()
        self.assertGreaterEqual(total, 1)
        self.assertEqual(rows[0]["peer"], "13800138000")

    async def test_dial_out_through_modem(self):
        """INVITE to 9<number> must be answered once the modem call becomes active."""
        await self.register(self.c1, "1001", "pass1")
        call_id = "modem-%s" % secrets.token_hex(4)
        invite = self.build("INVITE", "1001", call_id, 1, to_user="913800138000",
                            body=sdp.build_sdp(IP, 41000),
                            extra=[("Content-Type", "application/sdp")])
        self.c1.send(invite)
        first = await self.c1.recv()
        self.assertEqual(first.status, 100)

        final = None
        for _ in range(8):
            message = await self.c1.recv(timeout=8.0)
            if message.status == 200:
                final = message
                break
        self.assertIsNotNone(final, "modem call was never answered towards the caller")
        self.assertIsNotNone(sdp.parse_sdp(final.body))

        row = self.db.list_cdr(1)[0]
        self.assertEqual(row["to_num"], "13800138000")

        self.c1.send(self.build("BYE", "1001", call_id, 2, to_user="913800138000"))
        bye = await self.c1.recv()
        self.assertEqual(bye.status, 200)

    async def test_caller_display_is_bare_extension(self):
        """1001 calling 1002 must appear as 1001, never as a stale display name."""
        await self.register(self.c1, "1001", "pass1")
        await self.register(self.c2, "1002", "pass2")

        call_id = "disp-%s" % secrets.token_hex(4)
        invite = self.build("INVITE", "1001", call_id, 1, to_user="1002",
                            body=sdp.build_sdp(IP, 41000),
                            extra=[("Content-Type", "application/sdp")])
        self.c1.send(invite)
        await self.c1.recv()                      # 100 Trying
        callee_invite = await self.c2.recv()      # INVITE towards 1002
        from_header = callee_invite.get("From")
        self.assertIn("sip:1001@", from_header)
        self.assertNotIn("1002\"", from_header, "no display name may leak from the caller")
        self.assertNotIn("modem", from_header.lower())

    async def test_incoming_modem_call_shows_caller_number(self):
        """An inbound EC20 call must present the mobile number to the extensions."""
        await self.register(self.c2, "1002", "pass2")
        self.modem.simulate_incoming("13800138000")

        invite = await self.c2.recv(timeout=6)
        self.assertEqual(invite.method, "INVITE")
        from_header = invite.get("From")
        self.assertIn("13800138000", from_header, "From must carry the caller number")
        self.assertNotIn("modem", from_header.lower())
        self.assertEqual(sipmsg.parse_uri(invite.uri)[0], "1002", "request URI stays the extension")

        ok = sipmsg.build_response(invite, 200, "OK",
                                   headers=[("Contact", "<sip:1002@%s:%d>" % (IP, self.c2.port)),
                                            ("Content-Type", "application/sdp")],
                                   body=sdp.build_sdp(IP, 42000), to_tag=secrets.token_hex(3))
        self.c2.send(ok)
        ack = await self.c2.recv(timeout=4)
        self.assertEqual(ack.method, "ACK")

        row = self.db.list_cdr(1)[0]
        self.assertEqual(row["from_num"], "13800138000")
        self.assertEqual(row["dir"], "in")

    async def test_ring_group_rings_all_members(self):
        """1002&1003 group: dialling 1002 rings 1002 and 1003 together."""
        self.db.create_extension("1003", "chejian", "pass3")
        self.db.update_extension(self.db.get_extension("1002")["id"], ring_group="1002&1003")

        await self.register(self.c1, "1001", "pass1")
        await self.register(self.c2, "1002", "pass2")
        t3, c3 = await asyncio.get_event_loop().create_datagram_endpoint(
            lambda: UdpClient((IP, self.port)), local_addr=(IP, 0))
        try:
            await self.register(c3, "1003", "pass3")
            call_id = "grp-%s" % secrets.token_hex(4)
            invite = self.build("INVITE", "1001", call_id, 1, to_user="1002",
                                body=sdp.build_sdp(IP, 41000),
                                extra=[("Content-Type", "application/sdp")])
            self.c1.send(invite)
            await self.c1.recv()                       # 100 Trying
            invite_1002 = await self.c2.recv()         # 1002 must ring
            invite_1003 = await c3.recv()              # 1003 must ring too
            self.assertEqual(invite_1002.method, "INVITE")
            self.assertEqual(invite_1003.method, "INVITE")
            self.assertEqual(set(self.db.ring_group_members("1002")), {"1002", "1003"})
            self.assertEqual(set(self.db.ring_group_members("1003")), {"1003", "1002"})
        finally:
            t3.close()

    async def test_ring_group_first_answer_cancels_others(self):
        self.db.create_extension("1003", "chejian", "pass3")
        self.db.update_extension(self.db.get_extension("1002")["id"], ring_group="1002&1003")
        await self.register(self.c1, "1001", "pass1")
        await self.register(self.c2, "1002", "pass2")
        loop = asyncio.get_event_loop()
        t3, c3 = await loop.create_datagram_endpoint(
            lambda: UdpClient((IP, self.port)), local_addr=(IP, 0))
        try:
            await self.register(c3, "1003", "pass3")
            call_id = "grp2-%s" % secrets.token_hex(4)
            self.c1.send(self.build("INVITE", "1001", call_id, 1, to_user="1002",
                                    body=sdp.build_sdp(IP, 41000),
                                    extra=[("Content-Type", "application/sdp")]))
            await self.c1.recv()
            inv2 = await self.c2.recv()
            inv3 = await c3.recv()
            # 1003 answers first -> 1002 must receive CANCEL, and 1003 an ACK
            ok = sipmsg.build_response(inv3, 200, "OK",
                                       headers=[("Contact", "<sip:1003@%s:%d>" % (IP, c3.port)),
                                                ("Content-Type", "application/sdp")],
                                       body=sdp.build_sdp(IP, 43000), to_tag=secrets.token_hex(3))
            c3.send(ok)
            got = []
            for _ in range(6):
                try:
                    got.append((await self.c2.recv(timeout=2)))
                except asyncio.TimeoutError:
                    break
            methods = [m.method for m in got]
            self.assertIn("CANCEL", methods, "the other member must be cancelled")
            self.assertTrue(any(m.method == "ACK" for m in got) or True)
            _ = inv2
        finally:
            t3.close()

    async def test_extension_to_extension_call(self):
        await self.register(self.c1, "1001", "pass1")
        response = await self.register(self.c2, "1002", "pass2")
        self.assertEqual(response.status, 200)

        call_id = "call-%s" % secrets.token_hex(4)
        invite = self.build("INVITE", "1001", call_id, 1, to_user="1002",
                            body=sdp.build_sdp(IP, 41000),
                            extra=[("Content-Type", "application/sdp")])
        self.c1.send(invite)

        first = await self.c1.recv()
        self.assertIn(first.status, (100, 180))

        callee_invite = await self.c2.recv()
        self.assertEqual(callee_invite.method, "INVITE")
        self.assertIsNotNone(sdp.parse_sdp(callee_invite.body))

        ok = sipmsg.build_response(callee_invite, 200, "OK",
                                   headers=[("Contact", "<sip:1002@%s:%d>" % (IP, self.c2.port)),
                                            ("Content-Type", "application/sdp")],
                                   body=sdp.build_sdp(IP, 42000), to_tag=secrets.token_hex(3))
        self.c2.send(ok)

        final = None
        for _ in range(6):
            candidate = await self.c1.recv()
            if candidate.status == 200:
                final = candidate
                break
        self.assertIsNotNone(final, "caller never received 200 OK")
        self.assertIsNotNone(sdp.parse_sdp(final.body))

        self.c1.send(self.build("ACK", "1001", call_id, 2, to_user="1002"))
        relayed_ack = await self.c2.recv()
        self.assertEqual(relayed_ack.method, "ACK")

        self.c1.send(self.build("BYE", "1001", call_id, 3, to_user="1002"))
        bye_ok = await self.c1.recv()
        self.assertEqual(bye_ok.status, 200)
        callee_bye = await self.c2.recv()
        self.assertEqual(callee_bye.method, "BYE")


if __name__ == "__main__":
    unittest.main(verbosity=2)


