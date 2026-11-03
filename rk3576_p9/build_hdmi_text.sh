#!/bin/sh
set -eu
cd "$(dirname "$0")"
task_output=".libpcb_hdmi_text.so.$$"
trap 'rm -f "$task_output"' EXIT HUP INT TERM
cc -std=c11 -O3 -fPIC -shared -Wall -Wextra -Werror hdmi_text.c -o "$task_output" $(pkg-config --cflags --libs pangocairo cairo)
mv "$task_output" libpcb_hdmi_text.so
