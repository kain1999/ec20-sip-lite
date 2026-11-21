"""Stdlib HTTP server: JSON API + static admin panel (threaded, tiny)."""
from __future__ import annotations

import base64
import hmac
import json
import logging
import mimetypes
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..db import hash_password, random_password, verify_password
from ..util import (disk_free_mb, loadavg, mask_secret, mem_snapshot, norm_number,
                    ring_tail, rss_kb, temp_c, to_int, ts_str, uptime_str, truthy)

LOG = logging.getLogger("web")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class AppContext:
    """Everything the HTTP layer needs, plus loop-safe execution."""

    def __init__(self, loop, config, db, sip_server, modem, notifier):
        self.loop = loop
        self.config = config
        self.db = db
        self.sip = sip_server
        self.modem = modem
        self.notifier = notifier
        self.sessions = {}
        self.started = time.time()
        self.sim = False
        self._sync_settings_from_config()

    def _sync_settings_from_config(self):
        """First boot: seed the editable settings with values from config.json."""
        sip_cfg = {
            "realm": self.config.get("sip.realm"),
            "external_ip": self.config.get("sip.external_ip"),
            "port": self.config.get("sip.port"),
        }
        current = dict(self.db.get_setting("sip", {}) or {})
        updated = {k: v for k, v in sip_cfg.items() if v not in (None, "", 0) and not current.get(k)}
        if updated:
            current.update(updated)
            self.db.set_setting("sip", current)
        dial = dict(self.db.get_setting("dial", {}) or {})
        prefix = self.config.get("ec20.voice_prefix")
        if prefix and not dial.get("voice_prefix"):
            dial["voice_prefix"] = str(prefix)
            self.db.set_setting("dial", dial)

    # ---------------------------------------------------------------- helpers
    def call(self, coro, timeout=15.0):
        import asyncio
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout)

    def spawn(self, coro):
        import asyncio
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def session_valid(self, token):
        if not token:
            return False
        expiry = self.sessions.get(token)
        if not expiry:
            return False
        if expiry < time.time():
            self.sessions.pop(token, None)
            return False
        return True

    def new_session(self):
        token = secrets.token_urlsafe(24)
        ttl = int(self.config.get("web.session_ttl", 86400))
        self.sessions[token] = time.time() + ttl
        return token

    # ----------------------------------------------------------------- status
    def status(self):
        settings = self.db.all_settings()
        host = mem_snapshot()
        try:
            modem_status = self.modem.status() if self.modem else {}
        except Exception:  # pragma: no cover
            modem_status = {}
        sip_status = self.sip.status() if self.sip else {}
        channels = []
        for key in ("smtp", "dingtalk", "webhook"):
            section = settings.get(key, {})
            if section.get("enabled"):
                channels.append(key)
        return {
            "uptime": int(time.time() - self.started),
            "version": "1.0.0",
            "host": {
                "mem_total_kb": host["mem_total_kb"],
                "mem_avail_kb": host["mem_avail_kb"],
                "load": loadavg(),
                "temp_c": temp_c(),
                "disk_free_mb": disk_free_mb("/"),
                "uptime": uptime_str(),
                "rss_kb": rss_kb(),
            },
            "modem": modem_status,
            "calls": self.sip.calls.status() if self.sip else [],
            "sip": sip_status,
            "sms": self.db.sms_stats(),
            "sim": self.sim,
            "forward": {"enabled": bool(settings.get("sms", {}).get("forward_enabled", True)),
                        "channels": channels},
        }

    def public_settings(self):
        settings = self.db.all_settings()
        smtp = dict(settings.get("smtp", {}))
        ding = dict(settings.get("dingtalk", {}))
        auth = dict(settings.get("auth", {}))
        smtp_out = {k: v for k, v in smtp.items() if k != "password"}
        smtp_out["pass_set"] = bool(smtp.get("password"))
        ding_out = {k: v for k, v in ding.items() if k != "secret"}
        ding_out["secret_set"] = bool(ding.get("secret"))
        return {
            "smtp": smtp_out,
            "dingtalk": ding_out,
            "webhook": dict(settings.get("webhook", {})),
            "dial": dict(settings.get("dial", {})),
            "sip": dict(settings.get("sip", {})),
            "auth": {"username": auth.get("username", "admin")},
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "ec20-sip-lite"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- plumbing
    @property
    def ctx(self):
        return self.server.ctx

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        LOG.debug("%s - %s", self.client_address[0], fmt % args)

    def _send(self, code, body, content_type="application/json; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, headers=None):
        self._send(code, _json_bytes(obj), headers=headers)

    def _error(self, message, code=400):
        self._json({"ok": False, "error": message}, code)

    def _body(self):
        length = to_int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _cookie(self, name):
        header = self.headers.get("Cookie", "")
        for part in header.split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                if key.strip() == name:
                    return value.strip()
        return ""

    def _authed(self):
        return self.ctx.session_valid(self._cookie("sid"))

    # ------------------------------------------------------------ dispatch
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_HEAD(self):
        self._dispatch("GET")

    def _dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if not path.startswith("/api/"):
                self._serve_static(path)
                return
            if path == "/api/login" and method == "POST":
                self._login()
                return
            if not self._authed():
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._api(method, path, query)
        except BrokenPipeError:  # pragma: no cover
            pass
        except Exception as exc:  # pragma: no cover
            LOG.exception("request failed: %s %s", method, path)
            try:
                self._error(str(exc), 500)
            except Exception:
                pass

    # -------------------------------------------------------------- static
    def _serve_static(self, path):
        if path in ("/", "/index.html"):
            file_path = os.path.join(STATIC_DIR, "index.html")
        else:
            relative = path.lstrip("/")
            if relative.startswith("static/"):
                relative = relative[len("static/"):]
            file_path = os.path.normpath(os.path.join(STATIC_DIR, relative))
            if not file_path.startswith(STATIC_DIR):
                self._error("forbidden", 403)
                return
        if not os.path.isfile(file_path):
            if path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            self._error("not found", 404)
            return
        ctype = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        with open(file_path, "rb") as fh:
            body = fh.read()
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, body, ctype)

    # --------------------------------------------------------------- login
    def _login(self):
        payload = self._body()
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        auth = self.ctx.db.get_setting("auth", {}) or {}
        if username and username == auth.get("username") and verify_password(password, auth.get("password_hash", "")):
            token = self.ctx.new_session()
            self._json({"ok": True}, headers={"Set-Cookie": "sid=%s; Path=/; HttpOnly; SameSite=Lax" % token})
        else:
            self._json({"ok": False, "error": "用户名或密码错误"}, 401)

    # ------------------------------------------------------------------ API
    def _api(self, method, path, query):
        ctx = self.ctx
        db = ctx.db

        if path == "/api/status" and method == "GET":
            self._json(ctx.status())
            return

        if path == "/api/system/info" and method == "GET":
            import platform
            self._json({
                "python": platform.python_version(),
                "kernel": platform.release(),
                "model": platform.node(),
                "arch": platform.machine(),
                "rss_kb": rss_kb(),
                "install_dir": os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "data_dir": ctx.config.get("paths.data_dir"),
                "sim": ctx.sim,
            })
            return

        if path == "/api/logs" and method == "GET":
            tail = to_int(query.get("tail", ["200"])[0], 200)
            self._json({"lines": ring_tail(min(600, max(10, tail)))})
            return

        if path == "/api/extensions" and method == "GET":
            items = []
            for row in db.list_extensions():
                reg = ctx.sip.registrar.contact(row["number"]) if ctx.sip else None
                items.append({
                    "id": row["id"], "number": row["number"],
                    "display_name": row.get("display_name", ""),
                    "password": row["password"], "enabled": row["enabled"],
                    "forward_to": row.get("forward_to", ""),
                    "registered": bool(reg),
                    "contact": reg.get("contact", "") if reg else "",
                    "last_seen": reg.get("updated_at", "") if reg else "",
                    "created_at": row.get("created_at", ""),
                })
            self._json({"items": items})
            return

        if path == "/api/extensions" and method == "POST":
            payload = self._body()
            number = str(payload.get("number", "")).strip()
            if not number.isdigit() or not (2 <= len(number) <= 8):
                self._error("分机号必须是 2-8 位数字")
                return
            password = str(payload.get("password") or "").strip() or random_password(8)
            display = str(payload.get("display_name", "")).strip()
            enabled = 1 if payload.get("enabled", 1) else 0
            try:
                row = db.create_extension(number, display, password, enabled)
            except ValueError as exc:
                self._error(str(exc), 409)
                return
            LOG.info("extension %s created", number)
            self._json({"ok": True, "id": row["id"], "password": row["password"]})
            return

        if path.startswith("/api/extensions/"):
            rest = path[len("/api/extensions/"):].split("/")
            try:
                ext_id = int(rest[0])
            except (ValueError, IndexError):
                self._error("bad id", 400)
                return
            row = db.get_extension_by_id(ext_id)
            if not row:
                self._error("分机不存在", 404)
                return
            if len(rest) == 1 and method == "PUT":
                payload = self._body()
                fields = {}
                for key in ("display_name", "password", "enabled", "forward_to"):
                    if key in payload:
                        value = payload[key]
                        if key == "password":
                            value = str(value or "").strip() or None
                        elif key == "enabled":
                            value = 1 if truthy(value) else 0
                        fields[key] = value
                db.update_extension(ext_id, **fields)
                if "enabled" in fields and not fields["enabled"]:
                    db.remove_registration(row["number"])
                self._json({"ok": True})
                return
            if len(rest) == 1 and method == "DELETE":
                db.delete_extension(ext_id)
                LOG.info("extension %s deleted", row["number"])
                self._json({"ok": True})
                return
            if len(rest) == 2 and rest[1] == "kick" and method == "POST":
                db.remove_registration(row["number"])
                self._json({"ok": True})
                return
            if len(rest) == 2 and rest[1] == "reset_password" and method == "POST":
                password = random_password(8)
                db.update_extension(ext_id, password=password)
                self._json({"ok": True, "password": password})
                return

        if path == "/api/sms" and method == "GET":
            limit = min(200, max(1, to_int(query.get("limit", ["50"])[0], 50)))
            offset = max(0, to_int(query.get("offset", ["0"])[0], 0))
            q = query.get("q", [""])[0]
            unread = truthy(query.get("unread", ["0"])[0])
            rows, total = db.list_sms(limit, offset, q, unread)
            self._json({"items": rows, "total": total})
            return

        if path == "/api/sms/send" and method == "POST":
            payload = self._body()
            to = norm_number(payload.get("to", ""))
            text = str(payload.get("text", ""))
            if not to or not text:
                self._error("号码和内容不能为空")
                return
            try:
                ctx.call(ctx.modem.send_sms(to, text), timeout=90)
            except Exception as exc:
                self._error("发送失败: %s" % exc, 502)
                return
            self._json({"ok": True})
            return

        if path == "/api/sms/read_all" and method == "POST":
            db.mark_sms_read()
            self._json({"ok": True})
            return

        if path.startswith("/api/sms/"):
            rest = path[len("/api/sms/"):].split("/")
            try:
                sms_id = int(rest[0])
            except (ValueError, IndexError):
                self._error("bad id", 400)
                return
            if len(rest) == 2 and rest[1] == "read" and method == "POST":
                db.mark_sms_read(sms_id)
                self._json({"ok": True})
                return
            if len(rest) == 2 and rest[1] == "forward" and method == "POST":
                row = db.get_sms(sms_id)
                if not row:
                    self._error("not found", 404)
                    return
                results = ctx.call(ctx.notifier.forward(row), timeout=60)
                self._json({"ok": True, "results": results})
                return
            if len(rest) == 1 and method == "DELETE":
                db.delete_sms(sms_id)
                self._json({"ok": True})
                return

        if path == "/api/rules" and method == "GET":
            self._json({"items": db.list_rules()})
            return

        if path == "/api/rules" and method == "POST":
            payload = self._body()
            rule_id = db.create_rule(
                str(payload.get("name", "")).strip() or "规则",
                match_type=str(payload.get("match_type", "all")),
                pattern=str(payload.get("pattern", "")),
                channels=",".join(payload.get("channels", ["email"])) if isinstance(payload.get("channels"), list)
                else str(payload.get("channels", "email")),
                template=str(payload.get("template", "")),
                enabled=1 if truthy(payload.get("enabled", 1)) else 0,
            )
            self._json({"ok": True, "id": rule_id})
            return

        if path.startswith("/api/rules/"):
            rest = path[len("/api/rules/"):].split("/")
            try:
                rule_id = int(rest[0])
            except (ValueError, IndexError):
                self._error("bad id", 400)
                return
            if len(rest) == 1 and method == "PUT":
                payload = self._body()
                fields = {}
                for key in ("name", "match_type", "pattern", "template"):
                    if key in payload:
                        fields[key] = str(payload[key])
                if "channels" in payload:
                    channels = payload["channels"]
                    fields["channels"] = ",".join(channels) if isinstance(channels, list) else str(channels)
                if "enabled" in payload:
                    fields["enabled"] = 1 if truthy(payload["enabled"]) else 0
                db.update_rule(rule_id, **fields)
                self._json({"ok": True})
                return
            if len(rest) == 1 and method == "DELETE":
                db.delete_rule(rule_id)
                self._json({"ok": True})
                return
            if len(rest) == 2 and rest[1] == "test" and method == "POST":
                sample = {"id": 0, "peer": "13800138000", "text": "测试短信 验证码 123456",
                          "ts": ts_str()}
                results = ctx.call(ctx.notifier.forward(sample), timeout=60)
                self._json({"ok": True, "results": results})
                return

        if path == "/api/settings" and method == "GET":
            self._json(ctx.public_settings())
            return

        if path == "/api/settings" and method == "PUT":
            payload = self._body()
            patch = {}
            for section in ("smtp", "dingtalk", "webhook", "dial", "sip"):
                incoming = payload.get(section)
                if not isinstance(incoming, dict):
                    continue
                current = dict(db.get_setting(section, {}) or {})
                for key, value in incoming.items():
                    if key in ("password", "secret") and not str(value or "").strip():
                        continue
                    if key in ("enabled", "auto_answer"):
                        current[key] = 1 if truthy(value) else 0
                    else:
                        current[key] = value
                patch[section] = current
            auth_patch = payload.get("auth") or {}
            if auth_patch:
                auth = dict(db.get_setting("auth", {}) or {})
                if auth_patch.get("username"):
                    auth["username"] = str(auth_patch["username"]).strip()
                new_password = str(auth_patch.get("password") or "").strip()
                if new_password:
                    auth["password_hash"] = hash_password(new_password)
                patch["auth"] = auth
            db.merge_settings(patch)
            if "sip" in patch:
                keep = bool(patch["sip"].get("realm"))
                _apply_sip_settings(ctx, patch["sip"], keep)
            self._json({"ok": True})
            return

        if path == "/api/settings/test_email" and method == "POST":
            payload = self._body()
            smtp = db.get_setting("smtp", {}) or {}
            to_addr = str(payload.get("to") or smtp.get("from") or smtp.get("user") or "")
            status, detail = ctx.call(ctx.notifier.send_email(smtp, to_addr, "EC20 邮件测试", "这是一封测试邮件。"))
            self._json({"ok": status == "sent", "detail": detail})
            return

        if path == "/api/settings/test_dingtalk" and method == "POST":
            ding = db.get_setting("dingtalk", {}) or {}
            status, detail = ctx.call(ctx.notifier.send_dingtalk(ding, "EC20 钉钉测试消息"))
            self._json({"ok": status == "sent", "detail": detail})
            return

        if path == "/api/settings/test_webhook" and method == "POST":
            hook = db.get_setting("webhook", {}) or {}
            status, detail = ctx.call(ctx.notifier.send_webhook(hook.get("url", ""), {"event": "test"}))
            self._json({"ok": status == "sent", "detail": detail})
            return

        if path == "/api/cdr" and method == "GET":
            limit = min(200, max(1, to_int(query.get("limit", ["50"])[0], 50)))
            self._json({"items": db.list_cdr(limit)})
            return

        if path == "/api/dial" and method == "POST":
            payload = self._body()
            number = norm_number(payload.get("number", ""))
            if not number:
                self._error("号码为空")
                return
            try:
                call_id = ctx.sip.calls.dial(number)
            except Exception as exc:
                self._error(str(exc), 503)
                return
            self._json({"ok": True, "call_id": call_id})
            return

        if path.startswith("/api/calls/"):
            rest = path[len("/api/calls/"):].split("/")
            call_id = rest[0] if rest else ""
            action = rest[1] if len(rest) > 1 else ""
            if action == "answer" and method == "POST":
                self._json({"ok": ctx.sip.calls.answer(call_id)})
                return
            if action == "hangup" and method == "POST":
                self._json({"ok": ctx.sip.calls.hangup(call_id)})
                return

        if path == "/api/modem/init" and method == "POST":
            ctx.spawn(ctx.modem.init_modem())
            self._json({"ok": True})
            return

        # ---------------------------------------------------- simulator only
        if path == "/api/sim/incoming" and method == "POST":
            if not ctx.sim:
                self._error("模拟模式才可用", 400)
                return
            payload = self._body()
            number = norm_number(payload.get("number", "13800138000")) or "13800138000"
            call_id = ctx.modem.simulate_incoming(number)
            self._json({"ok": True, "call_id": call_id})
            return

        if path == "/api/sim/sms" and method == "POST":
            if not ctx.sim:
                self._error("模拟模式才可用", 400)
                return
            payload = self._body()
            peer = str(payload.get("peer", "13800138000"))
            text = str(payload.get("text", "模拟短信：验证码 123456"))
            record = ctx.modem.simulate_sms(peer, text)
            self._json({"ok": True, "sms": record})
            return

        if path == "/api/modem/reset" and method == "POST":
            async def _reset():
                await ctx.modem.stop()
                await ctx.modem.start()
            ctx.spawn(_reset())
            self._json({"ok": True})
            return

        if path == "/api/logout" and method == "POST":
            ctx.sessions.pop(self._cookie("sid"), None)
            self._json({"ok": True}, headers={"Set-Cookie": "sid=; Path=/; Max-Age=0"})
            return

        self._error("not found", 404)


def _apply_sip_settings(ctx, sip_settings, keep):
    server = ctx.sip
    if not server:
        return
    realm = sip_settings.get("realm")
    if realm and keep:
        from ..sip.digest import DigestAuth
        server.auth = DigestAuth(realm)
    external = sip_settings.get("external_ip")
    if external:
        server.media_ip = external


class WebServer:
    def __init__(self, ctx, bind="0.0.0.0", port=8080):
        self.ctx = ctx
        self.bind = bind
        self.port = port
        self.httpd = None
        self.thread = None

    def start(self):
        self.httpd = ThreadingHTTPServer((self.bind, self.port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.ctx = self.ctx
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="web", daemon=True)
        self.thread.start()
        LOG.info("web panel on http://%s:%d", self.bind, self.port)
        return self

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None