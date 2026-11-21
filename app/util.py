"""Small helpers shared by every component (stdlib only, Python 3.8 safe)."""
from __future__ import annotations

import collections
import logging
import os
import re
import time

# ---------------------------------------------------------------- time ---------


def ts_str(when=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when if when is not None else time.time()))


def human_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm%02ds" % (seconds // 60, seconds % 60)
    if seconds < 86400:
        return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dd%02dh" % (seconds // 86400, (seconds % 86400) // 3600)


# ---------------------------------------------------------------- system -------


def _read(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def meminfo_kb():
    """{'MemTotal': kb, 'MemAvailable': kb, ...}"""
    out = {}
    for line in _read("/proc/meminfo").splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        val = val.strip().split()
        if val and val[0].isdigit():
            out[key] = int(val[0])
    return out


def mem_snapshot():
    info = meminfo_kb()
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable") or info.get("MemFree", 0)
    return {"mem_total_kb": total, "mem_avail_kb": avail}


def loadavg():
    try:
        a, b, c = os.getloadavg()
        return "%.2f %.2f %.2f" % (a, b, c)
    except (OSError, AttributeError):
        return "n/a"


def disk_free_mb(path="/"):
    try:
        st = os.statvfs(path)
        return int(st.f_bavail * st.f_frsize / (1024 * 1024))
    except (OSError, AttributeError):
        try:
            import shutil
            return int(shutil.disk_usage(path).free / (1024 * 1024))
        except OSError:
            return 0


def temp_c():
    base = "/sys/class/thermal"
    try:
        zones = sorted(os.listdir(base))
    except OSError:
        return None
    for zone in zones:
        if not zone.startswith("thermal_zone"):
            continue
        raw = _read(os.path.join(base, zone, "temp")).strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 1000:
            value /= 1000.0
        if -20 < value < 150:
            return round(value, 1)
    return None


def uptime_str():
    raw = _read("/proc/uptime").split()
    try:
        return human_duration(float(raw[0]))
    except (IndexError, ValueError):
        return "n/a"


def rss_kb():
    for line in _read("/proc/self/status").splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) > 1 and parts[1].isdigit():
                return int(parts[1])
    return 0


# ---------------------------------------------------------------- logging ------


class RingHandler(logging.Handler):
    """Keeps the last N formatted records in memory for the web log view."""

    def __init__(self, capacity=600):
        super().__init__()
        self.buffer = collections.deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                                            "%Y-%m-%d %H:%M:%S"))

    def emit(self, record):
        try:
            self.buffer.append(self.format(record))
        except Exception:  # pragma: no cover - never break logging
            pass

    def tail(self, count=200):
        items = list(self.buffer)
        return items[-count:]


_RING = None


def setup_logging(level="INFO", log_file=None, debug=False):
    global _RING
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)
    if log_file:
        try:
            from logging.handlers import RotatingFileHandler
            directory = os.path.dirname(os.path.abspath(log_file))
            if directory:
                os.makedirs(directory, exist_ok=True)
            fh = RotatingFileHandler(log_file, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError:
            pass
    _RING = RingHandler()
    root.addHandler(_RING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    return root


def ring_tail(count=200):
    return _RING.tail(count) if _RING else []


# ---------------------------------------------------------------- misc ---------

_DIGITS = re.compile(r"[^0-9*#+]")


def norm_number(value):
    """Normalise a dialled number / MSISDN."""
    if value is None:
        return ""
    return _DIGITS.sub("", str(value)).strip()


def norm_peer(value):
    """Normalise an SMS peer so +8613800138000 and 13800138000 match."""
    value = norm_number(value)
    if value.startswith("0086"):
        value = "+86" + value[4:]
    elif value.startswith("86") and len(value) == 13:
        value = "+" + value
    return value


def mask_secret(text, keep=2):
    if not text:
        return ""
    text = str(text)
    if len(text) <= keep * 2:
        return "*" * len(text)
    return text[:keep] + "*" * (len(text) - keep * 2) + text[-keep:]


def to_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def truthy(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in ("", "0", "false", "no", "off", "none", "null")