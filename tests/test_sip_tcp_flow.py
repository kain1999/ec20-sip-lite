"""Integration test: SIP over TCP — registrar, OPTIONS, MESSAGE, calls, hangups."""
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

IP = "127.0.0.1"


def free_tcp_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((IP, 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


class TcpClient:
    """A SIP/TCP user agent: one connection, framed reads."""

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
        self._task = asyncio.get_event_loop().create_task(self._read_loop())
        return self

    async def _read_loop(self):
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

    def send(self, message, addr=None):
        if self.writer is None or self.writer.is_closing():
            raise RuntimeError("connection closed")
        self.writer.write(message.to_bytes())

    async def recv(self, timeout=4.0):
        return await asyncio.wait_for(self.queue.get(), timeout)

    def close(self):
        if self._task:
            self._task.cancel()
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass


def branch():
    return "z9hG4bK" + secrets.token_hex(6)


class SipTcpFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        config = Config(os.path.join(self.tmp, "config.json"))
        config.set("sip.bind", IP)
        self.port = free_tcp_port()
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
        self.c1 = await TcpClient("1001").connect(IP, self.port)
        self.c2 = await TcpClient("1002").connect(IP, self.port)

    async def asyncTearDown(self):
        self.c1.close()
        self.c2.close()
        await self.modem.stop()
        await self.sip.stop()
        self.db.close()

    # ------------------------------------------------------------------ utils
    def uri(self):
        return "sip:%s:%d" % (IP, self.port)

    def build(self, method, user, call_id, cseq, to_user=None, body=b"", extra=None):
        headers = [
            ("Via", "SIP/2.0/TCP %s:%d;branch=%s" % (IP, self.port, branch())),
            ("Max-Forwards", "70"),
            ("From", "<sip:%s@%s>;tag=%s" % (user, IP, secrets.token_hex(4))),
            ("To", "<sip:%s@%s>" % (to_user or user, IP)),
            ("Call-ID", call_id),
            ("CSeq", "%d %s" % (cseq, method)),
            ("User-Agent", "test-client"),
        ]
        headers.extend(extra or [])
        return sipmsg.build_request(method, self.uri(), headers=headers, body=body)

    async def register(self, client, user, password, cseq=1):
        call_id = "%s@test" % secrets.token_hex(4)
        contact = "<sip:%s@%s:%d;transport=tcp>" % (user, IP, client.port)

        def request(cseq_no, extra=None):
            headers = [
                ("Via", "SIP/2.0/TCP %s:%d;branch=%s" % (IP, self.port, branch())),
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
            return sipmsg.build_request("REGISTER", self.uri(), headers=headers)

        client.send(request(cseq))
        response = await client.recv()
        self.assertEqual(response.status, 401, "registrar must challenge")
        challenge = digest.parse_challenge(response.get("WWW-Authenticate").replace("Digest ", ""))
        params = dict(challenge)
        params.update({"nc": "00000001", "cnonce": "abcdef"})
        auth = digest.compute_response(user, password, "REGISTER", self.uri(), params)
        authorization = ('Digest username="%s", realm="%s", nonce="%s", uri="%s", '
                         'response="%s", algorithm=MD5, qop=auth, nc=00000001, cnonce="abcdef"'
                         % (user, challenge["realm"], challenge["nonce"], self.uri(), auth))
        client.send(request(cseq + 1, [("Authorization", authorization)]))
        return await client.recv()

    async def establish_call(self, target="1002"):
        """1001 -> 1002, answered, ACKed. Returns (call_id, callee_invite, caller_200)."""
        call_id = "c-%s" % secrets.token_hex(4)
        self.c1.send(self.build("INVITE", "1001", call_id, 1, to_user=target,
                                body=sdp.build_sdp(IP, 41000),
                                extra=[("Content-Type", "application/sdp")]))
        await self.c1.recv()                       # 100
        callee_invite = await self.c2.recv()
        ok = sipmsg.build_response(callee_invite, 200, "OK",
                                   headers=[("Contact", "<sip:1002@%s:%d>" % (IP, self.c2.port)),
                                            ("Content-Type", "application/sdp")],
                                   body=sdp.build_sdp(IP, 42000), to_tag=secrets.token_hex(3))
        self.c2.send(ok)
        caller_200 = None
        for _ in range(6):
            message = await self.c1.recv()
            if message.status == 200:
                caller_200 = message
                break
        self.assertIsNotNone(caller_200, "caller never got 200 OK")
        self.c1.send(self.build("ACK", "1001", call_id, 2, to_user=target,
                                extra=[("To", caller_200.get("To"))]))
        ack = await self.c2.recv()
        self.assertEqual(ack.method, "ACK")
        await asyncio.sleep(0.1)
        return call_id, callee_invite, caller_200

    # ------------------------------------------------------------------ tests
    async def test_tcp_register_and_options(self):
        response = await self.register(self.c1, "1001", "pass1")
        self.assertEqual(response.status, 200)
        self.assertIsNotNone(self.db.get_registration("1001"))
        self.assertIn("tcp", (self.db.get_registration("1001")["contact"] or ""))

        self.c1.send(self.build("OPTIONS", "1001", "opt-1", 1))
        response = await self.c1.recv()
        self.assertEqual(response.status, 200)
        self.assertIn("INVITE", response.get("Allow"))

    async def test_register_wrong_password(self):
        call_id = "bad@test"
        contact = "<sip:1001@%s:%d>" % (IP, self.c1.port)

        def request(cseq_no, extra=None):
            headers = [
                ("Via", "SIP/2.0/TCP %s:%d;branch=%s" % (IP, self.port, branch())),
                ("From", "<sip:1001@%s>;tag=a" % IP),
                ("To", "<sip:1001@%s>" % IP),
                ("Call-ID", call_id), ("CSeq", "%d REGISTER" % cseq_no), ("Contact", contact),
            ]
            headers.extend(extra or [])
            return sipmsg.build_request("REGISTER", self.uri(), headers=headers)

        self.c1.send(request(1))
        first = await self.c1.recv()
        challenge = digest.parse_challenge(first.get("WWW-Authenticate").replace("Digest ", ""))
        params = dict(challenge)
        params.update({"nc": "00000001", "cnonce": "abcdef"})
        bad = digest.compute_response("1001", "nope", "REGISTER", self.uri(), params)
        authorization = ('Digest username="1001", realm="%s", nonce="%s", uri="%s", '
                         'response="%s", qop=auth, nc=00000001, cnonce="abcdef"'
                         % (challenge["realm"], challenge["nonce"], self.uri(), bad))
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
        await self.register(self.c1, "1001", "pass1")
        call_id = "modem-%s" % secrets.token_hex(4)
        self.c1.send(self.build("INVITE", "1001", call_id, 1, to_user="913800138000",
                                body=sdp.build_sdp(IP, 41000),
                                extra=[("Content-Type", "application/sdp")]))
        first = await self.c1.recv()
        self.assertEqual(first.status, 100)
        final = None
        for _ in range(8):
            message = await self.c1.recv(timeout=8.0)
            if message.status == 200:
                final = message
                break
        self.assertIsNotNone(final, "modem call was never answered towards the caller")
        row = self.db.list_cdr(1)[0]
        self.assertEqual(row["to_num"], "13800138000")

    async def test_caller_display_is_bare_extension(self):
        await self.register(self.c1, "1001", "pass1")
        await self.register(self.c2, "1002", "pass2")
        call_id = "disp-%s" % secrets.token_hex(4)
        self.c1.send(self.build("INVITE", "1001", call_id, 1, to_user="1002",
                                body=sdp.build_sdp(IP, 41000),
                                extra=[("Content-Type", "application/sdp")]))
        await self.c1.recv()
        callee_invite = await self.c2.recv()
        from_header = callee_invite.get("From")
        self.assertIn("sip:1001@", from_header)
        self.assertNotIn('1002"', from_header)

    async def test_incoming_modem_call_shows_caller_number(self):
        await self.register(self.c2, "1002", "pass2")
        self.modem.simulate_incoming("13800138000")
        invite = await self.c2.recv(timeout=6)
        self.assertEqual(invite.method, "INVITE")
        self.assertIn("13800138000", invite.get("From"))
        self.assertNotIn("modem", invite.get("From").lower())
        row = self.db.list_cdr(1)[0]
        self.assertEqual(row["from_num"], "13800138000")

    async def test_ring_group_rings_all_members(self):
        self.db.create_extension("1003", "chejian", "pass3")
        self.db.update_extension(self.db.get_extension("1002")["id"], ring_group="1002&1003")
        await self.register(self.c1, "1001", "pass1")
        await self.register(self.c2, "1002", "pass2")
        c3 = await TcpClient("1003").connect(IP, self.port)
        try:
            await self.register(c3, "1003", "pass3")
            call_id = "grp-%s" % secrets.token_hex(4)
            self.c1.send(self.build("INVITE", "1001", call_id, 1, to_user="1002",
                                    body=sdp.build_sdp(IP, 41000),
                                    extra=[("Content-Type", "application/sdp")]))
            await self.c1.recv()
            invite_1002 = await self.c2.recv()
            invite_1003 = await c3.recv()
            self.assertEqual(invite_1002.method, "INVITE")
            self.assertEqual(invite_1003.method, "INVITE")
            self.assertEqual(set(self.db.ring_group_members("1002")), {"1002", "1003"})
            self.assertEqual(set(self.db.ring_group_members("1003")), {"1003", "1002"})
        finally:
            c3.close()

    async def test_ring_group_first_answer_cancels_others(self):
        self.db.create_extension("1003", "chejian", "pass3")
        self.db.update_extension(self.db.get_extension("1002")["id"], ring_group="1002&1003")
        await self.register(self.c1, "1001", "pass1")
        await self.register(self.c2, "1002", "pass2")
        c3 = await TcpClient("1003").connect(IP, self.port)
        try:
            await self.register(c3, "1003", "pass3")
            call_id = "grp2-%s" % secrets.token_hex(4)
            self.c1.send(self.build("INVITE", "1001", call_id, 1, to_user="1002",
                                    body=sdp.build_sdp(IP, 41000),
                                    extra=[("Content-Type", "application/sdp")]))
            await self.c1.recv()
            await self.c2.recv()
            inv3 = await c3.recv()
            ok = sipmsg.build_response(inv3, 200, "OK",
                                       headers=[("Contact", "<sip:1003@%s:%d>" % (IP, c3.port)),
                                                ("Content-Type", "application/sdp")],
                                       body=sdp.build_sdp(IP, 43000), to_tag=secrets.token_hex(3))
            c3.send(ok)
            methods = []
            for _ in range(4):
                try:
                    methods.append((await self.c2.recv(timeout=2)).method)
                except asyncio.TimeoutError:
                    break
            self.assertIn("CANCEL", methods, "the other member must be cancelled")
        finally:
            c3.close()

    async def test_callee_hangup_reaches_caller(self):
        await self.register(self.c1, "1001", "pass1")
        await self.register(self.c2, "1002", "pass2")
        call_id, callee_invite, caller_200 = await self.establish_call()
        self.c2.send(sipmsg.build_request("BYE", self.uri(), headers=[
            ("Via", "SIP/2.0/TCP %s:%d;branch=%s" % (IP, self.port, branch())),
            ("From", callee_invite.get("To")),
            ("To", caller_200.get("From")),
            ("Call-ID", callee_invite.call_id),
            ("CSeq", "1 BYE"),
        ]))
        bye_at_caller = await self.c1.recv(timeout=6)
        self.assertEqual(bye_at_caller.method, "BYE",
                         "callee hangup must produce a BYE towards the caller")

    async def test_panel_hangup_reaches_extension(self):
        await self.register(self.c1, "1001", "pass1")
        await self.register(self.c2, "1002", "pass2")
        await self.establish_call()
        active = list(self.sip.calls.calls.values())
        self.assertTrue(active, "call should be tracked")
        self.sip.calls.hangup(active[0].id)
        bye = await self.c1.recv(timeout=6)
        self.assertEqual(bye.method, "BYE", "panel hangup must reach the extension")

    async def test_modem_hangup_reaches_extension(self):
        await self.register(self.c2, "1002", "pass2")
        self.modem.simulate_incoming("13900002222")
        invite = await self.c2.recv(timeout=6)
        ok = sipmsg.build_response(invite, 200, "OK",
                                   headers=[("Contact", "<sip:1002@%s:%d>" % (IP, self.c2.port)),
                                            ("Content-Type", "application/sdp")],
                                   body=sdp.build_sdp(IP, 42000), to_tag=secrets.token_hex(3))
        self.c2.send(ok)
        ack = await self.c2.recv(timeout=4)
        self.assertEqual(ack.method, "ACK")
        await asyncio.sleep(0.2)
        await self.modem.hangup()
        bye = await self.c2.recv(timeout=6)
        self.assertEqual(bye.method, "BYE",
                         "modem hangup must produce a BYE towards the extension")

    async def test_modem_failure_rejects_caller(self):
        await self.register(self.c1, "1001", "pass1")
        call_id = "died-%s" % secrets.token_hex(4)
        self.c1.send(self.build("INVITE", "1001", call_id, 1, to_user="913800138000",
                                body=sdp.build_sdp(IP, 41000),
                                extra=[("Content-Type", "application/sdp")]))
        first = await self.c1.recv()
        self.assertEqual(first.status, 100)
        await asyncio.sleep(0.4)
        await self.modem.hangup()
        final = None
        for _ in range(4):
            message = await self.c1.recv(timeout=5)
            if message.status >= 300:
                final = message
                break
        self.assertIsNotNone(final, "caller must be told the call failed")
        self.assertIn(final.status, (480, 486, 487))

    async def test_extension_to_extension_call(self):
        await self.register(self.c1, "1001", "pass1")
        await self.register(self.c2, "1002", "pass2")
        call_id, callee_invite, caller_200 = await self.establish_call()
        self.assertIsNotNone(sdp.parse_sdp(callee_invite.body))
        self.assertIsNotNone(sdp.parse_sdp(caller_200.body))
        # caller hangs up: the callee must be told immediately
        self.c1.send(self.build("BYE", "1001", call_id, 3, to_user="1002"))
        bye = await self.c2.recv(timeout=6)
        self.assertEqual(bye.method, "BYE")


if __name__ == "__main__":
    unittest.main(verbosity=2)