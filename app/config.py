"""Configuration for ec20-sip-lite.

Single JSON file (/etc/ec20-sip/config.json) holds boot-time wiring (ports,
paths, device nodes). Everything an end user may want to change at runtime
(SMTP, DingTalk, dial plan, SIP realm, password) lives in the SQLite settings
table and is edited from the web panel.

Python 3.8 compatible, stdlib only.
"""
from __future__ import annotations  # noqa: F401  (keep annotations lazy on 3.8)

import copy
import json
import os
import tempfile
import threading

DEFAULTS = {
    "sip": {
        "bind": "0.0.0.0",
        "port": 5060,
        # SIP over TCP is the default: one connection per phone, so the server
        # can call the phone back (works behind frp/NAT). Set true only for
        # phones that cannot do SIP over TCP.
        "enable_udp": False,
        "realm": "ec20sip.local",
        "external_ip": "",
        # RTP media ports. Leave empty to use ephemeral ports (LAN only).
        # Set a list (e.g. [10000,10001,10003,10004]) when the box sits behind
        # frp/NAT: these must match the udp tunnels on the frp server.
        "rtp_ports": [],
        "udp_port_min": 40000,
        "udp_port_max": 40099,
        "register_expiry": 3600,
        "max_extensions": 64,
        # relay video RTP between two extensions (no transcoding). EC20 has no
        # video path, so calls to/from the modem stay audio-only.
        "video": True,
        # always answer OPTIONS so monitoring tools see us
        "answer_options": True,
    },
    "ec20": {
        "at_port": "/dev/ec20-at",
        "fallback_at_ports": ["/dev/ttyUSB2", "/dev/ttyUSB3"],
        "baud": 115200,
        # ALSA device for the module's USB audio class (UAC) interface.
        # Empty string -> auto-detect a card whose name contains audio_card_match.
        "audio_device": "",
        "audio_card_match": "EC20",
        "audio_rate": 8000,
        "audio_channels": 1,
        "audio_sample": "S16_LE",
        "qaumod": 3,
        "voice_prefix": "9",
        "ring_seconds": 30,
        "auto_answer": False,
        "poll_interval": 20,
        "init_commands": [
            "ATE0",
            "AT+CMEE=2",
            "AT+CLIP=1",
            "AT+CRC=1",
            "AT+CMGF=0",
            "AT+CSCS=\"UCS2\"",
            "AT+CPMS=\"ME\",\"ME\",\"ME\"",
            "AT+CNMI=2,1,0,0,0",
        ],
        # commands whose failure is not fatal (firmware dependent)
        "optional_commands": [
            "AT+QAUDMOD={qaumod}",
            "AT+QPCMV=0",
            "AT+QCFG=\"ims\",1",
            "AT+QCFG=\"usbnet\",1",
        ],
    },
    "web": {
        "bind": "0.0.0.0",
        "port": 8080,
        "session_ttl": 86400,
    },
    "sms": {
        "forward_enabled": True,
        "max_store": 2000,
    },
    "paths": {
        "data_dir": "/var/lib/ec20-sip",
        "log_file": "/var/log/ec20-sip.log",
        "run_dir": "/run/ec20-sip",
    },
    "debug": False,
}


def _deep_merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


class Config:
    """Dotted-path config with atomic save."""

    def __init__(self, path=None):
        self.path = path or os.environ.get("EC20SIP_CONFIG", "/etc/ec20-sip/config.json")
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        self.load()

    # ------------------------------------------------------------------ io
    def load(self):
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                return self
            if isinstance(raw, dict):
                _deep_merge(self._data, raw)
            return self

    def save(self):
        with self._lock:
            directory = os.path.dirname(os.path.abspath(self.path)) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".cfg-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, self.path)

    # --------------------------------------------------------------- access
    def get(self, dotted, default=None):
        node = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted, value):
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        return self

    def as_dict(self):
        return copy.deepcopy(self._data)

    # --------------------------------------------------------------- dirs
    def ensure_dirs(self):
        for key in ("data_dir", "run_dir"):
            try:
                os.makedirs(self.get("paths." + key), exist_ok=True)
            except OSError:
                # unprivileged run (tests): fall back next to the checkout
                self.set("paths." + key, os.path.join(os.getcwd(), "data", key))
                os.makedirs(self.get("paths." + key), exist_ok=True)
        log_dir = os.path.dirname(self.get("paths.log_file"))
        try:
            os.makedirs(log_dir, exist_ok=True)
        except OSError:
            self.set("paths.log_file", os.path.join(self.get("paths.data_dir"), "ec20-sip.log"))
        return self


def project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))