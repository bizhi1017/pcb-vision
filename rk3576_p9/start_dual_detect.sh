#!/bin/sh
# Both channels use the existing raw-logit three-class model, one NPU context.
set -eu
# Keep variable multi-megabyte frame buffers out of unbounded glibc arenas.
# Explicit settings also prevent the dynamic mmap threshold growing to 32MB.
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
export MALLOC_MMAP_THRESHOLD_="${MALLOC_MMAP_THRESHOLD_:-1048576}"
export MALLOC_TRIM_THRESHOLD_="${MALLOC_TRIM_THRESHOLD_:-262144}"
cd "$(dirname "$0")"
sh ./prepare_pcie_camera.sh
model="${PCB_MODEL:-/home/rock/yolo/p07_near_raw_int8.rknn}"
classes="$(pwd)/pcb_classes.txt"
if [ -f mpp_jpeg.c ] && { [ ! -r libpcb_mpp_jpeg.so ] || [ mpp_jpeg.c -nt libpcb_mpp_jpeg.so ]; }; then
    sh ./build_mpp_jpeg.sh || echo 'MPP JPEG build unavailable; CPU JPEG fallback will be used.' >&2
fi
if [ -f raw10_hq.c ] && { [ ! -r libraw10_hq.so ] || [ raw10_hq.c -nt libraw10_hq.so ]; }; then
    sh ./build_hq_native.sh
fi
if [ ! -r "$model" ]; then
    echo "PCB model missing: $model (set PCB_MODEL to its actual path)" >&2
    exit 1
fi
if [ "${1:-}" = "--headless" ]; then
    shift
    if ! sh ./prepare_sd_storage.sh; then
        echo 'SD storage unavailable; PC screenshots and camera service remain available.' >&2
    fi
    exec python3 dual_camera_server.py --pcie --port 8001 --model "$model" --classes "$classes" "$@"
fi
exec sh ./start_dual_hdmi.sh --model "$model" --classes "$classes" "$@"
