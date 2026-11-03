#!/bin/sh
set -eu
cd "$(dirname "$0")"
"${CC:-cc}" -O3 -std=c11 -fPIC -shared -Wall -Wextra -Werror inference_prepare.c -o libpcb_inference_cpu.so.new
mv libpcb_inference_cpu.so.new libpcb_inference_cpu.so
