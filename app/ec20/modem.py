"""EC20 modem driver: AT state machine, calls, SMS, USB-audio session.

Everything here is asyncio; the serial reader thread lives in at.py.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from . import pdu
from .at import AtChannel, AtError, AtTimeout

LOG = logging.getLogger("ec20.modem")

CLCC_RE = re.compile(r"^\+CLCC:\s*(.*)$")
CLIP_RE = re.compile(r'^\+CLIP:\s*"([^"]*)"')
CMTI_RE = re.compile(r'^\+CMTI:\s*"([^"]*)",\s*(\d+)')
CREG_RE = re.compile(r"^\+C(?:E|G)?REG:\s*(\d+),(\d+)")
CSQ_RE = re.compile(r"^\+CSQ:\s*(\d+)")
CIREG_RE = re.compile(r"^\+CIREG:\s*(\d+)")

STATE_MAP = {0: "active", 1: "held", 2: "dialing", 3: "alerting", 4: "incoming", 5: "waiting"}


class Call:
    def __init__(self, direction, number, call_id=None):
        self.id = call_id or "e%d" % int(time.time() * 1000 % 1000000)
        self.dir = direction          # in | out
        self.number = number or ""
        self.state = "dialing" if direction == "out" else "incoming"
        self.started = time.time()
        self.answered_at = None
        self.audio_on = False

    @property
    def duration(self):
        if self.answered_at:
            return int(time.time() - self.answered_at)
        return 0

    def to_dict(self):
        labels = {"dialing": "拨号中", "alerting": "振铃中", "incoming": "来电", "active": "通话中",
                  "held": "保持", "ended": "已结束", "answer": "接听中"}
        return {
            "id": self.id, "dir": self.dir, "number": self.number,
            "state": labels.get(self.state, self.state), "state_raw": self.state,
            "duration": self.duration, "started": int(self.started),
        }


class Modem:
    AUDIO_ON = "AT+QPCMV=1,2"
    AUDIO_OFF = "AT+QPCMV=0"

    def __init__(self, loop, config, db, on_call=None, on_sms=None, on_state=None):
        self.loop = loop
        self.config = config
        self.db = db
        self.on_call = on_call or (lambda event, call: None)
        self.on_sms = on_sms or (lambda sms: None)
        self.on_state = on_state or (lambda: None)

        self.at = None
        self.present = False
        self.model = ""
        self.firmware = ""
        self.imei = ""
        self.iccid = ""
        self.sim_state = "UNKNOWN"
        self.csq = 0
        self.reg_status = "未注册"
        self.operator = ""
        self.ims = False
        self.voice_ready = False
        self.call = None
        self.last_error = ""
        self.last_init = 0
        self._poll_task = None
        self._call_poll_task = None
        self._assembler = {}
        self._stopping = False
        self._ring_number = ""
        self._incoming_deadline = 0

    # -------------------------------------------------------------- lifecycle
    async def start(self):
        port = self._pick_port()
        if not port:
            LOG.warning("no AT port found (module not plugged?)")
            self.present = False
            self._poll_task = self.loop.create_task(self._wait_for_port())
            return False
        self.at = AtChannel(self.loop, port, baud=self.config.get("ec20.baud", 115200),
                            on_urc=self._on_urc)
        ok = await self.at.open()
        if not ok:
            self.last_error = self.at.last_error
            self._poll_task = self.loop.create_task(self._wait_for_port())
            return False
        self.present = True
        await self.init_modem()
        self._poll_task = self.loop.create_task(self._poll_loop())
        return True

    async def stop(self):
        self._stopping = True
        for task in (self._poll_task, self._call_poll_task):
            if task:
                task.cancel()
        self._poll_task = None
        self._call_poll_task = None
        if self.at:
            try:
                await self.at.command(self.AUDIO_OFF, timeout=3, allow_error=True)
            except (AtError, AtTimeout):
                pass
            await self.at.close()
            self.at = None
        self.present = False

    def _pick_port(self):
        import os
        candidates = [self.config.get("ec20.at_port")] + list(self.config.get("ec20.fallback_at_ports", []))
        for path in candidates:
            if path and os.path.exists(path):
                return path
        return ""

    async def _wait_for_port(self):
        while not self._stopping:
            await asyncio.sleep(10)
            if self._pick_port():
                LOG.info("AT port appeared, initialising modem")
                await self.start()
                return

    async def init_modem(self):
        self.last_init = time.time()
        seq = ["AT", "ATE0"]
        for cmd in self.config.get("ec20.init_commands", []):
            seq.append(cmd)
        for cmd in seq:
            try:
                await self.at.command(cmd, timeout=5, allow_error=True)
            except (AtError, AtTimeout) as exc:
                LOG.debug("init %s failed: %s", cmd, exc)
        optional = []
        for cmd in self.config.get("ec20.optional_commands", []):
            optional.append(cmd.replace("{qaumod}", str(self.config.get("ec20.qaumod", 3))))
        for cmd in optional:
            try:
                await self.at.command(cmd, timeout=6, allow_error=True)
            except (AtError, AtTimeout) as exc:
                LOG.debug("optional %s failed: %s", cmd, exc)
        await self.refresh()
        self.on_state()
        LOG.info("modem ready: %s sim=%s reg=%s csq=%s", self.model, self.sim_state, self.reg_status, self.csq)
        return True

    # --------------------------------------------------------------- polling
    async def _poll_loop(self):
        interval = max(5, int(self.config.get("ec20.poll_interval", 20)))
        while not self._stopping:
            try:
                await asyncio.sleep(interval)
                await self.refresh()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # pragma: no cover
                LOG.debug("poll failed: %s", exc)

    async def refresh(self):
        if not self.at or not self.at.connected:
            return
        try:
            info = await self.at.command("ATI", timeout=4, allow_error=True)
            for line in info:
                text = line.strip()
                if text.upper().startswith("EC2") or text.upper().startswith("EP06"):
                    self.model = text
                if text.lower().startswith("revision:"):
                    self.firmware = text.split(":", 1)[1].strip()
        except (AtError, AtTimeout):
            pass
        try:
            cpin = await self.at.query("AT+CPIN?", timeout=4)
            self.sim_state = cpin.split(":", 1)[1].strip() if ":" in cpin else cpin
        except (AtError, AtTimeout):
            self.sim_state = "ERROR"
        try:
            csq = await self.at.query("AT+CSQ", timeout=4)
            match = CSQ_RE.match(csq)
            if match:
                self.csq = int(match.group(1))
        except (AtError, AtTimeout, ValueError):
            pass
        try:
            lines = await self.at.command("AT+CREG?", timeout=4, allow_error=True)
            for line in lines:
                match = CREG_RE.match(line)
                if match:
                    stat = int(match.group(2))
                    self.reg_status = {0: "未注册", 1: "已注册(本地)", 2: "搜索中", 3: "注册被拒",
                                       4: "未知", 5: "已注册(漫游)"}.get(stat, "状态%s" % stat)
        except (AtError, AtTimeout):
            pass
        try:
            cops = await self.at.query("AT+COPS?", timeout=4)
            match = re.search(r'\+COPS:\s*\d+,\d+,"([^"]*)"', cops)
            if match:
                self.operator = match.group(1)
        except (AtError, AtTimeout):
            pass
        if not self.imei:
            try:
                self.imei = (await self.at.query("AT+CGSN", timeout=5)).strip()
            except (AtError, AtTimeout):
                pass
        if not self.iccid:
            try:
                line = await self.at.query("AT+QCCID", timeout=5)
                self.iccid = line.split(":", 1)[1].strip() if ":" in line else line
            except (AtError, AtTimeout):
                pass
        try:
            line = await self.at.query("AT+CIREG?", timeout=4)
            match = CIREG_RE.match(line)
            self.ims = bool(match and int(match.group(1)) == 1)
        except (AtError, AtTimeout):
            self.ims = False
        self.voice_ready = self.sim_state == "READY" and self.at is not None
        self.on_state()

    # ------------------------------------------------------------------ URCs
    def _on_urc(self, line):
        upper = line.strip().upper()
        if upper == "RING" or upper.startswith("+CRING"):
            self._handle_ring()
        elif upper.startswith("+CLIP:"):
            match = CLIP_RE.match(line.strip())
            if match:
                self._ring_number = match.group(1)
        elif upper.startswith("NO CARRIER") or upper.startswith("BUSY") or upper.startswith("NO ANSWER"):
            self.loop.create_task(self._call_terminated())
        elif upper.startswith("+CMTI:"):
            match = CMTI_RE.match(line.strip())
            if match:
                index = int(match.group(2))
                self.loop.create_task(self._read_stored_sms(index))
        elif upper.startswith("+CREG:") or upper.startswith("+CEREG:"):
            match = CREG_RE.match(line.strip())
            if match:
                stat = int(match.group(2))
                self.reg_status = {0: "未注册", 1: "已注册(本地)", 2: "搜索中", 3: "注册被拒",
                                   4: "未知", 5: "已注册(漫游)"}.get(stat, "状态%s" % stat)
                self.on_state()
        elif upper.startswith("+CIREG:"):
            match = CIREG_RE.match(line.strip())
            if match:
                self.ims = int(match.group(1)) == 1
                self.on_state()

    def _handle_ring(self):
        if self.call and self.call.state in ("active", "dialing", "alerting"):
            return
        call = Call("in", self._ring_number or "unknown")
        self.call = call
        self._incoming_deadline = time.time() + int(self.config.get("ec20.ring_seconds", 30))
        LOG.info("incoming call from %s", call.number)
        self.on_call("incoming", call)
        if self.config.get("ec20.auto_answer"):
            self.loop.create_task(self.answer())

    async def _call_terminated(self):
        call = self.call
        if not call:
            return
        LOG.info("call ended: %s %s", call.number, call.state)
        call.state = "ended"
        self.call = None
        if self._call_poll_task:
            self._call_poll_task.cancel()
            self._call_poll_task = None
        try:
            await self.at.command(self.AUDIO_OFF, timeout=3, allow_error=True)
        except (AtError, AtTimeout):
            pass
        self.on_call("ended", call)

    # ----------------------------------------------------------------- calls
    async def dial(self, number):
        if not self.present or not self.at:
            raise AtError("模块未就绪")
        if self.call:
            raise AtError("已有通话进行中")
        number = str(number).strip()
        if not number:
            raise AtError("号码为空")
        call = Call("out", number)
        self.call = call
        self.on_call("dialing", call)
        try:
            await self.at.command("ATD%s;" % number, timeout=8, allow_error=True)
        except (AtError, AtTimeout) as exc:
            LOG.warning("dial failed: %s", exc)
            self.call = None
            call.state = "ended"
            self.on_call("ended", call)
            raise AtError("拨号失败: %s" % exc)
        self._call_poll_task = self.loop.create_task(self._call_poll())
        return call.id

    async def answer(self):
        if not self.call:
            raise AtError("无来电")
        try:
            await self.at.command("ATA", timeout=8, allow_error=True)
        except (AtError, AtTimeout) as exc:
            raise AtError("接听失败: %s" % exc)
        self.call.state = "active"
        self.call.answered_at = time.time()
        await self._audio_on()
        self.on_call("active", self.call)
        if not self._call_poll_task:
            self._call_poll_task = self.loop.create_task(self._call_poll())
        return True

    async def hangup(self):
        if not self.call:
            return False
        try:
            await self.at.command("ATH", timeout=6, allow_error=True)
        except (AtError, AtTimeout):
            pass
        await self._call_terminated()
        return True

    async def send_dtmf(self, digits):
        if not self.call:
            return False
        for digit in str(digits):
            try:
                await self.at.command('AT+VTS="%s"' % digit, timeout=3, allow_error=True)
            except (AtError, AtTimeout):
                return False
            await asyncio.sleep(0.12)
        return True

    async def _audio_on(self):
        if not self.call or self.call.audio_on:
            return
        try:
            await self.at.command(self.AUDIO_ON, timeout=5, allow_error=True)
            self.call.audio_on = True
            LOG.info("USB audio session started for %s", self.call.number)
        except (AtError, AtTimeout) as exc:
            LOG.warning("AT+QPCMV failed: %s", exc)

    async def _audio_off(self):
        if self.call and self.call.audio_on:
            try:
                await self.at.command(self.AUDIO_OFF, timeout=3, allow_error=True)
            except (AtError, AtTimeout):
                pass
            self.call.audio_on = False

    async def _call_poll(self):
        while self.call is not None and not self._stopping:
            try:
                await asyncio.sleep(1.0)
                if self.call is None:
                    return
                lines = await self.at.command("AT+CLCC", timeout=4, allow_error=True)
                active = None
                for line in lines:
                    match = CLCC_RE.match(line.strip())
                    if not match:
                        continue
                    fields = [f.strip() for f in match.group(1).split(",")]
                    if len(fields) < 5:
                        continue
                    try:
                        stat = int(fields[2])
                    except ValueError:
                        continue
                    active = stat
                    if not self.call.number or self.call.number == "unknown":
                        if len(fields) > 5:
                            self.call.number = fields[5].strip('"')
                if active is None:
                    if time.time() > self._incoming_deadline and self.call.state == "incoming":
                        await self.hangup()
                    continue
                state = STATE_MAP.get(active, "active")
                if state == "active" and self.call.state != "active":
                    self.call.state = "active"
                    self.call.answered_at = self.call.answered_at or time.time()
                    await self._audio_on()
                    self.on_call("active", self.call)
                elif state in ("alerting", "incoming") and self.call.state != state:
                    self.call.state = state
                    self.on_call("ringing", self.call)
                if time.time() > self._incoming_deadline and self.call.state == "incoming":
                    LOG.info("incoming call timed out")
                    await self.hangup()
            except (AtError, AtTimeout):
                continue
            except asyncio.CancelledError:
                return

    # ------------------------------------------------------------------- SMS
    async def send_sms(self, number, text):
        if not self.present or not self.at:
            raise AtError("模块未就绪")
        messages = pdu.encode_messages(number, text)
        reference = None
        for pdu_hex, tpdu_len in messages:
            reference = await self.at.send_pdu(pdu_hex, tpdu_len)
            await asyncio.sleep(0.4)
        sms_id = self.db.add_sms("out", number, text, parts=len(messages), raw=reference and str(reference) or "")
        LOG.info("SMS sent to %s (%d part)", number, len(messages))
        return sms_id

    async def _read_stored_sms(self, index):
        try:
            lines = await self.at.command("AT+CMGR=%d" % index, timeout=10, allow_error=True)
        except (AtError, AtTimeout) as exc:
            LOG.warning("CMGR %d failed: %s", index, exc)
            return
        payload = None
        for line in lines:
            text = line.strip()
            if text.startswith("+CMGR"):
                continue
            if re.fullmatch(r"[0-9A-Fa-f]{20,}", text):
                payload = text
                break
        if payload:
            await self._store_incoming(payload)
        try:
            await self.at.command("AT+CMGD=%d" % index, timeout=6, allow_error=True)
        except (AtError, AtTimeout):
            pass

    async def _store_incoming(self, pdu_hex):
        try:
            decoded = pdu.decode_pdu(pdu_hex)
        except pdu.PduError as exc:
            LOG.warning("cannot decode PDU (%s): %s", exc, pdu_hex[:40])
            return
        text = decoded["text"]
        sender = decoded["sender"]
        if decoded["parts"] > 1:
            key = (sender, decoded["ref"])
            entry = self._assembler.setdefault(key, {"total": decoded["parts"], "parts": {}, "ts": time.time()})
            entry["parts"][decoded["seq"]] = text
            if len(entry["parts"]) < entry["total"]:
                return
            text = "".join(entry["parts"][i] for i in sorted(entry["parts"]))
            self._assembler.pop(key, None)
        sms_id = self.db.add_sms("in", sender, text, parts=decoded["parts"], raw=pdu_hex)
        self.db.trim_sms(int(self.config.get("sms.max_store", 2000)))
        record = {"id": sms_id, "peer": sender, "text": text, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "parts": decoded["parts"]}
        LOG.info("SMS from %s: %s", sender, text[:40])
        self.on_sms(record)

    async def list_sms(self, limit=20):
        try:
            lines = await self.at.command("AT+CMGL=4", timeout=20, allow_error=True)
        except (AtError, AtTimeout):
            return []
        out = []
        for line in lines:
            text = line.strip()
            if re.fullmatch(r"[0-9A-Fa-f]{20,}", text):
                try:
                    out.append(pdu.decode_pdu(text))
                except pdu.PduError:
                    continue
        return out[:limit]

    # ----------------------------------------------------------------- status
    def status(self):
        signal_dbm = -113 + 2 * self.csq if self.csq else -113
        bars = max(0, min(5, (self.csq * 5) // 31)) if self.csq else 0
        calls = [self.call.to_dict()] if self.call else []
        return {
            "present": self.present,
            "model": self.model or ("EC20" if self.present else ""),
            "firmware": self.firmware,
            "imei": self.imei,
            "iccid": self.iccid,
            "sim_state": self.sim_state,
            "signal_dbm": signal_dbm,
            "signal_bars": bars,
            "csq": self.csq,
            "reg_status": self.reg_status,
            "operator": self.operator,
            "ims": self.ims,
            "voice_ready": self.voice_ready,
            "calls": calls,
            "last_error": self.last_error,
        }