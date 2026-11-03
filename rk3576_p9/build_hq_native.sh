#!/bin/sh
set -eu
cd "$(dirname "$0")"
tune=
case "$(uname -m)" in aarch64) tune=-mtune=cortex-a72;; esac
"${CC:-cc}" -O3 $tune -std=c11 -fPIC -shared -pthread raw10_hq.c raw10_fused.c -o libraw10_hq.so.new
mv libraw10_hq.so.new libraw10_hq.so
echo "Built CPU-only libraw10_hq.so"
