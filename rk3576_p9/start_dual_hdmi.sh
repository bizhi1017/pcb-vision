#!/bin/sh
# Verified on the current board: Xorg :0, LightDM root authority,
# HDMI-1 1920x1080+0+0. Environment overrides support another session.
set -eu
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
export MALLOC_MMAP_THRESHOLD_="${MALLOC_MMAP_THRESHOLD_:-1048576}"
export MALLOC_TRIM_THRESHOLD_="${MALLOC_TRIM_THRESHOLD_:-262144}"
cd "$(dirname "$0")"
sh ./prepare_pcie_camera.sh
if [ -f hdmi_text.c ] && { [ ! -r libpcb_hdmi_text.so ] || [ hdmi_text.c -nt libpcb_hdmi_text.so ]; }; then
    sh ./build_hdmi_text.sh || echo 'HDMI Chinese label build unavailable; inspect overlay diagnostics.' >&2
fi
if [ -f hdmi_egl.c ] && { [ ! -r libpcb_hdmi_egl.so ] || [ hdmi_egl.c -nt libpcb_hdmi_egl.so ] || [ hdmi_egl_types.h -nt libpcb_hdmi_egl.so ]; }; then
    sh ./build_hdmi_egl.sh || echo 'HDMI GPU build unavailable; explicit OpenCV fallback will be used.' >&2
fi
if [ -f mpp_jpeg.c ] && { [ ! -r libpcb_mpp_jpeg.so ] || [ mpp_jpeg.c -nt libpcb_mpp_jpeg.so ]; }; then
    sh ./build_mpp_jpeg.sh || echo 'MPP JPEG build unavailable; CPU JPEG fallback will be used.' >&2
fi
if ! sh ./prepare_sd_storage.sh; then
    echo 'SD storage unavailable; PC screenshots and camera service remain available.' >&2
fi
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/var/run/lightdm/root/:0}"
if [ ! -r "$XAUTHORITY" ]; then
    echo "X11 authority unavailable: $XAUTHORITY" >&2
    exit 1
fi
if ! xrandr --query >/dev/null 2>&1; then
    echo "Cannot access the X11 desktop; check DISPLAY and XAUTHORITY." >&2
    exit 1
fi
exec python3 dual_camera_server.py --pcie --port 8001 --hdmi-window \
    --hdmi-x 0 --hdmi-y 0 --hdmi-width 1920 --hdmi-height 1080 "$@"
