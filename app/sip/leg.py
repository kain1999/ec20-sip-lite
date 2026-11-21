"""One SIP dialog (leg) of a bridged call: UAS towards a phone, UAC towards the callee."""
from __future__ import annotations

import asyncio
import logging
import random
import secrets

from .. import media
from ..sip import message as sipmsg
from ..sip import sdp

LOG = logging.getLogger("sip.leg")

ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS, MESSAGE, INFO, NOTIFY, REFER"


def branch():
    return "z9hG4bK" + secrets.token_hex(8)


def new_tag():
    return secrets.token_hex(6)


class SipLeg:
    def __init__(self, server, call, role, user=""):
        self.server = server
        self.call = call
        self.role = role                     # "uas" | "uac"
        self.user = user
        self.call_id = ""
        self.local_tag = new_tag()
        self.remote_tag = ""
        self.local_cseq = random.randint(10, 500)
        self.remote_target = None            # (ip, port)
        self.remote_sdp = None
        self.codec = "PCMU"
        self.state = "init"
        self.invite_msg = None
        self.pending_request = None
        self.media = media.RtpPort(server.loop, on_dtmf=self._on_dtmf)
        self._retransmit_task = None
        self._answered_200 = None
        self.last_error = ""

    # ----------------------------------------------------------------- utils
    @property
    def display(self):
        return self.user or "?"

    def _on_dtmf(self, digit, end):
        if not end:
            return
        digits = {0: "0", 1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7",
                  8: "8", 9: "9", 10: "*", 11: "#"}.get(digit)
        if digits:
            try:
                self.call.on_dtmf(self, digits)
            except Exception:  # pragma: no cover
                LOG.exception("dtmf handler failed")

    # ------------------------------------------------------------------- UAS
    async def accept(self, request, addr):
        """Incoming INVITE: remember the transaction and open our media port."""
        self.invite_msg = request
        self.call_id = request.call_id
        self.remote_tag = request.from_tag
        self.remote_target = addr
        self.remote_sdp = sdp.parse_sdp(request.body)
        self.state = "invited"
        await self.media.open()
        self.server.send_message(sipmsg.build_response(request, 100, "Trying",
                                                       to_tag=self.local_tag), addr)
        return True

    def try_tag(self):
        try:
            return int((self.invite_msg.cseq or "0").split()[0])
        except (ValueError, IndexError):
            return 0

    def ringing(self):
        if self.state != "invited":
            return
        self.server.send_message(sipmsg.build_response(self.invite_msg, 180, "Ringing",
                                                       to_tag=self.local_tag), self.remote_target)
        self.state = "ringing"

    def answer(self):
        if self.state not in ("invited", "ringing"):
            return False
        body = sdp.build_sdp(self.server.media_ip, self.media.port, seed=self.call_id)
        headers = [
            ("Contact", "<sip:box@%s:%d>" % (self.server.media_ip, self.server.port)),
            ("Content-Type", "application/sdp"),
            ("Allow", ALLOW),
        ]
        response = sipmsg.build_response(self.invite_msg, 200, "OK", headers=headers,
                                         body=body, to_tag=self.local_tag)
        self._answered_200 = response
        self.server.send_message(response, self.remote_target)
        self.state = "answered"
        if self.remote_sdp:
            codec = self.remote_sdp.get("codec") or "PCMU"
            self.codec = codec
            self.media.set_remote(self.remote_sdp["ip"], self.remote_sdp["port"], codec)
        self._start_retransmit_200()
        return True

    def _start_retransmit_200(self):
        async def _run():
            for delay in (0.5, 1.0, 2.0, 4.0):
                await asyncio.sleep(delay)
                if self.state != "answered-acked" and self._answered_200 is not None:
                    self.server.send_message(self._answered_200, self.remote_target)
                else:
                    return
        self._retransmit_task = self.server.loop.create_task(_run())

    def reject(self, code=486, reason="Busy Here"):
        if not self.invite_msg:
            return
        self.server.send_message(sipmsg.build_response(self.invite_msg, code, reason,
                                                       to_tag=self.local_tag), self.remote_target)
        self.state = "terminated"

    def on_ack(self, message):
        self.state = "answered-acked"
        if self._retransmit_task:
            self._retransmit_task.cancel()
            self._retransmit_task = None
        self.server.dialogs.pop((self.call_id, self.local_tag + self.remote_tag), None)
        self.server.dialogs[(self.call_id, self.remote_tag + self.local_tag)] = self

    def on_bye(self, message):
        self.server.send_message(sipmsg.build_response(message, 200, "OK", to_tag=self.local_tag),
                                 self.remote_target)
        self.state = "terminated"
        try:
            self.call.on_leg_bye(self)
        except Exception:  # pragma: no cover
            LOG.exception("bye handler failed")

    def on_cancel(self, message):
        self.server.send_message(sipmsg.build_response(message, 200, "OK", to_tag=self.local_tag),
                                 self.remote_target)
        if self.invite_msg:
            self.server.send_message(sipmsg.build_response(self.invite_msg, 487, "Request Terminated",
                                                           to_tag=self.local_tag), self.remote_target)
        self.state = "terminated"
        try:
            self.call.on_leg_cancel(self)
        except Exception:  # pragma: no cover
            LOG.exception("cancel handler failed")

    # ------------------------------------------------------------------- UAC
    async def invite(self, target_user, host, port, from_user="box", display="EC20 Gateway"):
        self.call_id = "%s@%s" % (secrets.token_hex(8), self.server.media_ip)
        self.state = "inviting"
        self.remote_target = (host, int(port or 5060))
        await self.media.open()
        body = sdp.build_sdp(self.server.media_ip, self.media.port, seed=self.call_id)
        contact = "<sip:%s@%s:%d>" % (from_user, self.server.media_ip, self.server.port)
        uri = "sip:%s@%s:%d" % (target_user, host, int(port or 5060))
        # the caller's identity is the bare extension number: no stale display
        # name from the phone can leak into the other party's screen
        from_header = ("<sip:%s@%s>;tag=%s" % (from_user, self.server.media_ip, self.local_tag)
                       if not display or display == from_user
                       else "\"%s\" <sip:%s@%s>;tag=%s" % (display, from_user,
                                                           self.server.media_ip, self.local_tag))
        request = sipmsg.build_request("INVITE", uri, headers=[
            ("Via", "SIP/2.0/UDP %s:%d;branch=%s;rport" % (self.server.media_ip, self.server.port, branch())),
            ("Max-Forwards", "70"),
            ("From", from_header),
            ("To", "<sip:%s@%s>" % (target_user, host)),
            ("Call-ID", self.call_id),
            ("CSeq", "%d INVITE" % self.local_cseq),
            ("Contact", contact),
            ("Allow", ALLOW),
            ("Supported", "replaces, 100rel"),
            ("User-Agent", "ec20-sip-lite/1.0"),
            ("Content-Type", "application/sdp"),
        ], body=body)
        self.pending_request = request
        self.server.send_message(request, self.remote_target)
        self._schedule_invite_retransmit()
        return True

    def _schedule_invite_retransmit(self):
        async def _run():
            for delay in (0.5, 1.0, 2.0, 4.0):
                await asyncio.sleep(delay)
                if self.state in ("inviting", "ringing-remote") and self.pending_request is not None:
                    self.server.send_message(self.pending_request, self.remote_target)
                else:
                    return
        self._retransmit_task = self.server.loop.create_task(_run())

    def on_response(self, message):
        status = message.status or 0
        if status < 200:
            if self.remote_tag == "":
                self.remote_tag = sipmsg.tag_of(message.get("To"))
            if status == 180:
                self.state = "ringing-remote"
            try:
                self.call.on_leg_ringing(self)
            except Exception:  # pragma: no cover
                LOG.exception("ringing handler failed")
            return
        if status in (200, 202):
            self.remote_tag = sipmsg.tag_of(message.get("To")) or self.remote_tag
            self.state = "answered"
            self.remote_sdp = sdp.parse_sdp(message.body)
            if self.remote_sdp:
                codec = self.remote_sdp.get("codec") or "PCMU"
                self.codec = codec
                self.media.set_remote(self.remote_sdp["ip"], self.remote_sdp["port"], codec)
            self._send_ack(message)
            try:
                self.call.on_leg_answered(self)
            except Exception:  # pragma: no cover
                LOG.exception("answered handler failed")
            return
        if status >= 300:
            self.state = "failed"
            self.last_error = "%d %s" % (status, message.reason)
            self._send_ack(message)
            try:
                self.call.on_leg_failed(self, status, message.reason)
            except Exception:  # pragma: no cover
                LOG.exception("failed handler failed")

    def _send_ack(self, response):
        ack = sipmsg.build_request("ACK", self.pending_request.uri if self.pending_request else "",
                                   headers=[
            ("Via", "SIP/2.0/UDP %s:%d;branch=%s" % (self.server.media_ip, self.server.port, branch())),
            ("Max-Forwards", "70"),
            ("From", self.pending_request.get("From")),
            ("To", response.get("To")),
            ("Call-ID", self.call_id),
            ("CSeq", "%d ACK" % self.local_cseq),
            ("Content-Length", "0"),
        ])
        self.server.send_message(ack, self.remote_target)

    def on_bye_uac(self, message):
        self.server.send_message(sipmsg.build_response(message, 200, "OK", to_tag=self.remote_tag),
                                 self.remote_target)
        self.state = "terminated"
        try:
            self.call.on_leg_bye(self)
        except Exception:  # pragma: no cover
            LOG.exception("bye handler failed")

    def send_bye(self):
        if self.state not in ("answered", "ringing-remote", "inviting", "ringing"):
            return
        if self.state in ("inviting", "ringing-remote"):
            self.cancel()
            return
        if self.role == "uas":
            # we are the UAS: reply-side teardown is driven by the peer's BYE
            return
        if self.pending_request is None:
            return
        self.local_cseq += 1
        bye = sipmsg.build_request("BYE", self.pending_request.uri if self.pending_request else
                                   "sip:%s@%s" % (self.user, self.server.media_ip), headers=[
            ("Via", "SIP/2.0/UDP %s:%d;branch=%s" % (self.server.media_ip, self.server.port, branch())),
            ("Max-Forwards", "70"),
            ("From", self.pending_request.get("From")),
            ("To", "<sip:%s@%s>;tag=%s" % (self.user, self.server.media_ip, self.remote_tag)),
            ("Call-ID", self.call_id),
            ("CSeq", "%d BYE" % self.local_cseq),
            ("Content-Length", "0"),
        ])
        self.server.send_message(bye, self.remote_target)
        self.state = "terminated"

    def cancel(self):
        if not self.pending_request:
            return
        self.local_cseq += 1
        cancel = sipmsg.build_request("CANCEL", self.pending_request.uri, headers=[
            ("Via", "SIP/2.0/UDP %s:%d;branch=%s" % (self.server.media_ip, self.server.port, branch())),
            ("Max-Forwards", "70"),
            ("From", self.pending_request.get("From")),
            ("To", self.pending_request.get("To")),
            ("Call-ID", self.call_id),
            ("CSeq", "%d CANCEL" % self.local_cseq),
            ("Content-Length", "0"),
        ])
        self.server.send_message(cancel, self.remote_target)
        self.state = "cancelled"

    def close(self):
        if self._retransmit_task:
            self._retransmit_task.cancel()
            self._retransmit_task = None
        self.media.close()