#!/bin/sh
# Explicit user-invoked preparation only; never stops an application or flashes.
set -eu
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo 'Run as root.' >&2; exit 1; }
if pgrep -f '[p]ython3.*dual_camera_server.py' >/dev/null; then
    echo 'Camera service is still running. Press Ctrl+C in its terminal and wait for the shell prompt first.' >&2
    exit 1
fi
quarantine=/sys/module/anlogic_pci/parameters/frame_ring_quarantined
if [ -r "$quarantine" ] && grep -q '[1-9]' "$quarantine"; then
    echo 'DMA idle timeout protection is active. Do not force rmmod or program the FPGA. Reboot RK3576, reconnect SSH, then rerun this script before programming P9.' >&2
    exit 1
fi
if [ -d /sys/module/anlogic_pci ]; then rmmod anlogic_pci; fi
if [ -L /sys/bus/platform/devices/2a210000.pcie/driver ]; then
    printf '%s' 2a210000.pcie > /sys/bus/platform/drivers/rk-pcie/unbind
fi
printf '%s\n' 'Camera PCIe unbound. Program the P9 bitstream (vid008_compact_p9_20261103.bit), then run sh load_capture_driver.sh and sh start_capture_modes.sh.'
