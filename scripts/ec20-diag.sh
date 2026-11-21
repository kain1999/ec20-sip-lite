#!/bin/sh
# ec20-diag.sh — one-shot diagnosis for the EC20 + ec20-sip-lite stack.
# Prints everything needed to tell "module / driver / SIM / audio / service" apart.
set -u

AT="${EC20_AT:-/dev/ec20-at}"
[ -e "$AT" ] || AT="/dev/ttyUSB2"
SERVICE_ACTIVE=0
systemctl is-active --quiet ec20-sip 2>/dev/null && SERVICE_ACTIVE=1

section() { printf '\n=== %s ===\n' "$*"; }
at() {
    # the daemon owns the AT port exclusively: only probe when it is stopped
    if [ "$SERVICE_ACTIVE" = "1" ]; then
        return 0
    fi
    printf '%s\r' "$1" > "$AT" 2>/dev/null
    sleep 0.7
    timeout 1 cat < "$AT" 2>/dev/null | tr -d '\r'
}

section "service"
systemctl is-active ec20-sip 2>/dev/null || echo "inactive"
systemctl show -p MemoryCurrent ec20-sip 2>/dev/null

section "memory"
free -m | head -2
ps -o pid,rss,cmd -C python3 2>/dev/null | head -5

section "usb"
lsusb -d 2c7c:0125 || echo "module not on the bus"
for i in 0 1 2 3 4 5 6 7; do
    drv=$(readlink -f "/sys/bus/usb/devices/1-2:1.$i/driver" 2>/dev/null | xargs -r basename)
    [ -n "$drv" ] && echo "  iface $i -> $drv"
done

section "tty nodes"
ls -l /dev/ec20-at /dev/ttyUSB* 2>/dev/null || echo "no ttyUSB nodes (run ec20-bind.sh)"
readlink -f /dev/ec20-at 2>/dev/null

section "sound card"
grep -i -A1 'EC20\|USB-Audio' /proc/asound/cards 2>/dev/null || echo "no UAC card (AT+QCFG=\"usbcfg\",...,1 to enable)"
arecord -l 2>/dev/null | head -6

section "AT identity"
if [ "$SERVICE_ACTIVE" = "1" ]; then
    echo "service is running (owns $AT); modem state comes from the panel API:"
    curl -s -m 5 http://127.0.0.1:8080/api/status 2>/dev/null | tr ',' '\n' | grep -E '"(present|model|imei|iccid|sim_state|signal_dbm|reg_status|operator|ims|voice_ready)"' | sed 's/^/  /'
    echo "  (stop the service to probe AT directly: systemctl stop ec20-sip)"
else
    at 'AT'
    at 'ATI'
    at 'AT+CPIN?'
    at 'AT+CSQ'
    at 'AT+CREG?'
    at 'AT+COPS?'
fi

section "voice / SMS capability"
if [ "$SERVICE_ACTIVE" = "0" ]; then
    at 'AT+QPCMV=?'
    at 'AT+QAUDMOD?'
    at 'AT+QCFG="ims"'
    at 'AT+CIREG?'
    at 'AT+CMGF?'
    at 'AT+CPMS?'
    at 'AT+CSMS?'
    at 'AT+CSCA?'
else
    echo "skipped (service running)"
fi

section "audio self-test (1 s capture)"
if [ -e /dev/ec20-at ] && arecord -l >/dev/null 2>&1; then
    CARD=$(awk '/EC20|USB-Audio/{print $1; exit}' /proc/asound/cards 2>/dev/null | tr -d '[]')
    if [ -n "${CARD:-}" ]; then
        timeout 4 arecord -D "plughw:$CARD,0" -f S16_LE -r 8000 -c 1 -d 1 -q /tmp/ec20-diag.wav 2>/dev/null \
            && ls -l /tmp/ec20-diag.wav
    fi
fi

section "sip / web"
ss -tulnp 2>/dev/null | grep -E ':5060|:8080'

section "recent log"
tail -20 /var/log/ec20-sip.log 2>/dev/null