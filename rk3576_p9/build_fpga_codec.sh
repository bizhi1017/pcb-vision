#!/bin/sh
set -eu
cd "$(dirname "$0")"
case "$(uname -m)" in
 aarch64) cc -march=armv8-a+crc -std=c11 -O3 -fPIC -shared -Wall -Wextra -Werror fpga_codec.c -o libfpga_codec.so ;;
 *) cc -std=c11 -O3 -fPIC -shared -Wall -Wextra -Werror fpga_codec.c -o libfpga_codec.so ;;
esac
