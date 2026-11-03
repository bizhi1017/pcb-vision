#!/bin/sh
# CPU-only build, no capture or accelerator contexts.
set -eu
cd "$(dirname "$0")"
cc -O3 -std=c11 -fPIC -shared -Wall -Wextra -Werror -Wl,-Bsymbolic -Wl,--exclude-libs,libzstd.a fast_lossless.c -Wl,-Bstatic -lzstd -Wl,-Bdynamic -ldeflate -pthread -o libpcb_fast_lossless.so.tmp
mv libpcb_fast_lossless.so.tmp libpcb_fast_lossless.so
