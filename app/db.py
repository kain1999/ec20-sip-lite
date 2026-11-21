"""SQLite storage: extensions, registrations, SMS, rules, settings, CDR."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading

from .util import ts_str

SCHEMA = """
CREATE TABLE IF NOT EXISTS extensions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT UNIQUE NOT NULL,
    display_name TEXT DEFAULT '',
    password TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    forward_to TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS registrations (
    number TEXT PRIMARY KEY,
    contact TEXT NOT NULL,
    user_agent TEXT DEFAULT '',
    transport TEXT DEFAULT 'udp',
    expires INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    call_id TEXT DEFAULT '',
    source_ip TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,
    peer TEXT NOT NULL,
    text TEXT NOT NULL,
    ts TEXT NOT NULL,
    read INTEGER DEFAULT 0,
    forwarded INTEGER DEFAULT 0,
    parts INTEGER DEFAULT 1,
    raw TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sms_ts ON sms(ts DESC);
CREATE INDEX IF NOT EXISTS idx_sms_peer ON sms(peer);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    match_type TEXT DEFAULT 'all',
    pattern TEXT DEFAULT '',
    channels TEXT DEFAULT 'email',
    template TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cdr (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT NOT NULL,
    ended TEXT DEFAULT '',
    from_num TEXT DEFAULT '',
    to_num TEXT DEFAULT '',
    dir TEXT DEFAULT 'out',
    state TEXT DEFAULT '',
    duration INTEGER DEFAULT 0,
    sip_call_id TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS forward_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sms_id INTEGER DEFAULT 0,
    channel TEXT DEFAULT '',
    status TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    ts TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "smtp": {
        "enabled": 0,
        "host": "",
        "port": 465,
        "user": "",
        "password": "",
        "from": "",
        "tls": "ssl",  # ssl | starttls | none
    },
    "dingtalk": {
        "enabled": 0,
        "webhook": "",
        "secret": "",
        "at_mobiles": "",
    },
    "webhook": {
        "enabled": 0,
        "url": "",
    },
    "dial": {
        "voice_prefix": "9",
        "ring_seconds": 30,
        "auto_answer": 0,
        "forward_all_to": "",
    },
    "sip": {
        "realm": "ec20sip.local",
        "external_ip": "",
        "port": 5060,
    },
    "auth": {
        "username": "admin",
        "password_hash": "",
    },
}


# --------------------------------------------------------------- passwords ---

def hash_password(password, iterations=120000):
    salt = secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, salt, digest)


def verify_password(password, stored):
    if not password or not stored:
        return False
    try:
        algo, iters, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters)).hex()
    return hmac.compare_digest(calc, digest)


def random_password(length=8):
    alphabet = "23456789abcdefghjkmnpqrstuvwxyz"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ------------------------------------------------------------------ store ----

