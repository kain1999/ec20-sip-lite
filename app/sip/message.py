"""Minimal SIP message model: parse, inspect, build (RFC 3261 subset)."""
from __future__ import annotations

import re

COMPACT = {
    "i": "call-id", "m": "contact", "e": "content-encoding", "l": "content-length",
    "c": "content-type", "f": "from", "s": "subject", "k": "supported", "t": "to",
    "v": "via", "o": "event", "r": "refer-to", "b": "referred-by", "x": "session-expires",
    "u": "allow-events", "y": "identity", "n": "identity-info",
}

_HEADER_RE = re.compile(r"^([A-Za-z0-9\-\._!%*+`'~]+)\s*:\s*(.*)$")
_REQ_RE = re.compile(r"^([A-Za-z]+)\s+(\S+)\s+SIP/2\.0$")
_RESP_RE = re.compile(r"^SIP/2\.0\s+(\d{3})\s*(.*)$")


def _norm(name):
    name = name.strip().lower()
    return COMPACT.get(name, name)


def tag_of(value):
    if not value:
        return ""
    m = re.search(r";\s*tag=([^;>\s,]+)", value, re.I)
    return m.group(1) if m else ""


def param_of(value, name, default=""):
    if not value:
        return default
    m = re.search(r";\s*%s=([^;>\s,]+)" % re.escape(name), value, re.I)
    return m.group(1) if m else default


def strip_params(value):
    if not value:
        return ""
    out = []
    depth = 0
    for ch in value:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == ";" and depth == 0:
            break
        out.append(ch)
    return "".join(out).strip()


def parse_uri(value):
    """Return (user, host, port) from a SIP URI, tolerating angle brackets."""
    if not value:
        return "", "", 0
    value = strip_params(value).strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    if value.lower().startswith("sip:"):
        value = value[4:]
    elif value.lower().startswith("sips:"):
        value = value[5:]
    user = ""
    if "@" in value:
        user, value = value.split("@", 1)
    host, port = value, 0
    if ":" in value:
        host, port_s = value.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            port = 0
    return user, host, port


class SipMessage:
    __slots__ = ("is_request", "method", "uri", "version", "status", "reason", "headers", "body")

    def __init__(self):
        self.is_request = True
        self.method = None
        self.uri = None
        self.version = "SIP/2.0"
        self.status = None
        self.reason = ""
        self.headers = []  # list of [name, value]
        self.body = b""

    # ------------------------------------------------------------- parsing
    @classmethod
    def parse(cls, data):
        if isinstance(data, str):
            data = data.encode("latin-1")
        head, sep, body = data.partition(b"\r\n\r\n")
        if not sep:
            head, _, body = data.partition(b"\n\n")
        lines = head.decode("latin-1", "replace").replace("\r\n", "\n").split("\n")
        if not lines or not lines[0].strip():
            raise ValueError("empty SIP message")
        msg = cls()
        first = lines[0].strip()
        match = _REQ_RE.match(first)
        if match:
            msg.is_request = True
            msg.method = match.group(1).upper()
            msg.uri = match.group(2)
        else:
            match = _RESP_RE.match(first)
            if not match:
                raise ValueError("bad SIP start line: %r" % first)
            msg.is_request = False
            msg.status = int(match.group(1))
            msg.reason = match.group(2).strip()
        # unfold headers
        raw_headers = []
        for line in lines[1:]:
            if line[:1] in (" ", "\t") and raw_headers:
                raw_headers[-1] += " " + line.strip()
                continue
            if line.strip():
                raw_headers.append(line)
        for line in raw_headers:
            m = _HEADER_RE.match(line)
            if not m:
                continue
            msg.headers.append([m.group(1), m.group(2).strip()])
        msg.body = body
        clen = msg.get("Content-Length")
        if clen is not None and clen.strip().isdigit():
            want = int(clen.strip())
            if len(msg.body) > want:
                msg.body = msg.body[:want]
        return msg

    # ------------------------------------------------------------ accessors
    def get(self, name, default=None):
        key = _norm(name)
        for k, v in self.headers:
            if _norm(k) == key:
                return v
        return default

    def get_all(self, name):
        key = _norm(name)
        return [v for k, v in self.headers if _norm(k) == key]

    def set(self, name, value):
        key = _norm(name)
        for idx, (k, _) in enumerate(self.headers):
            if _norm(k) == key:
                self.headers[idx] = [name, value]
                del_idx = [i for i, (k2, _) in enumerate(self.headers) if _norm(k2) == key][1:]
                for i in reversed(del_idx):
                    del self.headers[i]
                return
        self.headers.append([name, value])

    def add(self, name, value):
        self.headers.append([name, value])

    def remove(self, name):
        key = _norm(name)
        self.headers = [[k, v] for k, v in self.headers if _norm(k) != key]

    def to_bytes(self):
        if self.is_request:
            first = "%s %s %s" % (self.method, self.uri, self.version)
        else:
            first = "%s %d %s" % (self.version, int(self.status or 0), self.reason)
        if self.get("Content-Length") is None:
            self.set("Content-Length", str(len(self.body)))
        lines = [first]
        for k, v in self.headers:
            lines.append("%s: %s" % (k, v))
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace") + (self.body or b"")

    # -------------------------------------------------------------- helpers
    def __str__(self):
        return self.to_bytes().decode("latin-1", "replace")

    @property
    def call_id(self):
        return self.get("Call-ID") or ""

    @property
    def cseq(self):
        return self.get("CSeq") or ""

    @property
    def cseq_method(self):
        parts = self.cseq.split()
        return parts[1].upper() if len(parts) > 1 else ""

    @property
    def from_tag(self):
        return tag_of(self.get("From"))

    @property
    def to_tag(self):
        return tag_of(self.get("To"))

    @property
    def top_via(self):
        return self.get("Via") or ""

    @property
    def source_ip(self):
        via = self.top_via
        m = re.search(r"SIP/2\.0/\w+\s+([^;>\s]+)", via)
        return m.group(1) if m else ""


def build_request(method, uri, headers=None, body=b"", version="SIP/2.0"):
    msg = SipMessage()
    msg.is_request = True
    msg.method = method.upper()
    msg.uri = uri
    msg.version = version
    for name, value in (headers or []):
        msg.add(name, value)
    msg.body = body or b""
    return msg


def build_response(request, code, reason, headers=None, body=b"", to_tag=None, server="ec20-sip-lite"):
    msg = SipMessage()
    msg.is_request = False
    msg.status = int(code)
    msg.reason = reason
    for name in ("Via",):
        for value in request.get_all(name):
            msg.add(name, value)
    # RFC 3261: exactly the Vias of the request, in order
    if not msg.get_all("Via"):
        for value in request.get_all("v"):
            msg.add("Via", value)
    for name in ("From", "Call-ID", "CSeq"):
        value = request.get(name)
        if value is not None:
            msg.add(name if name != "Call-ID" else "Call-ID", value)
    to_value = request.get("To") or ""
    if to_tag and not tag_of(to_value):
        to_value = "%s;tag=%s" % (to_value, to_tag)
    msg.add("To", to_value)
    if server:
        msg.add("Server", server)
    for name, value in (headers or []):
        msg.add(name, value)
    if body:
        msg.body = body
    return msg


def via_branch(message):
    return param_of(message.top_via, "branch")