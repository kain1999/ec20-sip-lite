#!/bin/sh
# ec20-bind.sh — attach the built-in usbserial_generic driver to a Quectel
# EC20/EC25 (2c7c:0125) and wait for the AT node.
#
# Why this exists: the hisi vendor kernel (4.4.35_ecoo_*) ships usbserial,
# qcserial and usbserial_generic built in, but no "option" driver and no table
# entry that matches the EC20-CE-HCLG composition (5 vendor interfaces).
# Writing the VID/PID to generic/new_id makes the kernel bind each interface,
# which creates /dev/ttyUSB0..4 (interface 2 = AT).
set -u

VID="2c7c"
PID="0125"
AT_LINK="/dev/ec20-at"
QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1
log() { [ "$QUIET" = "1" ] || echo "ec20-bind: $*"; }

# 1) wait for the device to appear on the bus
i=0
while [ "$i" -lt 50 ]; do
    if lsusb -d "${VID}:${PID}" >/dev/null 2>&1; then
        break
    fi
    i=$((i + 1))
    sleep 0.2
done

if ! lsusb -d "${VID}:${PID}" >/dev/null 2>&1; then
    log "no ${VID}:${PID} USB device"
    exit 0
fi

# 2) bind the generic usb-serial driver for every interface
NEW_ID="/sys/bus/usb-serial/drivers/generic/new_id"
if [ -e "$NEW_ID" ]; then
    printf '%s %s' "$VID" "$PID" > "$NEW_ID" 2>/dev/null || true
    log "new_id written"
else
    log "generic/new_id missing (kernel without usb-serial support?)"
    exit 1
fi

# 3) wait for the AT port symlink created by 99-ec20.rules
i=0
while [ "$i" -lt 60 ]; do
    if [ -e "$AT_LINK" ]; then
        log "AT port ready: $AT_LINK -> $(readlink -f "$AT_LINK" 2>/dev/null)"
        exit 0
    fi
    i=$((i + 1))
    sleep 0.25
done

# fallback: interface 2 may show up as a plain ttyUSB node
for t in /dev/ttyUSB2 /dev/ttyUSB3; do
    [ -e "$t" ] && { log "AT port without symlink: $t"; exit 0; }
done
log "AT port did not appear"
exit 1