"""HTTP-style Digest authentication for SIP (RFC 2617, MD5)."""
from __future__ import annotations

import hashlib
import re
import secrets
import time

_PARAM_RE = re.compile(r'(\w+)\s*=\s*("([^"]*)"|([^,\s]+))')


def md5(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def parse_challenge(value):
    params = {}
    if not value:
        return params
    for match in _PARAM_RE.finditer(value):
        key = match.group(1).lower()
        params[key] = match.group(3) if match.group(3) is not None else match.group(4)
    return params


def compute_response(username, password, method, uri, params):
    realm = params.get("realm", "")
    nonce = params.get("nonce", "")
    qop = params.get("qop", "")
    algorithm = params.get("algorithm", "MD5")
    if algorithm and algorithm.upper() not in ("MD5", ""):
        return None
    nc = params.get("nc", "00000001")
    cnonce = params.get("cnonce", "")
    ha1 = md5("%s:%s:%s" % (username, realm, password))
    ha2 = md5("%s:%s" % (method, uri))
    if qop:
        return md5("%s:%s:%s:%s:%s:%s" % (ha1, nonce, nc, cnonce, qop, ha2))
    return md5("%s:%s:%s" % (ha1, nonce, ha2))


class DigestAuth:
    """Issues nonces and verifies Authorization headers."""

    def __init__(self, realm, nonce_ttl=600):
        self.realm = realm
        self.nonce_ttl = nonce_ttl
        self._nonces = {}

    def _prune(self):
        now = time.time()
        for key in [k for k, exp in self._nonces.items() if exp < now]:
            self._nonces.pop(key, None)

    def issue_nonce(self):
        self._prune()
        nonce = secrets.token_hex(16)
        self._nonces[nonce] = time.time() + self.nonce_ttl
        return nonce

    def challenge(self, stale=False):
        header = ('Digest realm="%s", nonce="%s", qop="auth", algorithm=MD5, '
                  'opaque="%s"' % (self.realm, self.issue_nonce(), secrets.token_hex(8)))
        if stale:
            header += ', stale=true'
        return header

    def verify(self, message, password):
        """message: SipMessage with Authorization header. password: str or None."""
        auth = message.get("Authorization")
        if not auth:
            return False, "no Authorization header"
        params = parse_challenge(auth)
        if params.get("algorithm", "MD5").upper() not in ("MD5", ""):
            return False, "unsupported algorithm"
        nonce = params.get("nonce", "")
        expiry = self._nonces.get(nonce)
        if expiry is None:
            return False, "unknown nonce"
        if expiry < time.time():
            self._nonces.pop(nonce, None)
            return False, "stale nonce"
        if params.get("realm") != self.realm:
            return False, "realm mismatch"
        if password is None:
            return False, "unknown user"
        expected = compute_response(params.get("username", ""), password,
                                    message.method, params.get("uri", ""), params)
        if not expected:
            return False, "cannot compute response"
        response = params.get("response", "")
        if secrets.compare_digest(expected.lower(), response.lower()):
            return True, ""
        return False, "bad response"