#!/bin/sh
# install.sh — deploy ec20-sip-lite on a Debian/Ubuntu ARM box (hinas / hi3798mv300)
#
#   sh install.sh            # install to /opt/ec20-sip-lite
#   sh install.sh --upgrade  # keep config + data, replace code
set -eu

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="${APP_DIR:-/opt/ec20-sip-lite}"
CONF_DIR="/etc/ec20-sip"
DATA_DIR="/var/lib/ec20-sip"
LOG_FILE="/var/log/ec20-sip.log"
UPGRADE=0
[ "${1:-}" = "--upgrade" ] && UPGRADE=1

echo "== ec20-sip-lite installer =="
[ "$(id -u)" = "0" ] || { echo "must run as root"; exit 1; }

echo "-- python / audio deps"
if ! command -v python3 >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq python3
fi
python3 - <<'PY'
import sys
assert sys.version_info >= (3, 7), sys.version
print("   python", sys.version.split()[0])
PY
command -v arecord >/dev/null 2>&1 || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq alsa-utils >/dev/null 2>&1 || true

echo "-- files"
mkdir -p "$APP_DIR" "$CONF_DIR" "$DATA_DIR"
if [ "$UPGRADE" = "0" ]; then
    rm -rf "$APP_DIR/app" "$APP_DIR/tools" "$APP_DIR/scripts" "$APP_DIR/packaging"
fi
cp -r "$SRC_DIR/app" "$APP_DIR/"
cp -r "$SRC_DIR/tools" "$APP_DIR/" 2>/dev/null || true
cp -r "$SRC_DIR/scripts" "$APP_DIR/" 2>/dev/null || true
cp -r "$SRC_DIR/packaging" "$APP_DIR/" 2>/dev/null || true
cp "$SRC_DIR/README.md" "$APP_DIR/" 2>/dev/null || true
mkdir -p "$APP_DIR/docs"
cp "$SRC_DIR"/docs/*.md "$APP_DIR/docs/" 2>/dev/null || true

if [ "$UPGRADE" = "0" ] || [ ! -f "$CONF_DIR/config.json" ]; then
    if [ -f "$SRC_DIR/config/config.example.json" ]; then
        cp "$SRC_DIR/config/config.example.json" "$CONF_DIR/config.json"
    fi
    python3 - "$CONF_DIR/config.json" <<'PY'
import json, socket, sys, os
path = sys.argv[1]
cfg = {}
if os.path.exists(path):
    try:
        cfg = json.load(open(path))
    except ValueError:
        cfg = {}
def ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9)); return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
cfg.setdefault("sip", {})["external_ip"] = cfg.get("sip", {}).get("external_ip") or ip()
json.dump(cfg, open(path, "w"), indent=2)
print("   config:", path, "advertising", cfg["sip"]["external_ip"])
PY
fi

echo "-- udev + helper scripts"
install -m 0755 "$APP_DIR/scripts/ec20-bind.sh" /usr/local/sbin/ec20-bind.sh
install -m 0755 "$APP_DIR/scripts/ec20-prepare.sh" /usr/local/sbin/ec20-prepare.sh
install -m 0644 "$APP_DIR/packaging/99-ec20.rules" /etc/udev/rules.d/99-ec20.rules
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger 2>/dev/null || true

echo "-- systemd units"
install -m 0644 "$APP_DIR/packaging/ec20-sip.service" /etc/systemd/system/ec20-sip.service
cat > /etc/systemd/system/ec20-bind.service <<'UNIT'
[Unit]
Description=Bind EC20 USB serial interfaces (usbserial_generic)
DefaultDependencies=no
After=systemd-udev-settle.service
Before=ec20-sip.service
Wants=systemd-udev-settle.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/ec20-bind.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable ec20-bind.service >/dev/null 2>&1 || true
systemctl start ec20-bind.service >/dev/null 2>&1 || true

echo "-- account"
python3 - "$DATA_DIR/ec20sip.db" <<'PY'
import os, sys
sys.path.insert(0, os.environ.get("APP_DIR", "/opt/ec20-sip-lite"))
from app.db import Db, hash_password, random_password
db = Db(sys.argv[1])
auth = db.get_setting("auth", {}) or {}
if not auth.get("password_hash"):
    password = random_password(10)
    db.set_setting("auth", {"username": "admin", "password_hash": hash_password(password)})
    print("   admin password:", password)
else:
    print("   admin account kept (use the panel to change the password)")
print("   web panel: http://<box-ip>:8080")
db.close()
PY

echo "-- service"
systemctl enable ec20-sip.service >/dev/null 2>&1 || true
if [ "$UPGRADE" = "1" ]; then
    systemctl restart ec20-sip.service
else
    systemctl start ec20-sip.service
fi
sleep 2
systemctl --no-pager --lines=5 status ec20-sip.service || true

echo
echo "install.sh finished."
echo "  config : $CONF_DIR/config.json"
echo "  data   : $DATA_DIR/ec20sip.db"
echo "  log    : $LOG_FILE"
echo "  panel  : http://$(hostname -I 2>/dev/null | awk '{print $1}'):8080"