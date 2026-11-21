"""Outbound notifications: e-mail (SMTP), DingTalk robot, generic webhook."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import smtplib
import ssl
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr

from .util import ts_str

LOG = logging.getLogger("notify")


def _matches(rule, sms):
    match_type = (rule.get("match_type") or "all").lower()
    pattern = (rule.get("pattern") or "").strip()
    if match_type == "all" or not pattern:
        return True
    if match_type == "sender":
        candidates = [p.strip() for p in pattern.replace(",", " ").split() if p.strip()]
        peer = sms.get("peer", "")
        for cand in candidates:
            if cand and (cand in peer or peer.endswith(cand.lstrip("+"))):
                return True
        return False
    if match_type == "keyword":
        keywords = [p.strip() for p in pattern.replace("，", ",").split(",") if p.strip()]
        text = sms.get("text", "")
        return any(k in text for k in keywords)
    return True


def render(template, sms):
    if template:
        try:
            return template.format(peer=sms.get("peer", ""), text=sms.get("text", ""), time=sms.get("ts", ""))
        except (KeyError, IndexError, ValueError):
            pass
    return "【EC20 短信】\n来自: %s\n时间: %s\n内容:\n%s" % (
        sms.get("peer", ""), sms.get("ts", ts_str()), sms.get("text", ""))


class Notifier:
    def __init__(self, loop, db, settings_provider):
        self.loop = loop
        self.db = db
        self.settings = settings_provider  # callable -> dict

    # ------------------------------------------------------------------ api
    async def forward(self, sms):
        """Apply enabled rules to an inbound SMS. Returns list of (channel, status)."""
        results = []
        rules = [r for r in self.db.list_rules() if r.get("enabled")]
        for rule in rules:
            if not _matches(rule, sms):
                continue
            channels = [c.strip() for c in (rule.get("channels") or "").split(",") if c.strip()]
            body = render(rule.get("template"), sms)
            for channel in channels:
                status, detail = await self._dispatch(channel, body, sms)
                self.db.log_forward(sms.get("id", 0), channel, status, detail)
                results.append((channel, status))
        if results and all(status == "sent" for _, status in results):
            self.db.mark_sms_forwarded(sms.get("id", 0), 1)
        return results

    async def _dispatch(self, channel, body, sms):
        settings = self.settings()
        if channel == "email":
            smtp = settings.get("smtp", {})
            if not smtp.get("enabled"):
                return "skipped", "smtp disabled"
            return await self.send_email(smtp, smtp.get("from") or smtp.get("user"), "EC20 短信转发", body)
        if channel == "dingtalk":
            ding = settings.get("dingtalk", {})
            if not ding.get("enabled") or not ding.get("webhook"):
                return "skipped", "dingtalk disabled"
            return await self.send_dingtalk(ding, body)
        if channel == "webhook":
            hook = settings.get("webhook", {})
            if not hook.get("enabled") or not hook.get("url"):
                return "skipped", "webhook disabled"
            return await self.send_webhook(hook["url"], {
                "event": "sms", "peer": sms.get("peer", ""), "text": sms.get("text", ""),
                "ts": sms.get("ts", ""), "id": sms.get("id", 0),
            })
        return "skipped", "unknown channel %s" % channel

    # --------------------------------------------------------------- email
    async def send_email(self, smtp_cfg, to_addr, subject, body):
        if not to_addr:
            return "error", "收件人为空"

        def _send():
            msg = EmailMessage()
            msg["From"] = formataddr(("EC20 Gateway", smtp_cfg.get("from") or smtp_cfg.get("user", "")))
            recipients = [a.strip() for a in str(to_addr).replace(";", ",").split(",") if a.strip()]
            msg["To"] = ", ".join(recipients)
            msg["Subject"] = subject
            msg.set_content(body)
            host = smtp_cfg.get("host", "")
            port = int(smtp_cfg.get("port", 465) or 465)
            tls = (smtp_cfg.get("tls") or "ssl").lower()
            user = smtp_cfg.get("user", "")
            password = smtp_cfg.get("password", "")
            if tls == "ssl":
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(host, port, timeout=12, context=context) as server:
                    if user:
                        server.login(user, password)
                    server.send_message(msg, to_addrs=recipients)
            else:
                with smtplib.SMTP(host, port, timeout=12) as server:
                    server.ehlo()
                    if tls == "starttls":
                        server.starttls(context=ssl.create_default_context())
                        server.ehlo()
                    if user:
                        server.login(user, password)
                    server.send_message(msg, to_addrs=recipients)
            return "sent", "to=%s" % ",".join(recipients)

        try:
            status, detail = await self.loop.run_in_executor(None, _send)
            LOG.info("email %s: %s", status, detail)
            return status, detail
        except Exception as exc:
            LOG.warning("email failed: %s", exc)
            return "error", str(exc)[:300]

    # ------------------------------------------------------------- dingtalk
    async def send_dingtalk(self, ding_cfg, body):
        webhook = ding_cfg.get("webhook", "")
        if not webhook:
            return "error", "webhook 为空"
        secret = ding_cfg.get("secret", "")
        url = webhook
        if secret:
            timestamp = str(int(time.time() * 1000))
            string_to_sign = "%s\n%s" % (timestamp, secret)
            digest = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(digest))
            url = "%s&timestamp=%s&sign=%s" % (webhook, timestamp, sign)
        payload = {"msgtype": "text", "text": {"content": body[:1800]}}
        mobiles = ding_cfg.get("at_mobiles", "")
        if mobiles:
            payload["at"] = {"atMobiles": [m.strip() for m in mobiles.split(",") if m.strip()],
                             "isAtAll": False}
        return await self._post_json(url, payload)

    # -------------------------------------------------------------- webhook
    async def send_webhook(self, url, payload):
        return await self._post_json(url, payload)

    async def _post_json(self, url, payload):
        def _post():
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(url, data=data, headers={
                "Content-Type": "application/json; charset=utf-8", "User-Agent": "ec20-sip-lite/1.0"})
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, resp.read(2048).decode("utf-8", "replace")

        try:
            status, text = await self.loop.run_in_executor(None, _post)
        except Exception as exc:
            LOG.warning("notify %s failed: %s", url[:40], exc)
            return "error", str(exc)[:300]
        if 200 <= status < 300:
            ok = True
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and parsed.get("errcode") not in (None, 0):
                    ok = False
                    text = str(parsed.get("errmsg", text))
            except ValueError:
                pass
            return ("sent" if ok else "error"), text[:300]
        return "error", "http %s: %s" % (status, text[:200])