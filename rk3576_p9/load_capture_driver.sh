#!/bin/sh
# Explicit user action only. Never unload a running module or stop an app.
set -eu
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo 'Run as root.' >&2; exit 1; }
[ ! -d /sys/module/anlogic_pci ] || { echo 'Old driver is still loaded. Stop the camera service and explicitly unload anlogic_pci first.' >&2; exit 1; }
[ -r driver/anlogic_pci.ko ] || { echo 'Missing candidate driver/anlogic_pci.ko.' >&2; exit 1; }
actual=$(modinfo -F vermagic driver/anlogic_pci.ko)
case "$actual" in "$(uname -r) "*) ;; *) echo 'Candidate kernel version does not match this board.' >&2; exit 1;; esac
port=/sys/bus/platform/devices/2a210000.pcie
if [ -d "$port" ] && [ ! -L "$port/driver" ]; then
    printf '%s' 2a210000.pcie > /sys/bus/platform/drivers/rk-pcie/bind
fi
attempt=0
while :; do
    count=0
    for pci in /sys/bus/pci/devices/*; do
        [ -r "$pci/vendor" ] && [ -r "$pci/device" ] || continue
        [ "$(cat "$pci/vendor")" = 0x1edb ] && [ "$(cat "$pci/device")" = 0xabcd ] || continue
        count=$((count+1))
        [ ! -L "$pci/driver" ] || { echo 'Camera device already bound.' >&2; exit 1; }
    done
    [ "$count" -eq 1 ] && break
    [ "$count" -eq 0 ] && [ "$attempt" -lt 100 ] || { echo "Expected one camera PCIe function, got $count." >&2; exit 1; }
    sleep .2;attempt=$((attempt+1))
done
insmod ./driver/anlogic_pci.ko interrupt_mode=1 poll_mode=0 completion_cpu=5 completion_realtime=1 enable_st_c2h_credit=1
udevadm settle
printf '%s\n' 'P8 candidate driver loaded. Run sh start_capture_modes.sh.'
