"""SMS PDU codec: decode SMS-DELIVER, encode SMS-SUBMIT (GSM 03.40 / 03.38).

Handles GSM 7-bit (with the extension table), UCS2 (Chinese), and concatenated
messages through the 8-bit user-data header. Used because text mode cannot
represent concatenated or UCS2 messages reliably across firmwares.
"""
from __future__ import annotations

import binascii

GSM7 = (
    "@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r\u00c5\u00e5"
    "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u0398\u039e\u001b\u00c6\u00e6\u00df\u00c9"
    " !\"#\u00a4%&'()*+,-./0123456789:;<=>?"
    "\u00a1ABCDEFGHIJKLMNOPQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7"
    "\u00bfabcdefghijklmnopqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0"
)
GSM7_EXT = {0x0A: "\f", 0x14: "^", 0x28: "{", 0x29: "}", 0x2F: "\\",
            0x3C: "[", 0x3D: "~", 0x3E: "]", 0x40: "|", 0x65: "\u20ac"}
GSM7_EXT_REV = {v: k for k, v in GSM7_EXT.items()}
GSM7_REV = {ch: idx for idx, ch in enumerate(GSM7)}


class PduError(ValueError):
    pass


# ------------------------------------------------------------------ BCD ------

def bcd_number(digits, ton=None):
    digits = str(digits).strip()
    if digits.startswith("+"):
        ton = 0x91 if ton is None else ton
        digits = digits[1:]
    elif digits.startswith("00"):
        ton = 0x91 if ton is None else ton
        digits = digits[2:]
    else:
        ton = 0x81 if ton is None else ton
    length = len(digits)
    padded = digits + ("F" if length % 2 else "")
    swapped = "".join(padded[i + 1] + padded[i] for i in range(0, len(padded), 2))
    return "%02X%02X%s" % (length, ton, swapped)


