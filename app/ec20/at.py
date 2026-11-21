"""Serial AT channel for the EC20, stdlib only (os + termios + select).

The reader runs in a thread because a raw tty read blocks; lines are handed to
the asyncio loop through call_soon_threadsafe, so the modem driver stays
single-threaded from the application's point of view.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import os
import select
import threading
import time

LOG = logging.getLogger("ec20.at")

URC_PREFIXES = (
    "RING", "+CRING", "+CLIP", "NO CARRIER", "NO ANSWER", "BUSY", "NO DIALTONE",
    "+CMTI", "+CMT", "+CDS", "+CBM", "+CIREG", "+QIND", "+CGEV", "+CREG",
    "+CEREG", "+CGREG", "+CSQ", "+QUSIM", "+CPIN", "RDY", "POWER", "+CFUN",
    "SMS DONE", "+QPCMV", "+CPCMV", "CONNECT", "VOICE CALL",
)


class AtError(Exception):
    pass


class AtTimeout(AtError):
    pass


def is_urc(line):
    upper = line.strip().upper()
    for prefix in URC_PREFIXES:
        if upper.startswith(prefix):
            return True
    return False


class AtChannel:
    def __init__(self, loop, port, baud=115200, on_urc=None):
        self.loop = loop
        self.port = port
        self.baud = baud
        self.on_urc = on_urc
        self.fd = -1
        self._thread = None
        self._running = False
        self._lock = asyncio.Lock()
        self._pending = None          # dict(future, lines, expect, echoes)
        self._prompt_future = None
        self._last_written = ""
        self.connected = False
        self.last_error = ""

    # ------------------------------------------------------------------ open
    def _configure(self):
        import termios
        attrs = termios.tcgetattr(self.fd)
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
        iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP |
                   termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON |
                   termios.IXOFF | termios.IXANY)
        oflag &= ~termios.OPOST
        lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
        cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
        cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        speed = getattr(termios, "B%d" % int(self.baud), termios.B115200)
        ispeed = ospeed = speed
        termios.tcsetattr(self.fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, ispeed, ospeed, cc])

    async def open(self, retries=3):
        for attempt in range(1, retries + 1):
            try:
                self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            except OSError as exc:
                self.last_error = "%s: %s" % (self.port, exc)
                LOG.warning("open %s failed (%s/%s): %s", self.port, attempt, retries, exc)
                await asyncio.sleep(1.0)
                continue
            try:
                self._configure()
            except Exception as exc:  # termios missing (e.g. Windows dev box)
                os.close(self.fd)
                self.fd = -1
                self.last_error = "termios unavailable: %s" % exc
                raise AtError(self.last_error)
            self._running = True
            self._thread = threading.Thread(target=self._reader, name="at-reader", daemon=True)
            self._thread.start()
            self.connected = True
            LOG.info("AT port %s open at %d baud", self.port, self.baud)
            await asyncio.sleep(0.1)
            return True
        self.connected = False
        return False

    async def close(self):
        self._running = False
        self.connected = False
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    # ---------------------------------------------------------------- reader
    def _reader(self):
        buffer = b""
        while self._running and self.fd >= 0:
            try:
                ready, _, _ = select.select([self.fd], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 4096)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                LOG.debug("read error: %s", exc)
                break
            if not chunk:
                continue
            if self._prompt_future is not None and b">" in chunk:
                chunk = chunk.replace(b">", b"")
                try:
                    self.loop.call_soon_threadsafe(self._resolve_prompt)
                except RuntimeError:
                    pass
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                line = raw.decode("latin-1", "replace").replace("\r", "").strip()
                if not line:
                    continue
                try:
                    self.loop.call_soon_threadsafe(self._handle_line, line)
                except RuntimeError:
                    return

    def _resolve_prompt(self):
        future = self._prompt_future
        self._prompt_future = None
        if future and not future.done():
            future.set_result(True)

    def _handle_line(self, line):
        pending = self._pending
        if line == self._last_written.strip() or (pending and line in pending["echoes"]):
            return
        if pending is not None and pending["future"] is not None and not pending["future"].done():
            upper = line.strip().upper()
            if upper in ("OK", "ERROR", "+CME ERROR", "+CMS ERROR") or upper.startswith(("+CME ERROR", "+CMS ERROR")):
                pending["future"].set_result((line, list(pending["lines"])))
                return
            # +CPIN/+CSQ/+CLCC/... are both URCs and command replies: while a
            # command is in flight, only hand the line to the URC handler when
            # it cannot be the reply we are waiting for.
            prefix = pending.get("prefix") or ""
            if is_urc(line) and not (prefix and upper.startswith(prefix)):
                self._dispatch_urc(line)
                return
            pending["lines"].append(line)
            return
        if is_urc(line):
            self._dispatch_urc(line)
        else:
            LOG.debug("AT unsolicited: %s", line)

    def _dispatch_urc(self, line):
        LOG.debug("URC: %s", line)
        if self.on_urc:
            try:
                self.on_urc(line)
            except Exception:  # pragma: no cover
                LOG.exception("URC handler failed")

    # ---------------------------------------------------------------- writer
    def write(self, text):
        if self.fd < 0:
            raise AtError("AT port not open")
        if not text.endswith("\r"):
            text += "\r"
        self._last_written = text
        data = text.encode("latin-1", "replace")
        try:
            os.write(self.fd, data)
        except OSError as exc:
            raise AtError("write failed: %s" % exc)

    # ------------------------------------------------------------- commands
    @staticmethod
    def _prefix_for(line):
        """Response prefix a command expects, e.g. AT+CSQ -> '+CSQ'.

        The EC20 answers AT+CPIN?/AT+CSQ/AT+CLCC with lines that are also valid
        URCs; knowing the prefix keeps them out of the URC dispatcher.
        """
        import re
        match = re.match(r"^AT\+([A-Z0-9]+)", line.strip().upper())
        if not match:
            return ""
        name = match.group(1)
        if name in ("CMGF", "CMEE", "CSCS", "CPMS", "CNMI", "QAUDMOD", "QPCMV", "CFUN", "CVHU"):
            return ""
        return "+" + name

    async def command(self, line, timeout=5.0, allow_error=False, additional_ok=()):
        """Send one AT command, return the response lines (OK/ERROR stripped)."""
        if not self.connected:
            raise AtError("AT port not open")
        async with self._lock:
            future = self.loop.create_future()
            self._pending = {"future": future, "lines": [], "echoes": {line},
                             "prefix": self._prefix_for(line)}
            try:
                self.write(line)
                try:
                    final, lines = await asyncio.wait_for(future, timeout)
                except asyncio.TimeoutError:
                    raise AtTimeout(line)
            finally:
                self._pending = None
            if final.strip().upper() != "OK" and not allow_error:
                raise AtError("%s -> %s" % (line, final))
            return lines

    async def query(self, line, timeout=5.0):
        """Return the first response line (e.g. '+CSQ: 14,99')."""
        lines = await self.command(line, timeout=timeout)
        for item in lines:
            if item.startswith("+"):
                return item
        return lines[0] if lines else ""

    async def send_pdu(self, pdu_hex, tpdu_len, timeout=60.0):
        """AT+CMGS flow: prompt, payload, ^Z, wait for +CMGS: <ref> OK."""
        if not self.connected:
            raise AtError("AT port not open")
        async with self._lock:
            self._prompt_future = self.loop.create_future()
            future = self.loop.create_future()
            try:
                self.write("AT+CMGS=%d" % int(tpdu_len))
                try:
                    await asyncio.wait_for(self._prompt_future, 10.0)
                except asyncio.TimeoutError:
                    raise AtTimeout("AT+CMGS prompt")
                self._pending = {"future": future, "lines": [], "echoes": {pdu_hex}}
                try:
                    os.write(self.fd, (pdu_hex + "\x1a").encode("latin-1"))
                except OSError as exc:
                    raise AtError("write failed: %s" % exc)
                try:
                    final, lines = await asyncio.wait_for(future, timeout)
                except asyncio.TimeoutError:
                    raise AtTimeout("AT+CMGS result")
            finally:
                self._pending = None
                self._prompt_future = None
            if final.strip().upper() != "OK":
                raise AtError("CMGS failed: %s %s" % (final, lines))
            reference = None
            for item in lines:
                if item.startswith("+CMGS:"):
                    try:
                        reference = int(item.split(":", 1)[1].strip())
                    except ValueError:
                        reference = None
            return reference

    # --------------------------------------------------------------- probes
    async def ping(self, timeout=3.0):
        try:
            await self.command("AT", timeout=timeout)
            return True
        except (AtError, AtTimeout):
            return False


class FakeAtChannel:
    """In-process AT channel used by tests and the simulator."""

    def __init__(self, loop, handler=None):
        self.loop = loop
        self.handler = handler
        self.connected = True
        self.port = "fake"
        self.sent = []
        self.on_urc = None

    async def open(self, retries=1):
        return True

    async def close(self):
        self.connected = False

    async def command(self, line, timeout=5.0, allow_error=False, additional_ok=()):
        self.sent.append(line)
        await asyncio.sleep(0)
        if self.handler:
            result = self.handler(line)
            if result is None:
                return []
            if isinstance(result, str):
                return [result]
            return list(result)
        if line.strip().upper() in ("AT", "ATE0", "AT+CMEE=2"):
            return []
        return []

    async def query(self, line, timeout=5.0):
        lines = await self.command(line, timeout=timeout)
        return lines[0] if lines else "OK"

    async def send_pdu(self, pdu_hex, tpdu_len, timeout=60.0):
        self.sent.append("AT+CMGS=%d" % tpdu_len)
        return 1

    async def ping(self, timeout=3.0):
        return True

    def emit(self, line):
        if self.on_urc:
            self.on_urc(line)