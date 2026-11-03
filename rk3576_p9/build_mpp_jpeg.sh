#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ -r /usr/include/rga/im2d.h ]; then
    cc -O3 -fPIC -shared -Wall -Wextra -DPCB_USE_RGA -I/usr/include/rga -I/usr/include/rockchip mpp_jpeg.c -lrockchip_mpp -lrga -o libpcb_mpp_jpeg.so.tmp
else
    cc -O3 -fPIC -shared -Wall -Wextra -I/usr/include/rockchip mpp_jpeg.c -lrockchip_mpp -o libpcb_mpp_jpeg.so.tmp
fi
mv libpcb_mpp_jpeg.so.tmp libpcb_mpp_jpeg.so
