"""SIP signalling: UDP transport, registrar, and the B2BUA that bridges calls."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import socket
import time

from .. import media
from ..sip import digest, message as sipmsg, sdp, transport
from ..sip.leg import SipLeg
from ..util import norm_number, ts_str

LOG = logging.getLogger("sip")

ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS, MESSAGE, INFO"

CONTACT_RE = re.compile(r"<sip:([^@>]*)@([^>:;]+)(?::(\d+))?")


def local_ip():
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.3)
        probe.connect(("192.0.2.1", 9))
        ip = probe.getsockname()[0]
        probe.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _is_private(ip):
    if not ip:
        return False
    if ip.startswith(("10.", "127.", "169.254.")):
        return True
    if ip.startswith("192.168."):
        return True
    if ip.startswith("172."):
        try:
            second = int(ip.split(".")[1])
        except (IndexError, ValueError):
            return False
        return 16 <= second <= 31
    return False


class UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, server):
        self.server = server

    def connection_made(self, transport):
        self.server.transport = transport

    def datagram_received(self, data, addr):
        self.server.on_datagram(data, addr)

    def error_received(self, exc):  # pragma: no cover
        LOG.debug("sip socket error: %s", exc)


class Registrar:
    def __init__(self, server):
        self.server = server
        self.db = server.db

    def contact(self, number):
        row = self.db.get_registration(number)
        if not row:
            return None
        expires = int(row.get("expires") or 0)
        updated = row.get("updated_at") or ""
        try:
            stamp = time.mktime(time.strptime(updated, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            stamp = 0
        if expires and stamp and time.time() > stamp + expires:
            self.db.remove_registration(number)
            return None
        return row

    def registered(self):
        out = {}
        for row in self.db.registrations():
            number = row["number"]
            if self.contact(number):
                out[number] = self.contact(number)
        return out

    def handle(self, message, peer):
        server = self.server
        to_user = sipmsg.parse_uri(message.get("To") or "")[0]
        from_user = sipmsg.parse_uri(message.get("From") or "")[0]
        number = to_user or from_user or sipmsg.parse_uri(message.uri or "")[0]
        if not number:
            server.send_message(sipmsg.build_response(message, 400, "Bad Request"), peer)
            return
        extension = self.db.get_extension(number)
        if not extension:
            LOG.warning("REGISTER rejected 403: unknown extension %r (raw To=%r From=%r from %s)",
                        number, message.get("To"), message.get("From"), peer.ip)
            server.send_message(sipmsg.build_response(message, 403, "Forbidden"), peer)
            return
        if not int(extension.get("enabled", 1)):
            LOG.warning("REGISTER rejected 403: extension %s is disabled", number)
            server.send_message(sipmsg.build_response(message, 403, "Forbidden"), peer)
            return
        if not message.get("Authorization"):
            server.send_message(sipmsg.build_response(message, 401, "Unauthorized", headers=[
                ("WWW-Authenticate", server.auth.challenge())]), peer)
            return
        ok, reason = server.auth.verify(message, extension["password"])
        if not ok:
            LOG.info("REGISTER auth failed for %s: %s", number, reason)
            server.send_message(sipmsg.build_response(message, 401, "Unauthorized", headers=[
                ("WWW-Authenticate", server.auth.challenge(stale=(reason == "stale nonce")))]), peer)
            return

        contact_header = message.get("Contact") or ""
        expires_header = message.get("Expires")
        expires = server.config.get("sip.register_expiry", 3600)
        if expires_header and expires_header.strip().isdigit():
            expires = int(expires_header.strip())
        match = re.search(r"expires=(\d+)", contact_header)
        if match:
            expires = int(match.group(1))
        if expires <= 0:
            self.db.remove_registration(number)
            server.peer_by_user.pop(number, None)
            LOG.info("unregistered %s", number)
            server.send_message(sipmsg.build_response(message, 200, "OK", headers=[
                ("Contact", contact_header or "<sip:%s@%s>" % (number, server.media_ip)),
                ("Expires", "0")]), peer)
            return
        default_port = server.config.get("sip.port", 5060)
        contact_match = CONTACT_RE.search(contact_header)
        host, port = "", default_port
        if contact_match:
            host = contact_match.group(2)
            port = int(contact_match.group(3) or default_port)
        if not host or host in ("0.0.0.0", "::"):
            host, port = peer.ip, peer.port or default_port
        contact_uri = "<sip:%s@%s:%d;transport=%s>" % (number, host, port, peer.kind)
        self.db.upsert_registration(number, contact_uri, expires,
                                    user_agent=message.get("User-Agent", ""),
                                    transport=peer.kind, call_id=message.call_id,
                                    source_ip=peer.ip)
        server.bind_user_peer(number, peer, expires)
        LOG.info("registered %s at %s:%s via %s (expires %ss)", number, host, port,
                 peer.kind, expires)
        server.send_message(sipmsg.build_response(message, 200, "OK", headers=[
            ("Contact", contact_uri),
            ("Expires", str(expires)),
            ("Allow", ALLOW)]), peer)


class SipCall:
    """A bridged call: leg_a (SIP phone) and leg_b (phone or modem)."""

    def __init__(self, manager, direction, from_user, to_user):
        self.manager = manager
        self.id = "c" + secrets.token_hex(4)
        self.direction = direction
        self.from_user = from_user
        self.to_user = to_user
        self.started = time.time()
        self.answered_at = None
        self.ended = False
        self.leg_a = None
        self.leg_b = None
        self.modem_id = None
        self.modem_port = None
        self.bridge = None
        self.audio = None
        self.cdr_id = manager.server.db.add_cdr(ts_str(), from_user, to_user, direction)
        self._answered = asyncio.Event()
        self.ring_legs = []

    # ------------------------------------------------------------------ info
    def set_target(self, number):
        self.to_user = number
        try:
            self.manager.server.db.execute("UPDATE cdr SET to_num=? WHERE id=?", (number, self.cdr_id))
        except Exception:  # pragma: no cover - CDR is best effort
            LOG.debug("cannot update CDR target", exc_info=True)

    def to_dict(self):
        seconds = int(time.time() - (self.answered_at or self.started))
        label = "通话中" if self.answered_at else ("来电振铃" if self.direction == "in" else "拨号中")
        if self.ended:
            label = "已结束"
        return {"id": self.id, "dir": self.direction, "from": self.from_user, "to": self.to_user,
                "state": label, "duration": seconds, "answered": bool(self.answered_at)}

    # --------------------------------------------------------------- events
    def on_leg_ringing(self, leg):
        if self.ring_legs:
            # ring group: everyone rings until one answers, do not cancel on 180
            return
        if leg is self.leg_b and self.leg_a:
            self.leg_a.ringing()

    def on_leg_answered(self, leg):
        if self.ended:
            leg.send_bye()
            return
        if leg in self.ring_legs:
            if leg is self.leg_b or leg is self.leg_a:
                return                       # already the active party
            if self.answered_at is not None:
                # somebody else already picked this group call up: this phone
                # must be released, without touching the active call
                LOG.info("ring group: already answered, releasing %s", leg.user)
                leg.send_bye()
                return
            # first answer wins: stop the remaining members
            for other in self.ring_legs:
                if other is not leg and other.state in ("inviting", "ringing-remote"):
                    try:
                        other.cancel()
                    except Exception:  # pragma: no cover
                        LOG.debug("cancel failed", exc_info=True)
            if self.leg_a is None:
                # inbound EC20 call answered by an extension
                self.leg_a = leg
                self.answered_at = time.time()
                self.set_target("%s(分机%s)" % (self.from_user, leg.user))
                self._answered.set()
                self.manager.server.loop.create_task(self._connect_modem())
                return
            # extension -> ring group: bridge the caller to the answered member
            if self.leg_a.answer():
                self.leg_b = leg
                self.answered_at = time.time()
                self.set_target(leg.user)
                self._start_bridge()
            else:
                self.end("channel unavailable")
            return
        if leg is self.leg_b and self.leg_a:
            if self.leg_a.answer():
                self.answered_at = time.time()
                self._start_bridge()
            else:
                self.end("channel unavailable")

    def on_leg_failed(self, leg, status, reason):
        LOG.info("leg failed %s: %s %s", leg.display, status, reason)
        if leg in self.ring_legs:
            # one member rejected/busy: keep ringing the rest, only give up when
            # nobody is left (caller stays on the line meanwhile)
            alive = [other for other in self.ring_legs
                     if other is not leg and other.state not in ("failed", "terminated", "cancelled")]
            if not alive:
                LOG.info("ring group exhausted for call %s", self.id)
                if self.leg_a is not None and self.leg_a.state in ("invited", "ringing"):
                    self.leg_a.reject(486, "Busy Here")
                self.end("no answer")
            return
        if leg is self.leg_b and self.leg_a:
            code = 486 if status in (486, 480) else 503
            self.leg_a.reject(code, "Destination Unavailable")
            self.end("failed")
        elif self.leg_a is leg:
            self.end("failed")

    def on_leg_bye(self, leg):
        if leg is self.leg_a:
            if self.leg_b:
                self.leg_b.send_bye()
            for other in self.ring_legs:
                if other.state in ("inviting", "ringing-remote"):
                    other.cancel()
            if self.manager.is_modem_call(self):
                self.manager.server.loop.create_task(self.manager.modem_hangup())
        else:
            if self.leg_a and self.leg_a.state in ("answered", "answered-acked"):
                self.leg_a.send_bye()
        self.end("bye")

    def on_leg_cancel(self, leg):
        self.end("cancel")

    def on_dtmf(self, leg, digit):
        if self.manager.is_modem_call(self):
            self.manager.server.loop.create_task(self.manager.play_dtmf(digit))
        elif leg is self.leg_a and self.leg_b:
            self.leg_b.media.send_dtmf(digit)
        elif leg is self.leg_b and self.leg_a:
            self.leg_a.media.send_dtmf(digit)

    # --------------------------------------------------------------- bridge
    def _start_bridge(self):
        if self.bridge is not None:
            return
        loop = self.manager.server.loop
        if self.leg_b is not None:
            self.bridge = media.Bridge(loop, self.leg_a.media, self.leg_b.media).start()
        elif self.modem_port is not None:
            self.bridge = media.Bridge(loop, self.leg_a.media, self.modem_port).start()
        LOG.info("bridge up for call %s (%s -> %s)", self.id, self.from_user, self.to_user)

    async def _connect_modem(self):
        self.answered_at = self.answered_at or time.time()
        if self.manager.server.modem:
            try:
                await self.manager.server.modem.answer()
            except Exception as exc:
                LOG.warning("modem answer failed: %s", exc)
                self.end("modem answer failed")
                return
        await self.manager.start_modem_audio(self)
        self._start_bridge()

    def end(self, reason=""):
        if self.ended:
            return
        self.ended = True
        loop = self.manager.server.loop
        if self.bridge:
            loop.create_task(self.bridge.stop())
            self.bridge = None
        if self.modem_port:
            loop.create_task(self.modem_port.stop())
            self.modem_port = None
        # still-ringing group members must be cancelled, not just dropped
        for leg in self.ring_legs:
            if leg.state in ("inviting", "ringing-remote"):
                try:
                    leg.cancel()
                except Exception:  # pragma: no cover
                    LOG.debug("cancel failed", exc_info=True)
            leg.close()
        for leg in (self.leg_a, self.leg_b):
            if leg:
                if leg.state in ("answered", "answered-acked", "invited", "ringing"):
                    if leg.state in ("invited", "ringing"):
                        leg.reject(487, "Request Terminated")
                    else:
                        leg.send_bye()
                leg.close()
        duration = int(time.time() - (self.answered_at or self.started))
        self.manager.server.db.finish_cdr(self.cdr_id, "answered" if self.answered_at else reason or "missed",
                                          duration)
        self.manager.calls.pop(self.id, None)
        LOG.info("call %s ended (%s, %ds)", self.id, reason, duration)


class CallManager:
    def __init__(self, server):
        self.server = server
        self.calls = {}

    # -------------------------------------------------------------- helpers
    def display_for(self, number):
        """Canonical caller display: the extension number itself.

        Phones often carry a stale "Display Name" (e.g. every account labelled
        1002), which leaks into From and confuses everyone. We always present
        the extension number so 1001 shows as 1001 for the other party.
        """
        number = (number or "").strip()
        return number or "unknown"

    def _registered_contact(self, number):
        contact = self.server.registrar.contact(number)
        if not contact:
            return None
        match = CONTACT_RE.search(contact.get("contact", ""))
        if match:
            return match.group(2), int(match.group(3) or self.server.config.get("sip.port", 5060))
        return None

    def is_modem_call(self, call):
        return call.modem_id is not None

    async def modem_hangup(self):
        if self.server.modem:
            await self.server.modem.hangup()

    async def play_dtmf(self, digit):
        if self.server.modem:
            await self.server.modem.send_dtmf(digit)

    async def start_modem_audio(self, call):
        loop = self.server.loop
        audio = self.server.make_audio()
        call.audio = audio
        port = media.ModemPort(loop, audio)
        if not await port.start():
            LOG.warning("modem audio unavailable: %s", audio.last_error)
        call.modem_port = port

    # ------------------------------------------------------------- incoming
    def on_invite(self, message, peer):
        from_user = sipmsg.parse_uri(message.get("From"))[0]
        to_user = sipmsg.parse_uri(message.get("To"))[0] or sipmsg.parse_uri(message.uri)[0]
        extension = self.server.db.get_extension(from_user) if from_user else None
        if not extension or not int(extension.get("enabled", 1)):
            self.server.send_message(sipmsg.build_response(message, 403, "Forbidden"), peer)
            return
        if not to_user:
            self.server.send_message(sipmsg.build_response(message, 404, "Not Found"), peer)
            return
        call = SipCall(self, "in", from_user, to_user)
        self.calls[call.id] = call
        leg = SipLeg(self.server, call, "uas", user=from_user)
        call.leg_a = leg
        self.server.loop.create_task(self._setup_invite(call, leg, message, peer, to_user))

    async def _setup_invite(self, call, leg, message, peer, target):
        await leg.accept(message, peer)
        self.server.legs[leg.call_id] = leg
        prefix = str(self.server.config.get("ec20.voice_prefix", "9") or "")
        extension = self.server.db.get_extension(target)
        if extension:
            group = self.server.db.ring_group_members(target)
            live = [n for n in group if n == target and self._registered_contact(n)
                    or n != target and self._registered_contact(n)]
            if live:
                await self._call_group(call, live)
                return
        number = target
        if prefix and number.startswith(prefix):
            number = number[len(prefix):]
        number = norm_number(number)
        if not number or len(number) < 3:
            leg.reject(404, "Not Found")
            call.end("not found")
            return
        await self._call_modem(call, number)

    async def _call_group(self, call, numbers):
        """Ring several extensions at once; the first answer wins, rest cancel."""
        call.to_user = "&".join(numbers)
        call.ring_legs = []
        for number in numbers:
            contact = self._registered_contact(number)
            if not contact:
                continue
            host, port = contact
            leg = SipLeg(self.server, call, "uac", user=number)
            await leg.invite(number, host, port, from_user=call.from_user,
                             display=self.display_for(call.from_user))
            self.server.legs[leg.call_id] = leg
            call.ring_legs.append(leg)
        if not call.ring_legs:
            call.leg_a.reject(480, "Temporarily Unavailable")
            call.end("no registered member")
            return
        call.leg_a.ringing()
        LOG.info("ring group %s -> %s", call.from_user, ",".join(numbers))

    async def _call_extension(self, call, target, contact, peer):
        host, port = contact
        leg_b = SipLeg(self.server, call, "uac", user=target)
        call.leg_b = leg_b
        call.set_target(target)
        ok = await leg_b.invite(target, host, port, from_user=call.from_user,
                                display=self.display_for(call.from_user))
        # register only after invite(): invite() assigns the Call-ID
        self.server.legs[leg_b.call_id] = leg_b
        if not ok:
            call.leg_a.reject(503, "Service Unavailable")
            call.end("invite failed")
            return
        call.leg_a.ringing()

    async def _call_modem(self, call, number):
        modem = self.server.modem
        if not modem or not modem.present:
            call.leg_a.reject(503, "Modem Unavailable")
            call.end("modem unavailable")
            return
        call.set_target(number)
        call.modem_id = "pending"
        call.leg_a.ringing()
        try:
            call.modem_id = await modem.dial(number)
        except Exception as exc:
            LOG.warning("modem dial %s failed: %s", number, exc)
            call.leg_a.reject(503, "Modem Dial Failed")
            call.end("dial failed")
            return

    # -------------------------------------------------- modem-driven events
    def on_modem_call(self, event, modem_call):
        if event == "incoming":
            self.server.loop.create_task(self._ring_extensions(modem_call))
            return
        if event == "active":
            # the modem call we started for a SIP caller just got answered
            for call in list(self.calls.values()):
                if call.ended or not call.leg_a:
                    continue
                if call.modem_id not in (modem_call.id, "pending"):
                    continue
                call.modem_id = modem_call.id
                call.answered_at = call.answered_at or time.time()
                if call.leg_a.state in ("invited", "ringing"):
                    call.leg_a.answer()
                self.server.loop.create_task(self._attach_audio(call))
                return
            return
        if event == "ended":
            for call in list(self.calls.values()):
                if call.modem_id != modem_call.id:
                    continue
                leg = call.leg_a
                if leg is not None:
                    if leg.state in ("invited", "ringing"):
                        # modem call died before the extension answered
                        leg.reject(486 if modem_call.state == "ended" else 480,
                                   "Temporarily Unavailable")
                    elif leg.state in ("answered", "answered-acked"):
                        leg.send_bye()
                for other in call.ring_legs:
                    if other.state in ("inviting", "ringing-remote"):
                        other.cancel()
                call.end("modem ended")
                LOG.info("modem hangup released call %s", call.id)

    async def _attach_audio(self, call):
        await self.start_modem_audio(call)
        if not call.bridge and call.answered_at:
            call._start_bridge()

    async def _ring_extensions(self, modem_call):
        ring_seconds = int(self.server.config.get("ec20.ring_seconds", 30))
        caller = norm_number(modem_call.number) or "unknown"
        call = SipCall(self, "in", caller, "群振")
        call.modem_id = modem_call.id
        call.to_user = caller
        self.calls[call.id] = call
        dial = self.server.db.get_setting("dial", {}) or {}
        target = (dial.get("forward_all_to") or "").strip()
        if target:
            numbers = self.server.db.ring_group_members(target)
            call.to_user = "&".join(numbers)
        else:
            numbers = list(self.server.registrar.registered().keys())
        if not numbers:
            LOG.info("incoming call with no registered extension, rejecting")
            await self.server.modem.hangup()
            call.end("no extension")
            return
        legs = []
        for number in numbers:
            contact = self._registered_contact(number)
            if not contact:
                continue
            host, port = contact
            leg = SipLeg(self.server, call, "uac", user=number)
            # From carries the real caller, so the phone's screen shows the
            # mobile number instead of an internal label.
            await leg.invite(number, host, port, from_user=caller,
                             display=self.display_for(caller))
            self.server.legs[leg.call_id] = leg
            call.ring_legs.append(leg)
            legs.append(leg)
        if not legs:
            await self.server.modem.hangup()
            call.end("no contact")
            return
        try:
            await asyncio.wait_for(call._answered.wait(), ring_seconds)
        except asyncio.TimeoutError:
            LOG.info("incoming call not answered in %ss", ring_seconds)
            for leg in legs:
                leg.cancel()
            await self.server.modem.hangup()
            call.end("no answer")

    # ------------------------------------------------------------------ api
    def dial(self, number):
        modem = self.server.modem
        if not modem or not modem.present:
            raise RuntimeError("模块未就绪")
        number = norm_number(number)
        call = SipCall(self, "out", "panel", number)
        call.modem_id = "pending"
        self.calls[call.id] = call

        async def _run():
            try:
                call.modem_id = await modem.dial(number)
            except Exception as exc:
                LOG.warning("panel dial failed: %s", exc)
                call.end("dial failed")
        self.server.loop.create_task(_run())
        return call.id

    def hangup(self, call_id):
        call = self.calls.get(call_id)
        if not call:
            return False
        if call.modem_id and call.modem_id != "pending":
            self.server.loop.create_task(self.modem_hangup())
        call.end("panel hangup")
        return True

    def answer(self, call_id):
        call = self.calls.get(call_id)
        if not call:
            return False
        self.server.loop.create_task(call._connect_modem())
        return True

    def status(self):
        return [call.to_dict() for call in self.calls.values()]


class SipServer:
    def __init__(self, loop, config, db, modem=None):
        self.loop = loop
        self.config = config
        self.db = db
        self.modem = modem
        self.transport = None
        self.local_ip = local_ip()
        self.external_ip = config.get("sip.external_ip") or ""
        self.media_ip = self.external_ip or self.local_ip
        self.bind = config.get("sip.bind", "0.0.0.0")
        self.port = int(config.get("sip.port", 5060))
        self.auth = digest.DigestAuth(config.get("sip.realm", "ec20sip.local"))
        self.rtp_allocator = media.PortAllocator(config.get("sip.rtp_ports") or [])
        self.registrar = Registrar(self)
        self.calls = CallManager(self)
        self.legs = {}
        self.dialogs = {}
        self._peers = set()
        self.peer_by_user = {}
        self.tcp_listener = None
        self.started = time.time()
        self.sim_audio = False

    # ------------------------------------------------------------------ life
    async def start(self):
        self.tcp_listener = transport.TcpListener(self, self.bind, self.port)
        await self.tcp_listener.start()
        if self.config.get("sip.enable_udp", False):
            await self.loop.create_datagram_endpoint(lambda: UdpProtocol(self),
                                                     local_addr=(self.bind, self.port))
            LOG.info("SIP/UDP also enabled on %s:%s", self.bind, self.port)
        LOG.info("SIP listening on %s:%s via TCP (advertising %s)", self.bind, self.port,
                 self.media_ip)
        return True

    async def stop(self):
        for call in list(self.calls.calls.values()):
            call.end("shutdown")
        for leg in list(self.legs.values()):
            leg.close()
        self.legs.clear()
        if self.tcp_listener:
            await self.tcp_listener.stop()
            self.tcp_listener = None
        for peer in list(self._peers):
            peer.close()
        self._peers.clear()
        if self.transport:
            self.transport.close()
            self.transport = None

    def media_ip_for(self, peer_ip):
        """Address to advertise in SDP for a given peer.

        With frp/NAT configured, `media_ip` is the public address. A LAN phone
        must still be told the LAN address, otherwise its RTP leaves the local
        network and comes back through the tunnel (latency, sometimes one-way).
        """
        if not self.external_ip or not self.local_ip:
            return self.media_ip
        if self.local_ip == self.media_ip:
            return self.media_ip
        if peer_ip in ("127.0.0.1", "::1", "localhost"):
            # arrived over the frp/ssh tunnel: the far end is remote, so it must
            # be told the public address or its RTP goes to an unreachable LAN IP
            return self.media_ip
        if _is_private(peer_ip):
            return self.local_ip
        return self.media_ip

    def make_audio(self):
        from ..ec20.audio import AlsaAudio, ToneAudio, detect_device
        if self.sim_audio:
            return ToneAudio(self.loop)
        device = self.config.get("ec20.audio_device") or detect_device(
            self.config.get("ec20.audio_card_match", "EC20"))
        audio = AlsaAudio(self.loop, device, rate=int(self.config.get("ec20.audio_rate", 8000)),
                          channels=int(self.config.get("ec20.audio_channels", 1)),
                          sample=self.config.get("ec20.audio_sample", "S16_LE"))
        if not device:
            LOG.warning("no EC20 sound card, falling back to tone audio")
            return ToneAudio(self.loop)
        return audio

    # ------------------------------------------------------------------ send
    def send_message(self, message, peer):
        """Send to a transport.Peer (or a bare (ip, port) tuple for UDP)."""
        if not peer:
            return
        if not isinstance(peer, transport.Peer):
            if self.transport is None:
                return
            peer = transport.UdpPeer(self.transport, peer)
        peer.send(message.to_bytes())

    def register_peer(self, peer):
        self._peers.add(peer)

    def unregister_peer(self, peer):
        self._peers.discard(peer)
        for user, known in list(self.peer_by_user.items()):
            if known is peer:
                self.peer_by_user.pop(user, None)

    def bind_user_peer(self, user, peer, expires=0):
        """Remember the connection a registered user came in on.

        With SIP/TCP we can call that user back over the very same socket,
        which is what makes one-to-many operation behind frp work.
        """
        if user and peer is not None:
            self.peer_by_user[user] = peer
            if expires > 0:
                self.loop.call_later(expires, self._expire_peer, user, peer)

    def _expire_peer(self, user, peer):
        if self.peer_by_user.get(user) is peer and peer.closed:
            self.peer_by_user.pop(user, None)

    async def outbound_peer(self, user, host, port):
        """Reuse the user's live TCP connection, else dial a fresh one."""
        known = self.peer_by_user.get(user)
        if known is not None and not known.closed and known.kind == "tcp":
            return known
        return await transport.open_tcp_connection(host, port, self)

    # --------------------------------------------------------------- receive
    def on_datagram(self, data, addr):
        try:
            message = sipmsg.SipMessage.parse(data)
        except ValueError:
            return
        peer = transport.UdpPeer(self.transport, addr)
        try:
            self._dispatch(message, peer)
        except Exception:  # pragma: no cover - never kill the listener
            LOG.exception("failed handling %s", data[:80])

    def on_message(self, message, peer):
        """Unified entry point used by the TCP listener."""
        try:
            self._dispatch(message, peer)
        except Exception:  # pragma: no cover
            LOG.exception("failed handling %s from %s", message.method, peer)

    def _dispatch(self, message, peer):
        if message.is_request:
            self._handle_request(message, peer)
        else:
            self._handle_response(message, peer)

    def _handle_request(self, message, peer):
        method = message.method
        if method == "REGISTER":
            self.registrar.handle(message, peer)
        elif method == "INVITE":
            self.calls.on_invite(message, peer)
        elif method == "ACK":
            leg = self.legs.get(message.call_id)
            if leg:
                leg.on_ack(message)
            else:
                self.dialogs.pop(message.call_id, None)
        elif method == "BYE":
            leg = self.legs.get(message.call_id)
            if leg:
                if leg.role == "uas":
                    leg.on_bye(message)
                else:
                    leg.on_bye_uac(message)
                self.legs.pop(message.call_id, None)
            else:
                self.send_message(sipmsg.build_response(message, 481, "Call/Transaction Does Not Exist"), peer)
        elif method == "CANCEL":
            leg = self.legs.get(message.call_id)
            if leg:
                if leg.role == "uas":
                    leg.on_cancel(message)
                else:
                    leg.cancel()
                    self.send_message(sipmsg.build_response(message, 200, "OK"), peer)
            else:
                self.send_message(sipmsg.build_response(message, 481, "Call/Transaction Does Not Exist"), peer)
        elif method == "OPTIONS":
            self.send_message(sipmsg.build_response(message, 200, "OK", headers=[
                ("Allow", ALLOW), ("Accept", "application/sdp, text/plain")]), peer)
        elif method == "MESSAGE":
            self._handle_message(message, peer)
        elif method == "INFO":
            self._handle_info(message, peer)
        else:
            self.send_message(sipmsg.build_response(message, 501, "Not Implemented"), peer)

    def _handle_response(self, message, peer):
        leg = self.legs.get(message.call_id)
        if leg:
            leg.on_response(message)

    def _handle_message(self, message, peer):
        target = sipmsg.parse_uri(message.uri)[0] or sipmsg.parse_uri(message.get("To"))[0]
        text = message.body.decode("utf-8", "replace")
        if not self.modem or not self.modem.present:
            self.send_message(sipmsg.build_response(message, 503, "Service Unavailable"), peer)
            return
        number = norm_number(target)
        if not number:
            self.send_message(sipmsg.build_response(message, 400, "Bad Request"), peer)
            return

        async def _send():
            try:
                await self.modem.send_sms(number, text)
                self.send_message(sipmsg.build_response(message, 202, "Accepted"), peer)
            except Exception as exc:
                LOG.warning("SIP MESSAGE -> SMS failed: %s", exc)
                self.send_message(sipmsg.build_response(message, 500, "Server Internal Error"), peer)
        self.loop.create_task(_send())

    def _handle_info(self, message, peer):
        leg = self.legs.get(message.call_id)
        body = message.body.decode("latin-1", "replace")
        match = re.search(r"Signal=(\d+)", body) or re.search(r"([0-9*#])", body)
        if leg and match:
            digit = {"0": "0", "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6",
                     "7": "7", "8": "8", "9": "9", "10": "*", "11": "#"}.get(match.group(1), match.group(1))
            try:
                leg.call.on_dtmf(leg, digit)
            except Exception:
                LOG.exception("dtmf relay failed")
        self.send_message(sipmsg.build_response(message, 200, "OK"), peer)

    # ---------------------------------------------------------------- status
    def status(self):
        registrations = self.db.registrations()
        return {
            "listening": "%s:%d" % (self.media_ip, self.port),
            "registrations": len([r for r in registrations if self.registrar.contact(r["number"])]),
            "extensions": len(self.db.list_extensions()),
            "active_calls": len(self.calls.calls),
            "rtp": self.rtp_allocator.status(),
        }