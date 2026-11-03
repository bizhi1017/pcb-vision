#!/bin/sh
# Compile only: no display, camera or GPU context is opened by this script.
set -eu
cd "$(dirname "$0")"
task_output=".libpcb_hdmi_egl.so.$$"
trap 'rm -f "$task_output"' EXIT HUP INT TERM
cc -std=c11 -O3 -fPIC -shared -Wall -Wextra -Werror hdmi_egl.c -o "$task_output" $(pkg-config --cflags --libs egl glesv2 x11)
mv "$task_output" libpcb_hdmi_egl.so