class Db:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        self._seed()

    # ------------------------------------------------------------ low level
    def execute(self, sql, params=()):
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def execute_many(self, sql, rows):
        with self._lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    def query(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def query_one(self, sql, params=()):
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def scalar(self, sql, params=(), default=None):
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
        if row is None:
            return default
        return row[0]

    def close(self):
        with self._lock:
            self.conn.close()

    # --------------------------------------------------------------- seeds
    def _seed(self):
        self.generated_admin_password = None
        if not self.get_setting("auth", None):
            password = random_password(10)
            self.generated_admin_password = password
            self.set_setting("auth", {
                "username": "admin",
                "password_hash": hash_password(password),
            })
        for key, value in DEFAULT_SETTINGS.items():
            if self.get_setting(key, None) is None:
                self.set_setting(key, value)

    # ------------------------------------------------------------ settings
    def get_setting(self, key, default=None):
        row = self.query_one("SELECT value FROM settings WHERE key=?", (key,))
        if not row:
            return default
        try:
            value = json.loads(row["value"])
        except ValueError:
            return default
        if isinstance(DEFAULT_SETTINGS.get(key), dict) and isinstance(value, dict):
            merged = json.loads(json.dumps(DEFAULT_SETTINGS[key]))
            merged.update(value)
            return merged
        return value

    def set_setting(self, key, value):
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def all_settings(self):
        out = {}
        for key in DEFAULT_SETTINGS:
            out[key] = self.get_setting(key, DEFAULT_SETTINGS[key])
        return out

    def merge_settings(self, patch):
        """patch: {"smtp": {...}, ...} - shallow merge per section."""
        for key, value in (patch or {}).items():
            if key not in DEFAULT_SETTINGS:
                continue
            current = self.get_setting(key, DEFAULT_SETTINGS[key])
            if isinstance(current, dict) and isinstance(value, dict):
                current.update(value)
                self.set_setting(key, current)
            else:
                self.set_setting(key, value)

    # ---------------------------------------------------------- extensions
    def list_extensions(self):
        return self.query("SELECT * FROM extensions ORDER BY number")

    def get_extension(self, number):
        return self.query_one("SELECT * FROM extensions WHERE number=?", (number,))

    def get_extension_by_id(self, ext_id):
        return self.query_one("SELECT * FROM extensions WHERE id=?", (ext_id,))

    def create_extension(self, number, display_name="", password=None, enabled=1, forward_to=""):
        number = str(number).strip()
        if not number:
            raise ValueError("number required")
        if self.get_extension(number):
            raise ValueError("分机号已存在: %s" % number)
        password = password or random_password(6)
        self.execute(
            "INSERT INTO extensions(number, display_name, password, enabled, forward_to, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (number, display_name or "", password, 1 if enabled else 0, forward_to or "", ts_str()),
        )
        return self.get_extension(number)

    def update_extension(self, ext_id, **fields):
        allowed = ("display_name", "password", "enabled", "forward_to")
        sets, params = [], []
        for key in allowed:
            if key in fields and fields[key] is not None:
                value = fields[key]
                if key == "enabled":
                    value = 1 if value else 0
                sets.append("%s=?" % key)
                params.append(value)
        if not sets:
            return self.get_extension_by_id(ext_id)
        params.append(ext_id)
        self.execute("UPDATE extensions SET %s WHERE id=?" % ",".join(sets), params)
        return self.get_extension_by_id(ext_id)

    def delete_extension(self, ext_id):
        row = self.get_extension_by_id(ext_id)
        if row:
            self.execute("DELETE FROM registrations WHERE number=?", (row["number"],))
        self.execute("DELETE FROM extensions WHERE id=?", (ext_id,))

    # ------------------------------------------------------- registrations
    def upsert_registration(self, number, contact, expires, user_agent="", transport="udp",
                            call_id="", source_ip=""):
        self.execute(
            "INSERT INTO registrations(number, contact, user_agent, transport, expires, updated_at, "
            "call_id, source_ip) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(number) DO UPDATE SET contact=excluded.contact, user_agent=excluded.user_agent, "
            "transport=excluded.transport, expires=excluded.expires, updated_at=excluded.updated_at, "
            "call_id=excluded.call_id, source_ip=excluded.source_ip",
            (number, contact, user_agent, transport, expires, ts_str(), call_id, source_ip),
        )

    def remove_registration(self, number):
        self.execute("DELETE FROM registrations WHERE number=?", (number,))

    def registrations(self):
        return self.query("SELECT * FROM registrations")

    def get_registration(self, number):
        return self.query_one("SELECT * FROM registrations WHERE number=?", (number,))

    # ---------------------------------------------------------------- sms
    def add_sms(self, direction, peer, text, parts=1, raw="", read=None):
        now = ts_str()
        if read is None:
            read = 1 if direction == "out" else 0
        cur = self.execute(
            "INSERT INTO sms(direction, peer, text, ts, read, forwarded, parts, raw) VALUES(?,?,?,?,?,0,?,?)",
            (direction, peer, text, now, 1 if read else 0, int(parts), raw or ""),
        )
        return cur.lastrowid

    def list_sms(self, limit=50, offset=0, q="", unread=False):
        where, params = [], []
        if q:
            where.append("(peer LIKE ? OR text LIKE ?)")
            params.extend(["%" + q + "%", "%" + q + "%"])
        if unread:
            where.append("read=0")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.scalar("SELECT COUNT(*) FROM sms " + clause, tuple(params), 0)
        rows = self.query(
            "SELECT * FROM sms %s ORDER BY id DESC LIMIT ? OFFSET ?" % clause,
            tuple(params) + (int(limit), int(offset)),
        )
        return rows, total

    def get_sms(self, sms_id):
        return self.query_one("SELECT * FROM sms WHERE id=?", (sms_id,))

    def mark_sms_read(self, sms_id=None):
        if sms_id:
            self.execute("UPDATE sms SET read=1 WHERE id=?", (sms_id,))
        else:
            self.execute("UPDATE sms SET read=1 WHERE read=0")

    def mark_sms_forwarded(self, sms_id, forwarded=1):
        self.execute("UPDATE sms SET forwarded=? WHERE id=?", (1 if forwarded else 0, sms_id))

    def delete_sms(self, sms_id=None, ids=None):
        if ids:
            marks = ",".join("?" for _ in ids)
            self.execute("DELETE FROM sms WHERE id IN (%s)" % marks, tuple(ids))
        elif sms_id:
            self.execute("DELETE FROM sms WHERE id=?", (sms_id,))

    def trim_sms(self, keep=2000):
        excess = self.scalar("SELECT COUNT(*) FROM sms", (), 0) - keep
        if excess > 0:
            self.execute(
                "DELETE FROM sms WHERE id IN (SELECT id FROM sms ORDER BY id ASC LIMIT ?)", (excess,)
            )

    def sms_stats(self):
        total = self.scalar("SELECT COUNT(*) FROM sms", (), 0)
        unread = self.scalar("SELECT COUNT(*) FROM sms WHERE read=0", (), 0)
        today = self.scalar(
            "SELECT COUNT(*) FROM sms WHERE ts >= ?", (ts_str().split(" ")[0] + " 00:00:00",), 0
        )
        return {"total": total, "unread": unread, "today": today}

    # -------------------------------------------------------------- rules
    def list_rules(self):
        return self.query("SELECT * FROM rules ORDER BY id")

    def get_rule(self, rule_id):
        return self.query_one("SELECT * FROM rules WHERE id=?", (rule_id,))

    def create_rule(self, name, match_type="all", pattern="", channels="email", template="", enabled=1):
        cur = self.execute(
            "INSERT INTO rules(name, enabled, match_type, pattern, channels, template, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (name or "规则", 1 if enabled else 0, match_type or "all", pattern or "",
             channels or "email", template or "", ts_str()),
        )
        return cur.lastrowid

    def update_rule(self, rule_id, **fields):
        allowed = ("name", "enabled", "match_type", "pattern", "channels", "template")
        sets, params = [], []
        for key in allowed:
            if key in fields and fields[key] is not None:
                value = fields[key]
                if key == "enabled":
                    value = 1 if value else 0
                sets.append("%s=?" % key)
                params.append(value)
        if sets:
            params.append(rule_id)
            self.execute("UPDATE rules SET %s WHERE id=?" % ",".join(sets), params)

    def delete_rule(self, rule_id):
        self.execute("DELETE FROM rules WHERE id=?", (rule_id,))

    # --------------------------------------------------------------- cdr
    def add_cdr(self, started, from_num, to_num, direction, sip_call_id=""):
        cur = self.execute(
            "INSERT INTO cdr(started, from_num, to_num, dir, state, duration, sip_call_id) "
            "VALUES(?,?,?,?,'',0,?)",
            (started, from_num, to_num, direction, sip_call_id),
        )
        return cur.lastrowid

    def finish_cdr(self, cdr_id, state, duration):
        if not cdr_id:
            return
        self.execute(
            "UPDATE cdr SET ended=?, state=?, duration=? WHERE id=?",
            (ts_str(), state, int(duration), cdr_id),
        )

    def list_cdr(self, limit=50):
        return self.query("SELECT * FROM cdr ORDER BY id DESC LIMIT ?", (int(limit),))

    # ------------------------------------------------------------ forward
    def log_forward(self, sms_id, channel, status, detail=""):
        self.execute(
            "INSERT INTO forward_log(sms_id, channel, status, detail, ts) VALUES(?,?,?,?,?)",
            (sms_id, channel, status, (detail or "")[:500], ts_str()),
        )