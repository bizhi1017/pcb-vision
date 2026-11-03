#!/bin/sh
# CPU-only build; never opens a capture/display/accelerator device.
set -eu
cd "$(dirname "$0")"
cc -O3 -std=c11 -fPIC -shared -Wall -Wextra -Werror fast_png.c -ldeflate -lz -pthread -o libpcb_fast_png.so.tmp
mv libpcb_fast_png.so.tmp libpcb_fast_png.so
