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
        self.media = media.RtpPort(server.loop, on_dtmf=self._on_dtmf,
                                   allocator=getattr(server, "rtp_allocator", None))
        # address used in Via/Contact/SDP: LAN ip for LAN peers, public ip for
        # remote peers (frp). Filled in by accept()/invite().
        self.advert_ip = server.media_ip if server else ""
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
    async def accept(self, request, peer):
        """Incoming INVITE: remember the transaction and open our media port."""
        self.invite_msg = request
        self.call_id = request.call_id
        self.remote_tag = request.from_tag
        self.remote_target = peer
        self.advert_ip = self.server.media_ip_for(peer.ip)
        self.remote_sdp = sdp.parse_sdp(request.body)
        self.state = "invited"
        await self.media.open()
        self.server.send_message(sipmsg.build_response(request, 100, "Trying",
                                                       to_tag=self.local_tag), peer)
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
        body = sdp.build_sdp(self.advert_ip, self.media.port, seed=self.call_id)
        headers = [
            ("Contact", "<sip:box@%s:%d>" % (self.advert_ip, self.server.port)),
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
        LOG.info("CANCEL for %s from %s, replying 200 + 487", self.call_id, self.remote_target)
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
        self.advert_ip = self.server.media_ip_for(host)
        self.call_id = "%s@%s" % (secrets.token_hex(8), self.advert_ip)
        self.state = "inviting"
        self.remote_target = await self.server.outbound_peer(target_user, host, int(port or 5060))
        await self.media.open()
        body = sdp.build_sdp(self.advert_ip, self.media.port, seed=self.call_id)
        contact = "<sip:%s@%s:%d;transport=tcp>" % (from_user, self.advert_ip, self.server.port)
        uri = "sip:%s@%s:%d" % (target_user, host, int(port or 5060))
        # the caller's identity is the bare extension number: no stale display
        # name from the phone can leak into the other party's screen
        from_header = ("<sip:%s@%s>;tag=%s" % (from_user, self.advert_ip, self.local_tag)
                       if not display or display == from_user
                       else "\"%s\" <sip:%s@%s>;tag=%s" % (display, from_user,
                                                           self.advert_ip, self.local_tag))
        invite_branch = branch()
        self.invite_branch = invite_branch
        request = sipmsg.build_request("INVITE", uri, headers=[
            ("Via", "SIP/2.0/TCP %s:%d;branch=%s;rport" % (self.advert_ip, self.server.port, invite_branch)),
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
        # register the dialog *before* sending: a very fast 100/180 would
        # otherwise arrive before the caller had put us in the leg table, and
        # the INVITE retransmissions would never stop
        self.server.legs[self.call_id] = self
        LOG.info("INVITE %s -> %s via %s (branch %s)", target_user,
                 "%s:%d" % (host, port), self.remote_target, self.invite_branch[:12])
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
        if self._retransmit_task:
            # SIP over TCP does not need INVITE retransmission once the peer
            # answered with anything (1xx..6xx); keeping it re-rings the phone
            self._retransmit_task.cancel()
            self._retransmit_task = None
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
            ("Via", "SIP/2.0/TCP %s:%d;branch=%s" % (self.advert_ip, self.server.port, branch())),
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
        """Tear the dialog down from our side, regardless of which role we have."""
        if self.state in ("inviting", "ringing-remote"):
            self.cancel()
            return
        if self.state not in ("answered", "answered-acked"):
            return
        if self.role == "uas":
            self._send_bye_as_uas()
            return
        if self.pending_request is None:
            return
        self.local_cseq += 1
        bye = sipmsg.build_request("BYE", self.pending_request.uri if self.pending_request else
                                   "sip:%s@%s" % (self.user, self.advert_ip), headers=[
            ("Via", "SIP/2.0/TCP %s:%d;branch=%s" % (self.advert_ip, self.server.port, branch())),
            ("Max-Forwards", "70"),
            ("From", self.pending_request.get("From")),
            ("To", "<sip:%s@%s>;tag=%s" % (self.user, self.advert_ip, self.remote_tag)),
            ("Call-ID", self.call_id),
            ("CSeq", "%d BYE" % self.local_cseq),
            ("Content-Length", "0"),
        ])
        self.server.send_message(bye, self.remote_target)
        self._retransmit_until_gone(bye)
        self.state = "terminated"

    def _send_bye_as_uas(self):
        """We received the INVITE, so our tag sits in To of the 200 OK we sent.

        Building the BYE by hand is required: without it the other party keeps
        the call up until its own timeout whenever *we* end the conversation
        (callee hangs up, EC20 far end clears, panel hangup...).
        """
        if self.invite_msg is None or self.remote_target is None:
            return
        remote_from = self.invite_msg.get("From") or ""
        target_uri = sipmsg.strip_params(remote_from).strip()
        if target_uri.startswith("<") and target_uri.endswith(">"):
            target_uri = target_uri[1:-1].strip()
        if not target_uri.lower().startswith("sip"):
            target_uri = "sip:" + target_uri
        to_value = sipmsg.strip_params(remote_from).strip()
        if self.remote_tag and "tag=" not in to_value:
            to_value = "%s;tag=%s" % (to_value, self.remote_tag)
        self.local_cseq += 1
        bye = sipmsg.build_request("BYE", target_uri, headers=[
            ("Via", "SIP/2.0/TCP %s:%d;branch=%s" % (self.advert_ip, self.server.port, branch())),
            ("Max-Forwards", "70"),
            ("From", "<sip:%s@%s>;tag=%s" % (self.user or "box", self.advert_ip,
                                             self.local_tag)),
            ("To", to_value),
            ("Call-ID", self.call_id),
            ("CSeq", "%d BYE" % self.local_cseq),
            ("Content-Length", "0"),
        ])
        LOG.info("sending BYE to %s (uas side, call %s)", target_uri, self.call_id)
        self.server.send_message(bye, self.remote_target)
        self._retransmit_until_gone(bye)
        self.state = "terminated"

    def _retransmit_until_gone(self, request):
        """Repeat the BYE a couple of times; a duplicate gets 481 and is ignored."""

        async def _run():
            for delay in (0.6, 1.2):
                await asyncio.sleep(delay)
                self.server.send_message(request, self.remote_target)

        task = self.server.loop.create_task(_run())
        self._retransmit_task = task

    def cancel(self):
        """Cancel a pending INVITE.

        RFC 3261 9.1: the CANCEL must carry the *same* Via branch, From tag and
        CSeq sequence number as the INVITE it cancels. Phones silently ignore a
        CANCEL that does not match, which left the callee ringing forever.
        """
        if not self.pending_request:
            return
        if self.state not in ("inviting", "ringing-remote"):
            # already answered: tearing down needs BYE, not CANCEL
            if self.state in ("answered", "answered-acked"):
                self.send_bye()
            return
        invite_cseq = (self.pending_request.cseq or "").split()[0] or str(self.local_cseq)
        cancel = sipmsg.build_request("CANCEL", self.pending_request.uri, headers=[
            ("Via", "SIP/2.0/TCP %s:%d;branch=%s" % (self.advert_ip, self.server.port,
                                                     self.invite_branch or branch())),
            ("Max-Forwards", "70"),
            ("From", self.pending_request.get("From")),
            ("To", self.pending_request.get("To")),
            ("Call-ID", self.call_id),
            ("CSeq", "%s CANCEL" % invite_cseq),
            ("Content-Length", "0"),
        ])
        self.server.send_message(cancel, self.remote_target)
        self.state = "cancelled"

    def close(self):
        if self._retransmit_task:
            self._retransmit_task.cancel()
            self._retransmit_task = None
        self.media.close()