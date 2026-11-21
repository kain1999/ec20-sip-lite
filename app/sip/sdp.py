"""SDP offer/answer builder and parser (audio only, G.711)."""
from __future__ import annotations

import hashlib


def _session_id(seed):
    return int(hashlib.md5(str(seed).encode()).hexdigest()[:8], 16) & 0x7FFFFFFF


def build_sdp(ip, port, seed=None, payload_types=(0, 8), direction="sendrecv",
              telephone_event=True, session_name="ec20sip"):
    """Return an SDP body offering PCMU/PCMA (+ telephone-event 101)."""
    seed = seed or "%s:%s" % (ip, port)
    sid = _session_id(seed)
    pts = [str(pt) for pt in payload_types]
    if telephone_event:
        pts.append("101")
    rtpmaps = []
    for pt in payload_types:
        if int(pt) == 0:
            rtpmaps.append("a=rtpmap:0 PCMU/8000")
        elif int(pt) == 8:
            rtpmaps.append("a=rtpmap:8 PCMA/8000")
    if telephone_event:
        rtpmaps.append("a=rtpmap:101 telephone-event/8000")
        rtpmaps.append("a=fmtp:101 0-16")
    lines = [
        "v=0",
        "o=- %d %d IN IP4 %s" % (sid, sid, ip),
        "s=%s" % session_name,
        "c=IN IP4 %s" % ip,
        "t=0 0",
        "m=audio %d RTP/AVP %s" % (int(port), " ".join(pts)),
    ]
    lines.extend(rtpmaps)
    lines.append("a=ptime:20")
    lines.append("a=%s" % direction)
    return ("\r\n".join(lines) + "\r\n").encode("latin-1")


def parse_sdp(body):
    """Return dict(ip, port, payload_types, codec, direction) or None."""
    if not body:
        return None
    if isinstance(body, bytes):
        text = body.decode("latin-1", "replace")
    else:
        text = body
    ip = ""
    port = 0
    pts = []
    codec = None
    direction = "sendrecv"
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("c=IN IP4 "):
            candidate = line[9:].strip()
            if candidate and candidate != "0.0.0.0":
                ip = candidate
        elif line.startswith("c=IN IP6 "):
            if not ip:
                ip = line[9:].strip()
        elif line.startswith("m=audio "):
            parts = line.split()
            if len(parts) >= 4:
                try:
                    port = int(parts[1])
                except ValueError:
                    port = 0
                pts = [p for p in parts[3:] if p.isdigit()]
        elif line.startswith("a=sendrecv"):
            direction = "sendrecv"
        elif line.startswith("a=sendonly"):
            direction = "sendonly"
        elif line.startswith("a=recvonly"):
            direction = "recvonly"
        elif line.startswith("a=inactive"):
            direction = "inactive"
    if not port or not ip:
        return None
    chosen = None
    for pt in pts:
        if pt == "0":
            chosen = "PCMU"
            break
        if pt == "8":
            chosen = "PCMA"
            break
    if chosen is None:
        for pt in pts:
            pass
    return {
        "ip": ip,
        "port": port,
        "payload_types": pts,
        "codec": chosen,
        "direction": direction,
    }