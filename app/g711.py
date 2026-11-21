"""G.711 mu-law / A-law, implemented in plain Python.

160 samples per 20 ms frame means a few thousand integer ops per second per
call - far cheaper than pulling in an audio or codec library, and it keeps the
appliance to a single process with no C extension.
"""
from __future__ import annotations

BIAS = 0x84
CLIP = 32635

# exp_lut[i] == bit_length(i) - 1 (0 for i in {0,1}), computed once at import.
_EXP_LUT = bytes([0 if i < 2 else (i.bit_length() - 1) for i in range(256)])


def lin2ulaw(sample):
    sign = (sample >> 8) & 0x80
    if sign:
        sample = -sample
    if sample > CLIP:
        sample = CLIP
    sample += BIAS
    exponent = _EXP_LUT[(sample >> 7) & 0xFF]
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def ulaw2lin(byte):
    byte = (~byte) & 0xFF
    value = ((byte & 0x0F) << 3) + BIAS
    value <<= (byte & 0x70) >> 4
    return (BIAS - value) if (byte & 0x80) else (value - BIAS)


QUANT_MASK = 0xF
SEG_SHIFT = 4
SEG_MASK = 0x70
A_CLIP = 32256
# ITU G.711 A-law segment starts and step sizes (8 segments, 16 levels each).
_A_SEG_MIN = (8, 264, 528, 1056, 2112, 4224, 8448, 16896)
_A_SEG_STEP = (16, 16, 32, 64, 128, 256, 512, 1024)


def lin2alaw(sample):
    """16-bit PCM -> A-law byte (inverse of alaw2lin)."""
    negative = sample < 0
    value = -sample if negative else sample
    if value > A_CLIP:
        value = A_CLIP
    seg = 0
    for index in range(7, -1, -1):
        if value >= _A_SEG_MIN[index]:
            seg = index
            break
    quant = (value - _A_SEG_MIN[seg]) // _A_SEG_STEP[seg]
    if quant < 0:
        quant = 0
    if quant > 15:
        quant = 15
    code = (seg << SEG_SHIFT) | quant
    return code ^ (0x55 if negative else 0xD5)


def alaw2lin(byte):
    """A-law byte -> 16-bit PCM."""
    code = byte ^ 0x55
    quant = code & QUANT_MASK
    seg = (code & SEG_MASK) >> SEG_SHIFT
    if seg == 0:
        magnitude = (quant << 4) + 8
    elif seg == 1:
        magnitude = (quant << 4) + 0x108
    else:
        magnitude = ((quant << 4) + 0x108) << (seg - 1)
    return magnitude if (code & 0x80) else -magnitude


def encode(name, pcm):
    """pcm: bytes of little-endian int16 samples -> G.711 bytes."""
    if name == "PCMU":
        fn = lin2ulaw
    elif name == "PCMA":
        fn = lin2alaw
    else:
        return pcm
    out = bytearray(len(pcm) // 2)
    for i in range(len(out)):
        out[i] = fn(int.from_bytes(pcm[2 * i:2 * i + 2], "little", signed=True))
    return bytes(out)


def decode(name, payload):
    """G.711 bytes -> little-endian int16 PCM bytes."""
    if name == "PCMU":
        fn = ulaw2lin
    elif name == "PCMA":
        fn = alaw2lin
    else:
        return payload
    out = bytearray(len(payload) * 2)
    for i, b in enumerate(payload):
        out[2 * i:2 * i + 2] = fn(b).to_bytes(2, "little", signed=True)
    return bytes(out)


def silence_pcm(samples=160):
    return b"\x00\x00" * samples


# payload type -> (sdp encoding name, clock rate)
CODECS = {
    0: ("PCMU", 8000),
    8: ("PCMA", 8000),
    96: ("telephone-event", 8000),
}

SDP_CODECS = (0, 8)  # we always offer PCMU first, then PCMA