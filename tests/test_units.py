"""Unit tests: codecs, PDU, SIP message, digest, SDP, notify rules."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import g711                                  # noqa: E402
from app.ec20 import pdu                              # noqa: E402
from app.notify import _matches                        # noqa: E402
from app.sip import digest, message as sipmsg, sdp     # noqa: E402


def bcd(number):
    return pdu.bcd_number(number)


def build_deliver(sender, text, ucs2=True, udh=None):
    smsc = "00"
    if udh:
        pdu_type = "44"     # SMS-DELIVER with UDHI
        ud = udh + (text.encode("utf-16-be") if ucs2 else pdu.pack_septets(text, start_bits=0))
        if not ucs2:
            body = pdu.pack_septets(text, start_bits=(1 + len(udh)) * 8 + 1)
            ud = udh + body
            udl = 7 + pdu.septets_needed(text)
        else:
            udl = len(ud)
    else:
        pdu_type = "04"
        if ucs2:
            ud = text.encode("utf-16-be")
            udl = len(ud)
        else:
            ud = pdu.pack_septets(text)
            udl = pdu.septets_needed(text)
    oa = bcd(sender)
    scts = "62102123341180"   # 2026-01-12 23:43:11 (semi-octet BCD)
    return smsc + pdu_type + oa + "00" + ("08" if ucs2 else "00") + scts + "%02X" % udl + ud.hex().upper()


class TestG711(unittest.TestCase):
    def test_ulaw_roundtrip(self):
        for sample in (-30000, -1000, -1, 0, 1, 1000, 30000):
            decoded = g711.ulaw2lin(g711.lin2ulaw(sample))
            self.assertLess(abs(decoded - sample), 200, (sample, decoded))

    def test_alaw_roundtrip(self):
        # A-law is logarithmic: relative error at the top segment is ~6% of the
        # sample value by definition, so compare against a percentage bound.
        for sample in (-30000, -1000, -1, 0, 1, 1000, 30000):
            decoded = g711.alaw2lin(g711.lin2alaw(sample))
            tolerance = max(600, abs(sample) // 12)
            self.assertLess(abs(decoded - sample), tolerance, (sample, decoded))

    def test_encode_decode_bytes(self):
        import struct
        pcm = b"".join(struct.pack("<h", int(3000 * (1 if i % 2 else -1))) for i in range(160))
        encoded = g711.encode("PCMU", pcm)
        self.assertEqual(len(encoded), 160)
        decoded = g711.decode("PCMU", encoded)
        self.assertEqual(len(decoded), 320)


class TestPdu(unittest.TestCase):
    def test_decode_ucs2(self):
        pdu_hex = build_deliver("+8613800138000", "您的验证码是123456")
        decoded = pdu.decode_pdu(pdu_hex)
        self.assertEqual(decoded["text"], "您的验证码是123456")
        self.assertEqual(decoded["sender"], "+8613800138000")

    def test_decode_gsm7(self):
        pdu_hex = build_deliver("+8613800138001", "test 123", ucs2=False)
        decoded = pdu.decode_pdu(pdu_hex)
        self.assertEqual(decoded["text"], "test 123")

    def test_decode_concat(self):
        udh = bytes([0x05, 0x00, 0x03, 0x42, 0x02, 0x01])
        pdu_hex = build_deliver("+8613800138002", "第一部分", udh=udh)
        decoded = pdu.decode_pdu(pdu_hex)
        self.assertEqual(decoded["parts"], 2)
        self.assertEqual(decoded["seq"], 1)
        self.assertEqual(decoded["text"], "第一部分")

    def test_encode_submit(self):
        messages = pdu.encode_messages("+8613800138000", "hello")
        self.assertEqual(len(messages), 1)
        hex_pdu, octets = messages[0]
        self.assertTrue(hex_pdu.startswith("00"))
        self.assertGreater(octets, 10)

    def test_encode_long_ucs2(self):
        text = "测" * 80
        messages = pdu.encode_messages("+8613800138000", text)
        self.assertGreaterEqual(len(messages), 2)


class TestSipMessage(unittest.TestCase):
    def test_parse_request(self):
        raw = (b"INVITE sip:1002@192.168.1.188 SIP/2.0\r\n"
               b"Via: SIP/2.0/UDP 192.168.1.50:5060;branch=z9hG4bKabc\r\n"
               b"From: <sip:1001@example>;tag=1\r\nTo: <sip:1002@example>\r\n"
               b"Call-ID: abc@x\r\nCSeq: 1 INVITE\r\n"
               b"Content-Type: application/sdp\r\nContent-Length: 4\r\n\r\nv=0\r\n")
        msg = sipmsg.SipMessage.parse(raw)
        self.assertTrue(msg.is_request)
        self.assertEqual(msg.method, "INVITE")
        self.assertEqual(msg.uri, "sip:1002@192.168.1.188")
        self.assertEqual(msg.call_id, "abc@x")
        self.assertEqual(msg.from_tag, "1")
        self.assertEqual(msg.body, b"v=0\r")

    def test_build_response(self):
        raw = (b"REGISTER sip:x SIP/2.0\r\nVia: SIP/2.0/UDP 1.2.3.4:5060;branch=z9hG4bK1\r\n"
               b"From: <sip:1001@x>;tag=a\r\nTo: <sip:1001@x>\r\nCall-ID: c1\r\n"
               b"CSeq: 1 REGISTER\r\nContent-Length: 0\r\n\r\n")
        request = sipmsg.SipMessage.parse(raw)
        response = sipmsg.build_response(request, 401, "Unauthorized", to_tag="zzz")
        self.assertFalse(response.is_request)
        self.assertEqual(response.status, 401)
        self.assertIn("tag=zzz", response.get("To"))
        self.assertEqual(response.get_all("Via"), request.get_all("Via"))

    def test_parse_uri(self):
        self.assertEqual(sipmsg.parse_uri("<sip:1001@192.168.1.5:5060>"), ("1001", "192.168.1.5", 5060))
        self.assertEqual(sipmsg.parse_uri("sip:1001@host"), ("1001", "host", 0))

    def test_parse_uri_display_name_variants(self):
        # phones disagree about the space between display name and brackets
        expected = ("1003", "192.168.1.188", 0)
        self.assertEqual(sipmsg.parse_uri('"1002"<sip:1003@192.168.1.188>'), expected)
        self.assertEqual(sipmsg.parse_uri('"1002" <sip:1003@192.168.1.188>'), expected)
        self.assertEqual(sipmsg.parse_uri('1002<sip:1003@192.168.1.188>'), expected)
        self.assertEqual(sipmsg.parse_uri('<sip:1003@192.168.1.188;transport=udp>;tag=abc'), expected)
        self.assertEqual(sipmsg.parse_uri('"张三"<sip:1001@h:5060>'), ("1001", "h", 5060))


class TestDigest(unittest.TestCase):
    def test_challenge_and_verify(self):
        auth = digest.DigestAuth("ec20sip.local")
        challenge = auth.challenge()
        self.assertIn('realm="ec20sip.local"', challenge)
        params = digest.parse_challenge(challenge.replace("Digest ", ""))
        response = digest.compute_response("1001", "secret", "REGISTER", "sip:x", params)
        self.assertEqual(len(response), 32)

    def test_bad_password_rejected(self):
        auth = digest.DigestAuth("r")
        challenge = auth.challenge()
        params = digest.parse_challenge(challenge.replace("Digest ", ""))
        good = digest.compute_response("1001", "secret", "REGISTER", "sip:x", params)
        bad = digest.compute_response("1001", "wrong", "REGISTER", "sip:x", params)
        self.assertNotEqual(good, bad)


class TestSdp(unittest.TestCase):
    def test_roundtrip(self):
        body = sdp.build_sdp("192.168.1.188", 40000)
        parsed = sdp.parse_sdp(body)
        self.assertEqual(parsed["ip"], "192.168.1.188")
        self.assertEqual(parsed["port"], 40000)
        self.assertEqual(parsed["codec"], "PCMU")

    def test_parse_pcma(self):
        body = ("v=0\r\no=- 1 1 IN IP4 10.0.0.1\r\ns=-\r\nc=IN IP4 10.0.0.2\r\n"
                "t=0 0\r\nm=audio 5004 RTP/AVP 8 0\r\na=rtpmap:8 PCMA/8000\r\n")
        parsed = sdp.parse_sdp(body)
        self.assertEqual(parsed["codec"], "PCMA")
        self.assertEqual(parsed["port"], 5004)


class TestNotifyRules(unittest.TestCase):
    def test_keyword(self):
        rule = {"match_type": "keyword", "pattern": "验证码,快递"}
        self.assertTrue(_matches(rule, {"text": "您的验证码是123"}))
        self.assertFalse(_matches(rule, {"text": "广告"}))

    def test_sender(self):
        rule = {"match_type": "sender", "pattern": "10000"}
        self.assertTrue(_matches(rule, {"peer": "+8610000"}))
        self.assertFalse(_matches(rule, {"peer": "+8613800138000"}))

    def test_all(self):
        self.assertTrue(_matches({"match_type": "all", "pattern": ""}, {"text": "x"}))


class TestMediaAddress(unittest.TestCase):
    """SDP address selection: LAN peers get the LAN IP, tunnel peers the public IP."""

    def test_loopback_means_tunnel(self):
        from app.sip.server import SipServer

        class Fake:
            external_ip = "8.137.213.214"
            local_ip = "192.168.1.188"
            media_ip = "8.137.213.214"
        fn = SipServer.media_ip_for
        self.assertEqual(fn(Fake, "127.0.0.1"), "8.137.213.214")
        self.assertEqual(fn(Fake, "192.168.1.50"), "192.168.1.188")
        self.assertEqual(fn(Fake, "10.1.2.3"), "192.168.1.188")
        self.assertEqual(fn(Fake, "203.0.113.9"), "8.137.213.214")

    def test_no_external_ip(self):
        from app.sip.server import SipServer

        class Fake:
            external_ip = ""
            local_ip = "192.168.1.188"
            media_ip = "192.168.1.188"
        self.assertEqual(SipServer.media_ip_for(Fake, "192.168.1.50"), "192.168.1.188")


if __name__ == "__main__":
    unittest.main(verbosity=2)