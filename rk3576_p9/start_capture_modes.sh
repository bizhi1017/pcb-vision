#!/bin/sh
# User-invoked candidate entry. Keep the stable runtime and calibrations intact.
set -eu
cd "$(dirname "$0")"
capacity=/sys/module/anlogic_pci/parameters/frame_max_bytes
if [ ! -r "$capacity" ] || [ "$(cat "$capacity")" -lt 11000000 ]; then
    echo 'P9 large-frame DMA driver is not loaded. After stopping the old service and programming P9, run sh load_capture_driver.sh.' >&2
    exit 1
fi
python3 - <<'PY'
import os,socket,struct
from vid008_pcie import validate_capture_identity,P8_BUILD
try:
    connection=socket.create_connection(('127.0.0.1',8001),timeout=.3)
except OSError:pass
else:
    connection.close();raise SystemExit('Camera service port 8001 is already in use. Stop the old service first.')
fd=os.open('/dev/ANLOGIC-PCI0_user',os.O_RDONLY)
try:
    build=struct.unpack('<I',os.pread(fd,4,0x5c))[0]
    control=struct.unpack('<I',os.pread(fd,4,0x60))[0]
finally:os.close(fd)
validate_capture_identity(build,control)
if build!=P8_BUILD:raise SystemExit('This entry requires P9 BUILD 0x%08X; actual 0x%08X'%(P8_BUILD,build))
print('P9 BUILD 0x%08X; PCIe control registers ready.'%build,flush=True)
PY
[ -e 07_software ] || ln -s /home/rock/dual_camera/07_software ./07_software
if [ "${1:-}" = --headless ]; then
    shift
    exec sh ./start_dual_detect.sh --headless --calibration-dir /home/rock/dual_camera/calibrations "$@"
fi
exec sh ./start_dual_detect.sh --calibration-dir /home/rock/dual_camera/calibrations "$@"
