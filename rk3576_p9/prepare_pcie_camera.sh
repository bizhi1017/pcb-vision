#!/bin/sh
# Only initialize this board's confirmed camera root port. Never unbind/reset.
set -eu
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo 'Run camera startup as root.' >&2; exit 1; }
count_camera() {
    found=0
    for entry in /sys/bus/pci/devices/*; do
        [ -r "$entry/vendor" ] && [ -r "$entry/device" ] || continue
        if [ "$(cat "$entry/vendor")" = 0x1edb ] && [ "$(cat "$entry/device")" = 0xabcd ]; then found=$((found+1)); fi
    done
}
count_camera
if [ "$found" = 0 ]; then
    port=/sys/bus/platform/devices/2a210000.pcie
    if [ -d "$port" ] && [ ! -L "$port/driver" ]; then
        echo 'Camera PCIe host is unbound; binding 2a210000.pcie after FPGA configuration.'
        printf '%s' 2a210000.pcie > /sys/bus/platform/drivers/rk-pcie/bind
        udevadm settle --timeout=5 || echo 'udev settle timed out; checking the camera PCIe function directly.' >&2
    fi
    # Rockchip probes the host asynchronously. bind returning and an empty
    # udev queue do not mean that link training / PCI enumeration has finished.
    echo 'Waiting up to 20s for camera PCIe enumeration...'
    attempts=0
    while [ "$found" = 0 ] && [ "$attempts" -lt 100 ]; do
        sleep 0.2
        count_camera
        attempts=$((attempts+1))
    done
fi
[ "$found" != 0 ] || { echo 'Camera PCIe [1edb:abcd] not found after 20s. Check the p6 AB bitstream and camera power, then rerun this startup command.' >&2; exit 1; }
[ "$found" = 1 ] || { echo "Expected one camera PCIe [1edb:abcd], found $found. Driver not loaded." >&2; exit 1; }
if [ ! -d /sys/module/anlogic_pci ]; then
    sh ./07_software/load_driver.sh
fi
for name in user c2h_0 c2h_1; do
    [ -c "/dev/ANLOGIC-PCI0_$name" ] || { echo "Camera device missing: /dev/ANLOGIC-PCI0_$name" >&2; exit 1; }
done
echo 'Camera PCIe and driver ready.'
