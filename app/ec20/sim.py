"""Modem simulator: exercises the whole stack on a machine without an EC20."""
from __future__ import annotations

import asyncio
import logging
import random
import time

from .modem import Call

LOG = logging.getLogger("ec20.sim")


class SimModem:
    AUDIO_ON = "SIM"
    AUDIO_OFF = "SIM"

    def __init__(self, loop, config, db, on_call=None, on_sms=None, on_state=None):
        self.loop = loop
        self.config = config
        self.db = db
        self.on_call = on_call or (lambda event, call: None)
        self.on_sms = on_sms or (lambda sms: None)
        self.on_state = on_state or (lambda: None)
        self.present = False
        self.model = "EC20F (模拟)"
        self.firmware = "SIM-1.0"
        self.imei = "860000000000000"
        self.iccid = "89860000000000000000"
        self.sim_state = "READY"
        self.csq = 20
        self.reg_status = "已注册(模拟)"
        self.operator = "SIM"
        self.ims = True
        self.voice_ready = True
        self.call = None
        self.last_error = ""
        self.last_init = 0
        self._task = None
        self._auto_sms_task = None

    # ------------------------------------------------------------- lifecycle
    async def start(self):
        self.present = True
        self.voice_ready = True
        self.last_init = time.time()
        LOG.info("simulator modem started")
        self.on_state()
        if self._auto_sms_task is None:
            self._auto_sms_task = self.loop.create_task(self._auto_sms())
        return True

    async def stop(self):
        self.present = False
        for task in (self._task, self._auto_sms_task):
            if task:
                task.cancel()
        self._task = None
        self._auto_sms_task = None

    async def init_modem(self):
        self.last_init = time.time()
        self.present = True
        self.on_state()
        return True

    async def refresh(self):
        if self.present:
            self.csq = max(5, min(31, self.csq + random.randint(-1, 1)))
        self.on_state()

    # ----------------------------------------------------------------- calls
    async def dial(self, number):
        if self.call:
            raise RuntimeError("已有通话")
        call = Call("out", str(number))
        self.call = call
        self.on_call("dialing", call)

        async def _progress():
            await asyncio.sleep(1.5)
            if self.call is call:
                call.state = "active"
                call.answered_at = time.time()
                self.on_call("active", call)
        self._task = self.loop.create_task(_progress())
        return call.id

    async def answer(self):
        if not self.call:
            raise RuntimeError("无来电")
        self.call.state = "active"
        self.call.answered_at = time.time()
        self.on_call("active", self.call)
        return True

    async def hangup(self):
        if not self.call:
            return False
        call = self.call
        call.state = "ended"
        self.call = None
        self.on_call("ended", call)
        return True

    async def send_dtmf(self, digits):
        LOG.info("sim dtmf: %s", digits)
        return True

    def simulate_incoming(self, number="13800138000"):
        call = Call("in", number)
        self.call = call
        self.on_call("incoming", call)
        return call.id

    # ------------------------------------------------------------------- SMS
    async def send_sms(self, number, text):
        sms_id = self.db.add_sms("out", number, text, parts=1, raw="sim")
        LOG.info("sim SMS to %s: %s", number, text)
        return sms_id

    async def list_sms(self, limit=20):
        return []

    def simulate_sms(self, peer="13800138000", text="模拟短信：您的验证码是 123456"):
        sms_id = self.db.add_sms("in", peer, text, parts=1, raw="sim")
        record = {"id": sms_id, "peer": peer, "text": text,
                  "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "parts": 1}
        self.on_sms(record)
        return record

    async def _auto_sms(self):
        """One demo SMS after 40 s so the panel has something to show."""
        await asyncio.sleep(40)
        if self.present:
            try:
                self.simulate_sms("10000", "模拟短信：欢迎使用 EC20 SIP 网关")
            except Exception:  # pragma: no cover
                pass

    # ---------------------------------------------------------------- status
    def status(self):
        signal_dbm = -113 + 2 * self.csq
        bars = max(0, min(5, (self.csq * 5) // 31))
        return {
            "present": self.present, "model": self.model, "firmware": self.firmware,
            "imei": self.imei, "iccid": self.iccid, "sim_state": self.sim_state,
            "signal_dbm": signal_dbm, "signal_bars": bars, "csq": self.csq,
            "reg_status": self.reg_status, "operator": self.operator,
            "ims": self.ims, "voice_ready": self.voice_ready,
            "calls": [self.call.to_dict()] if self.call else [],
            "last_error": self.last_error,
        }