"""Box-side end-to-end check against a simulator instance (no hardware needed).

Run on the box after starting `python3 -m app.main --sim -c /tmp/t1/config.json`:

    python3 /tmp/t1/e2e.py /tmp/t1/x.log 16080 16060
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

LOG = sys.argv[1] if len(sys.argv) > 1 else "/tmp/t1/x.log"
WEB_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 16080
SIP_PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 16060
BASE = "http://127.0.0.1:%d" % WEB_PORT
ROOT = "/opt/ec20-sip-lite"

failures = []


def check(name, condition, detail=""):
    print(("  [ OK ] " if condition else "  [FAIL] ") + name + (" " + detail if detail else ""))
    if not condition:
        failures.append(name)


def admin_password():
    text = open(LOG, encoding="utf-8", errors="replace").read()
    match = re.search(r"admin / ([A-Za-z0-9]+)", text)
    return match.group(1) if match else ""


opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())


def api(path, payload=None, method=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    try:
        with opener.open(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


password = sys.argv[4] if len(sys.argv) > 4 else admin_password()
check("获取管理密码", bool(password), "(来自参数)" if len(sys.argv) > 4 else "(来自首次启动日志)")
status, body = api("/api/login", {"username": "admin", "password": password})
check("登录面板", status == 200 and body.get("ok"), str(body))

status, body = api("/api/status")
check("状态接口", status == 200 and body.get("modem", {}).get("present"), "modem.present=%s" % body.get("modem", {}).get("present"))

status, body = api("/api/extensions")
existing = [e for e in body.get("items", []) if e["number"] == "1002"]
if existing:
    check("分机 1002 已存在", True)
else:
    status, body = api("/api/extensions", {"number": "1002", "display_name": "E2E",
                                           "password": "e2epass", "enabled": 1})
    check("新增分机 1002", status == 200 and body.get("ok"), str(body))
    status, body = api("/api/extensions/%d" % body.get("id", 0), {"password": "e2epass", "enabled": 1}, "PUT")

ua = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "auto_answer.py"),
                       "--server", "127.0.0.1:%d" % SIP_PORT, "--user", "1002",
                       "--password", "e2epass", "--hold", "6"],
                      cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(3)
status, body = api("/api/extensions")
registered = any(e["number"] == "1002" and e["registered"] for e in body.get("items", []))
check("分机 1002 注册成功", registered)

status, body = api("/api/sim/incoming", {"number": "13800138000"})
check("触发模拟来电", status == 200 and body.get("ok"), str(body))
time.sleep(3)

status, body = api("/api/status")
calls = body.get("calls") or []
modem_calls = body.get("modem", {}).get("calls") or []
check("来电进入通话状态", bool(calls or modem_calls), json.dumps(calls or modem_calls, ensure_ascii=False))

# let the auto-answer UA hold and then hang up, so the CDR is written
out = b""
try:
    ua.wait(timeout=30)
    out = ua.stdout.read()
except subprocess.TimeoutExpired:
    ua.kill()
time.sleep(2)
text = out.decode("utf-8", "replace")
check("自动接听 UA 收到 INVITE/ACK", "INVITE from" in text and "ACK received" in text)

status, body = api("/api/cdr?limit=5")
items = body.get("items") or []
check("产生 CDR", bool(items), json.dumps(items[:1], ensure_ascii=False))

status, body = api("/api/sim/sms", {"peer": "10001", "text": "验证码 123456"})
check("模拟收到短信", status == 200 and body.get("ok"), str(body))
status, body = api("/api/sms?limit=5")
check("短信入库", (body.get("total") or 0) >= 1)

log = open(LOG, encoding="utf-8", errors="replace").read()
check("日志出现 bridge up", "bridge up" in log)

print()
if failures:
    print("E2E FAILED:", ", ".join(failures))
    sys.exit(1)
print("E2E PASSED")