def decode_bcd(hex_str):
    if len(hex_str) < 4:
        return ""
    length = int(hex_str[:2], 16)
    ton = int(hex_str[2:4], 16)
    body = hex_str[4:4 + ((length + 1) // 2) * 2]
    digits = "".join(body[i + 1] + body[i] for i in range(0, len(body), 2))
    digits = digits[:length].replace("F", "")
    if ton == 0x91:
        return "+" + digits
    if ton == 0xD1:  # alphanumeric nibbles
        return digits
    return digits


def decode_scts(hex_str):
    try:
        raw = binascii.unhexlify(hex_str)
    except (binascii.Error, ValueError):
        return ""
    if len(raw) < 7:
        return ""
    def swap(byte):
        return (byte & 0x0F) * 10 + (byte >> 4)
    year = 2000 + swap(raw[0])
    return "%04d-%02d-%02d %02d:%02d:%02d" % (
        year, swap(raw[1]), swap(raw[2]), swap(raw[3]), swap(raw[4]), swap(raw[5]))


# -------------------------------------------------------------- 7-bit bits ---

def _septet_codes(ch):
    if ch in GSM7_REV:
        return [GSM7_REV[ch]]
    if ch in GSM7_EXT_REV:
        return [0x1B, GSM7_EXT_REV[ch]]
    raise PduError("character not representable in GSM-7: %r" % ch)


def pack_septets(text, start_bits=0):
    bits = [0] * start_bits
    for ch in text:
        for code in _septet_codes(ch):
            bits.extend((code >> i) & 1 for i in range(7))
    while len(bits) % 8:
        bits.append(0)
    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for j in range(8):
            byte |= bits[i + j] << j
        out.append(byte)
    return bytes(out)


def septets_needed(text):
    return sum(len(_septet_codes(ch)) for ch in text)


def unpack_septets(data, start_bits=0, septets=None):
    bits = []
    for byte in data:
        bits.extend((byte >> j) & 1 for j in range(8))
    offset = start_bits
    limit = septets if septets is not None else (len(bits) - start_bits) // 7
    values = []
    for _ in range(limit):
        if offset + 7 > len(bits):
            break
        value = 0
        for j in range(7):
            value |= bits[offset + j] << j
        offset += 7
        values.append(value)
    out = []
    idx = 0
    while idx < len(values):
        value = values[idx]
        if value == 0x1B and idx + 1 < len(values):
            out.append(GSM7_EXT.get(values[idx + 1], ""))
            idx += 2
        else:
            out.append(GSM7[value] if value < len(GSM7) else "")
            idx += 1
    return "".join(out)


# ------------------------------------------------------------------ deliver --

def decode_pdu(pdu_hex):
    """Decode an SMS-DELIVER PDU.

    Returns dict(sender, text, scts, dcs, parts, ref, seq, udh)
    """
    hexstr = "".join(str(pdu_hex).split()).upper()
    if len(hexstr) < 20:
        raise PduError("PDU too short")
    idx = 0
    smsc_len = int(hexstr[idx:idx + 2], 16)
    idx += 2 + smsc_len * 2
    if idx + 2 > len(hexstr):
        raise PduError("truncated PDU")
    pdu_type = int(hexstr[idx:idx + 2], 16)
    idx += 2
    if pdu_type & 0x03 != 0x00:
        raise PduError("not SMS-DELIVER (MTI=%d)" % (pdu_type & 0x03))
    has_udh = bool(pdu_type & 0x40)
    oa_len = int(hexstr[idx:idx + 2], 16)
    idx += 2
    oa_digits = ((oa_len + 1) // 2) * 2 + 2
    sender_hex = "%02X" % oa_len + hexstr[idx:idx + oa_digits]
    idx += oa_digits
    if oa_len == 0:
        sender = ""
    else:
        sender = decode_bcd(sender_hex)
    idx += 2  # PID
    dcs = int(hexstr[idx:idx + 2], 16)
    idx += 2
    scts = decode_scts(hexstr[idx:idx + 14])
    idx += 14
    if idx + 2 > len(hexstr):
        raise PduError("truncated PDU (UDL)")
    udl = int(hexstr[idx:idx + 2], 16)
    idx += 2
    ud = binascii.unhexlify(hexstr[idx:]) if idx < len(hexstr) else b""

    alphabet = (dcs >> 2) & 0x03
    parts, ref, seq, total = 1, 0, 1, 1
    header_len = 0
    if has_udh and ud:
        header_len = ud[0]
    if header_len and len(ud) > header_len:
        header = ud[1:1 + header_len]
        pos = 0
        while pos + 2 <= len(header):
            iei, iel = header[pos], header[pos + 1]
            body = header[pos + 2:pos + 2 + iel]
            if iei == 0x00 and len(body) >= 3:
                ref, total, seq = body[0], body[1], body[2]
                parts = total
            elif iei == 0x08 and len(body) >= 4:
                ref, total, seq = (body[0] << 8) | body[1], body[2], body[3]
                parts = total
            pos += 2 + iel

    if alphabet == 0:
        start_bits = ((header_len + 1) * 8 + 6) // 7 * 7 if header_len else 0
        text_septets = udl - (start_bits // 7 if header_len else 0)
        text = unpack_septets(ud, start_bits, max(0, text_septets))
    else:
        payload = ud[1 + header_len:] if header_len else ud
        text = payload.decode("utf-16-be", "replace")

    return {
        "sender": sender,
        "text": text,
        "scts": scts,
        "dcs": dcs,
        "parts": parts,
        "ref": ref,
        "seq": seq,
        "udh": header_len > 0,
    }


# ------------------------------------------------------------------- submit --

def is_gsm7(text):
    try:
        for ch in text:
            _septet_codes(ch)
        return True
    except PduError:
        return False


def encode_pdu(number, text, smsc="", ref=None, total=1, seq=1):
    """Encode one SMS-SUBMIT PDU. Returns (hex_pdu, tpdu_octets)."""
    if smsc:
        smsc_field = "%02X%s" % (len(smsc.lstrip("+")) // 2 + 1, bcd_number(smsc))
    else:
        smsc_field = "00"
    da = bcd_number(number)
    if is_gsm7(text):
        dcs = 0x00
        if total > 1:
            udh = bytes([0x05, 0x00, 0x03, (ref or 1) & 0xFF, total & 0xFF, seq & 0xFF])
            payload = pack_septets(text, start_bits=(1 + len(udh)) * 8 + 1)
            udl = (len(payload) * 8) // 7
        else:
            payload = pack_septets(text)
            udl = septets_needed(text)
    else:
        dcs = 0x08
        payload = text.encode("utf-16-be")
        if total > 1:
            payload = bytes([0x05, 0x00, 0x03, (ref or 1) & 0xFF, total & 0xFF, seq & 0xFF]) + payload
        udl = len(payload)
    tpdu = "01" + "00" + da + "00" + "%02X" % dcs + "AA" + "%02X" % udl + binascii.hexlify(payload).decode().upper()
    return smsc_field + tpdu, len(binascii.unhexlify(tpdu))


def split_text(text, with_udh=False):
    if not text:
        return [""]
    if is_gsm7(text):
        limit = 153 if with_udh else 160
    else:
        limit = 67 if with_udh else 70
    return [text[i:i + limit] for i in range(0, len(text), limit)]


def encode_messages(number, text, smsc=""):
    chunks = split_text(text, with_udh=len(text) > (153 if is_gsm7(text) else 67))
    total = len(chunks)
    ref = binascii.crc32(text.encode("utf-8")) & 0xFF
    out = []
    for seq, chunk in enumerate(chunks, start=1):
        out.append(encode_pdu(number, chunk, smsc=smsc, ref=ref, total=total, seq=seq))
    return out