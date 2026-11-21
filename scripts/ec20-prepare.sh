#!/bin/sh
# ec20-prepare.sh — one-shot modem preparation for voice + SMS.
#
#   AT+QCFG="usbcfg",0x2C7C,0x0125,1,1,1,1,1,0,1   enable the USB audio class
#   AT+QCFG="ims",1                                enable IMS/VoLTE
#   AT+QAUDMOD=3                                   raw PCM audio profile
#   AT+CMGF=0 / AT+CNMI=2,1,0,0,0                  PDU-mode SMS + new-SMS URC
#
# The usbcfg change needs a module reboot (AT+CFUN=1,1). Values are stored in
# the module NVM, so this only has to run once (or after a factory reset).
set -u

PORT="${EC20_AT:-/dev/ec20-at}"
[ -e "$PORT" ] || PORT="/dev/ttyUSB2"
[ -e "$PORT" ] || { echo "no AT port ($PORT)"; exit 1; }

stty -F "$PORT" 115200 raw -echo 2>/dev/null

at() {
    printf '%s\r' "$1" > "$PORT"
    sleep 0.8
    timeout 1 cat < "$PORT" 2>/dev/null | tr -d '\r'
}

echo "== AT port: $PORT"
at 'AT' | tail -2
echo "== current configuration"
at 'AT+QCFG="usbcfg"'
at 'AT+QCFG="ims"'
at 'AT+QAUDMOD?'

echo "== enabling USB audio class (UAC) + IMS"
at 'AT+QCFG="usbcfg",0x2C7C,0x0125,1,1,1,1,1,0,1'
at 'AT+QCFG="ims",1'
at 'AT+QAUDMOD=3'

echo "== SMS in PDU mode with new-message indication"
at 'AT+CMGF=0'
at 'AT+CNMI=2,1,0,0,0'
at 'AT+CSCS="UCS2"'

echo "== rebooting module to apply usbcfg (this drops the AT port for ~20s)"
printf 'AT+CFUN=1,1\r' > "$PORT"
sleep 1

echo "done. re-run scripts/ec20-bind.sh after the module re-enumerates."