#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pc_qt/pcb_station.py —— PCB 缺陷检测系统 · PC 上位机（PyQt5 操作台）

这个程序是整台设备的"操作台"，所有信息都收在**同一个窗口**里：
    (1) 通过 USB 串口（J30 / CH9102F，115200 8N1）切 FPGA 里的 5 种图像算法、
        调 Sobel 阈值 —— 赛题要求 4 的"上位机切换图像算法"就是这一条；
    (2) 通过以太网看板卡推来的 MJPEG 实时画面（/stream.mjpg）；
    (3) 每 1 秒轮询 /api/status，把 FPGA 硬件 JPEG 的压缩率 / 码率 / SD 剩余空间显示出来，
        并把「覆盖率(本窗口抽样) / 字节每块」摆在同一区 —— cover = 本窗口编码块数 / 一帧满块数
        32400，即"这个窗口采到整幅图多少面积"(条带轮转后约 31%，跨帧约 13 帧才走满整幅)，
        所以只在抽样明显偏少(<25%)时橙色提醒 "(跨帧约 13 帧覆盖整幅)"，绝不暗示"覆盖率越低越糟"；
    (4) 缺陷图墙：每 2 秒轮询 /api/defects，新文件自动入墙（按文件名去重）、
        缩略图 160×90，可下载到 ./downloads/；
    (5) 「📷 截图存 SD」按钮：让板卡把当前帧存进 SD 卡（/api/snap），存完立即刷新图墙；
    (6) 事件日志面板：每 1 秒轮询 /api/events?since=N 增量拉取"检出/手动截图"事件，
        按 `14:22:33 [检出] defect_missing 0.91  → det_142233.jpg (86 KB)` 的格式逐行追加，
        最多保留 2000 行，可一键清空；
    (7) 「远程推送」分组：开关板卡主动推送（/api/push?enabled=）、改 PC 接收端地址
        （?url=，默认 http://<本机IP>:9000/upload）、发一张测试图（?test=1），
        状态行 `推送: 开 ｜ 队列 0 ｜ 成功 12 ｜ 失败 0` 只认 /api/push 回读，
        失败时红色小字显示 last_error；旧板卡没有这个路由 -> 整组置灰、文案 '--'；
    (8) 「手机监看」一行：`手机看: http://<板卡IP>:8000/m` + 「复制」按钮，
        装了可选库 qrcode 就再画一个 120×120 二维码（没装就只显示文字，绝不崩）；
        目标码率按 40 KB/s 一档显示（280/320/360/400/440），界面只显示回读值、不自己换算。

为什么算法切换只能走串口：
    FPGA 与 RK3576 之间只有单向 MIPI（FPGA→RK）和 PCIe，没有可用的控制侧信道，
    所以"PC 切 FPGA 算法"只能由 PC 自己的 USB 串口直连 FPGA 的 UART0。

板卡端 HTTP 契约（与 rk3576/rk_pcb_view.py 的 --http 服务一一对应）：
    GET /api/status   -> {"ok":true,"ratio":0.856,"kbps":10.8,"qsel":2,
                          "fps":14.0,"blocks":1440,"nbytes":13200,
                          "sd_free":30512078438,"save_dir":"/mnt/sdcard/defect",
                          "auto":0,"target_kbps":20,"autosave":1,"detect":1,
                          "cover":0.0444,"b_per_blk":9.17}
                          ratio 为 0~1 小数（对外显示时 x100 变百分数），无数据时为 null
                          cover = 本窗口编码块数 / 满帧块数(32400 = 240x135 一帧)，0~1
                          —— 即"这个窗口采到了整幅图多少面积"；分母是**一帧**满块数(不是 4 帧预算
                          129600，旧分母会把 p19 读成 7.7%)。条带轮转后单窗口约 31%，跨帧约 13 帧
                          才走满整幅；⚠ 旧位流(p18)重复编码会读 0.40，那是虚高、不是异常；
                          b_per_blk = nbytes/blocks（字节/块，blocks=0 时 null）；
                          这两个字段只有新版板卡才有，旧板卡缺失时本界面显示 '--'
    GET /snap.jpg     -> 当前帧 JPEG（只读快照，不落盘）
    GET /stream.mjpg  -> multipart/x-mixed-replace，边界字符串固定 "frame"
    GET /api/defects  -> {"files":[{"name":"det_142233.jpg","size":123456,"mtime":1712345678}]}
    GET /defects/<name> -> 下载单张缺陷图
    GET /api/events?since=N -> {"next":M,"events":[{seq,t,type,file,size,cls,score,ratio,fps,mode}]}
                          type 为 "detect" | "manual"；since=N 表示"只要 seq > N 的"
    GET /api/snap     -> {"ok":true,"file":"manual_142307.jpg","size":86321,
                          "dir":"/mnt/sdcard/defect"}（无帧时 {"ok":false,"error":"no frame"} + 503）
    GET/POST /api/push?enabled=0|1 -> {"ok":true,"enabled":..,"url":..,"changed":[..]}（开关推送）
    GET/POST /api/push?url=<http(s)://...>  -> 同上（非法 url -> 400 {"ok":false,"error":"bad url"}）
    GET/POST /api/push?test=1      -> 同上 + test_queued:1（板卡立刻推一张测试图；没开/没帧 -> 0）
    GET /api/push     -> {"enabled":0|1,"url":"...","queued":N,"ok":N,"fail":N,"last_error":"..."}
                          注意 GET 里的 **ok/fail 是"成功/失败次数"（整数）**，不是请求状态；
                          旧板卡没有这个路由 -> 404：本界面整组置灰并提示"板卡版本过旧"
    GET /m            -> 手机低带宽页（大字状态 + 低带宽画面 + 缺陷墙 + 告警提示音）：
                          本界面只给出地址 http://<板卡IP>:8000/m 和可选二维码
    默认端口 8000。

依赖：
    PC 上需要 PyQt5 + pillow + pyserial：
        pip install PyQt5 pillow pyserial
    网络只用标准库 urllib，**不需要 cv2 / requests**。
    二维码是**可选**：装了 qrcode 就画，没装只在界面上显示文字地址（不写进 requirements）。
    本机（开发机）没有 PyQt5，GUI 无法在这里跑；因此本文件额外提供
    `--selftest`，它在不 import PyQt5、不接板卡的情况下验证全部纯逻辑
    （串口命令映射 / MJPEG 多段流解析 / URL 构造 / 缺陷 JSON 解析 / 缩略图与下载路径 /
      /api/push 推送解析 / 手机监看地址 / 二维码可选依赖 / 目标码率 40 KB/s 单位）。

用法：
    python pc_qt/pcb_station.py                     # 正常启动（默认 192.168.10.50:8000）
    python pc_qt/pcb_station.py --ip 192.168.1.51 --port 8000
    python pc_qt/pcb_station.py --serial COM5       # 启动后自动连串口
    python pc_qt/pcb_station.py --selftest          # 自检（不需要 PyQt5 / 板卡）
"""

from __future__ import print_function

import argparse
import collections
import io
import json
import os
import re
import sys
import threading
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

# ============================================================================
# 0. 常量（与 fpga/p3_link_rk/uisrc/01_rtl/pre/pre_ctrl.v 严格一致）
# ============================================================================

APP_TITLE = 'PCB 缺陷检测上位机 (FPGA 算法切换 + MJPEG 预览 + 缺陷图墙)'

DEFAULT_BOARD_IP = '192.168.10.50'      # 网线直连：PC 192.168.10.100 <-> 板卡 192.168.10.50
#   ⚠ 不要用 192.168.1.x：很多家用 WiFi 路由器就占这个网段，
#     Windows 会把包从 WiFi 发出去，导致 ping 不通板卡。
DEFAULT_BOARD_PORT = 8000
DEFAULT_DOWNLOAD_DIR = 'downloads'     # 相对当前工作目录的 ./downloads/

SERIAL_BAUD = 115200                   # pre_ctrl.v: 115200 8N1 无流控
SERIAL_TIMEOUT = 0.2                   # 读超时（秒）
SERIAL_WRITE_TIMEOUT = 0.5             # 写超时（秒）

# 模式号 -> 命令字符 -> 显示名（顺序即 pre_ctrl.v 的 '0'..'4'）
MODE_KEYS = ('0', '1', '2', '3', '4')
MODE_NAMES = (
    '原图 (RGB 直通)',
    '灰度',
    '灰度 + 3x3 高斯',
    '灰度 + Sobel 边缘',
    '灰度 + 直方图均衡',
)
THRESH_STEP = 32                       # pre_ctrl.v: '+' / '-' 每次 32，范围 8~2040

# ---- 压缩质量（t7 RTL 契约：Q=手动并循环四档、A=自适应、B=循环目标码率） ----
# 命令字符必须与 fpga/p3_link_rk/uisrc/01_rtl/pre/pre_ctrl.v 完全一致；
# FPGA 侧**没有** Q/A/B 的串口回包，反馈走画面水印 → 板卡 /api/status → 本界面。
QSEL_NAMES = ('Q30', 'Q50', 'Q75', 'Q90')       # qsel 0~3 对应的 JPEG 质量档
QUALITY_PRESETS = (30, 50, 75, 90)              # 仅供显示，与 QSEL_NAMES 一一对应
# 目标码率的**单位**：p20+ 位流每个 target5 单位 = 40 KB/s（旧位流是 5 KB/s/单位）。
# 界面**只显示** /api/status.target_kbps（板卡已经换算好），绝不自己乘单位：
# 拿新脚本读旧位流会看到 35/40/45/50/55 这类值（放大了 8 倍），那是板卡/位流的事，不是界面猜。
TARGET_STEP_KBPS = 40                           # 每个 target5 单位 = 40 KB/s（p20+）
BITRATE_PRESETS_KBPS = tuple(TARGET_STEP_KBPS * n for n in (7, 8, 9, 10, 11))
#   -> (280, 320, 360, 400, 440) KB/s，'B' 键的循环顺序
QUALITY_PRESS_GAP_MS = 250                      # 连发 'Q' 的间隔（契约要求 ≥200ms）
QUALITY_VERIFY_DELAY_MS = 1200                  # 连发完等一轮 /api/status 再回读核对
QUALITY_MAX_ATTEMPTS = 3                        # 最多几轮（每轮 ≤4 发 Q）

# ---- 远程推送（板卡 /api/push：把缺陷图主动 POST 到 PC 上的接收端）+ 手机监看页 ----
# 板卡契约：GET/POST /api/push?enabled=0|1&url=http(s)://...&test=1（不传参 = 只查询）
#   GET  -> {"enabled":0|1,"url":"...","queued":N,"ok":N,"fail":N,"last_error":"..."}
#   POST -> {"ok":true,"enabled":..,"url":..,"changed":[...]}；非法 url -> HTTP 400 bad url
# 旧板卡没有这个路由 -> HTTP 404：界面**整组置灰**并显示 '--'，点击只提示"版本过旧"。
PUSH_DEFAULT_PORT = 9000                        # PC 接收端默认端口（tools/recv_upload.py）
PUSH_DEFAULT_PATH = '/upload'                   # PC 接收端默认路径
PUSH_URL_PLACEHOLDER = 'http://192.168.10.100:9000/upload'   # 拿不到本机 IP 时的占位
PUSH_TIMEOUT = 5.0                              # /api/push 请求超时（板卡侧单次投递 3s）
PUSH_POLL_INTERVAL = 1.0                        # /api/push 轮询周期（秒），跟状态轮询一起
PUSH_STATE_KEYS = ('enabled', 'url', 'queued', 'ok', 'fail', 'last_error')
# [D29] 点了「开/关」之后等板卡回读确认的最长时间：超时就解除置灰并提示，
#   否则板卡不确认（例如目标 URL 为空被拒）时按钮会永久不可点（真机踩过）。
PUSH_PENDING_TIMEOUT = 3.0
MOBILE_PAGE_PATH = 'm'                          # 板卡手机低带宽页：GET /m
MOBILE_QR_SIZE = 120                            # 状态区二维码边长（点击弹 400x400 大图）
MOBILE_QR_BIG_SIZE = 400                        # 点二维码弹出的大图边长（远距离扫）
QR_NO_WIRELESS_WARN = '板卡未连 WiFi：手机扫码可能打不开'   # 没有无线 IP 时的橙色警告
QR_UNAVAILABLE_HINT = '二维码不可用（pip install segno）'    # 缺可选库时的占位文字
# ---- 网卡枚举（板卡 GET /api/net，t14）：给二维码挑"手机能连到的板卡 IP" ----
NET_TIMEOUT = 3.0                               # /api/net 请求超时（只在连上板卡时取一次）
PUSH_URL_AUTO_HINT = ('（该地址是板卡能走到的本机 IP；若无线/有线切换过，'
                      '点"应用"前的建议值会自动更新）')

BOUNDARY = b'frame'                    # MJPEG 边界字符串（契约固定 "frame"）
MJPEG_MAX_BUFFER = 8 * 1024 * 1024     # 解析缓冲上限，超了直接丢（防内存涨爆）

THUMB_W, THUMB_H = 160, 90             # 缺陷缩略图尺寸

STATUS_POLL_INTERVAL = 1.0             # /api/status 轮询周期（秒）
EVENTS_POLL_INTERVAL = 1.0             # /api/events 增量轮询周期（秒）
DEFECT_POLL_INTERVAL = 2.0             # 缺陷图墙自动刷新周期（秒）
LOG_MAX_LINES = 2000                   # 事件日志面板最多保留的行数
HTTP_TIMEOUT = 2.0                     # 状态/列表请求超时
STREAM_TIMEOUT = 5.0                   # MJPEG 流 socket 超时
DOWNLOAD_TIMEOUT = 10.0                # 缺陷图单项下载超时

# ---- 覆盖率 / 字节/块（t4/t6/t8）：/api/status 的 cover 与 b_per_blk ----
# cover = 本窗口编码块数 / **一帧满块数(240x135 = 32400)**，即"这个窗口采到了整幅图多少面积"。
# 分母是**一帧**满块数（板卡 5d7337a 起），不是 4 帧预算 129600：
#   旧分母会把 p19 读成 9960/129600 = 7.7%，误导成"比修前更差"；正确读数是 9960/32400 = 30.7%。
# 它仍然不是"整幅图覆盖率"，而是一个窗口的抽样：
#   · 修前(p18/t2 之前)一个窗口只重复编同一批条带 -> 单窗口 ~10%，永远编不到别的条带；
#   · t2 条带轮转后单窗口 ~31%，跨帧约 13 帧(12 帧 92%)才走满整幅 —— 满覆盖靠 RTL 轮转保证；
#   · ⚠ 旧位流(p18)用新分母会读 12960/32400 = 0.40 —— 那是**重复编码造成的虚高**，已知口径不是 bug。
# 所以判据不是"越高越好"：只有单窗口抽样明显偏少(<25%，对应修前的 10%)才橙色提醒，
# 修后的 30.7% 不报警（否则状态行会永久橙色，反而误导人）。
COVER_FULL_FRAME_BLOCKS = 240 * 135    # 32400 = 一帧满块数，板卡 cover 的分母（PC 只用它做口径文档/自检）
COVER_LOW_THRESHOLD = 0.25             # 单窗口抽样 < 25% 才算"明显偏少" -> 标橙 + 13 帧说明
COVER_WARN_COLOR = '#d2691e'           # 橙色（刻意区别于红色错误 #c00000）
COVER_FULL_FRAMES_HINT = '跨帧约 13 帧覆盖整幅'   # 低抽样时的口径说明后缀
COVER_MISSING_HINT = '旧版板卡无此字段'           # 旧板卡(13 字段)没有 cover/b_per_blk

# 注意：HTTP 头部只能是 latin-1，User-Agent 里**不能**写中文
_USER_AGENT = 'pcb-station/1.0 (PyQt5)'


# ============================================================================
# 1. 纯逻辑层：串口命令 / 回传解析（不依赖 PyQt5，可被 --selftest 直接覆盖）
# ============================================================================

class SerialCommandError(ValueError):
    """串口命令非法（例如模式号越界）"""


def mode_key(idx):
    """模式号(0~4) -> 串口命令字符 '0'~'4'（与 pre_ctrl.v 的 rx_data 比较一致）"""
    try:
        i = int(idx)
    except (TypeError, ValueError):
        raise SerialCommandError('模式号必须是整数, 收到 %r' % (idx,))
    if i < 0 or i >= len(MODE_KEYS):
        raise SerialCommandError('模式号越界: %d (只允许 0~%d)' % (i, len(MODE_KEYS) - 1))
    return MODE_KEYS[i]


def mode_command(idx):
    """模式号 -> 待发送的串口字节，如 3 -> b'3'"""
    return mode_key(idx).encode('ascii')


def dump_command():
    """JPEG 码流输出开关命令（FPGA 侧 'J' 切换，默认关闭）
    —— 关闭时 J30 串口只跑文本(MODE=/TH=/F=)，打开后同一根线会混入
       二进制 JPEG 码流（终端会刷屏，属正常）。"""
    return b'J'


def thresh_command(step):
    """阈值步进 -> 串口字节：正数发 '+', 负数发 '-'（pre_ctrl.v 只认这两个字符）"""
    try:
        s = int(step)
    except (TypeError, ValueError):
        raise SerialCommandError('阈值步进必须是整数, 收到 %r' % (step,))
    if s > 0:
        return b'+'
    if s < 0:
        return b'-'
    raise SerialCommandError('阈值步进不能为 0（只会发 + 或 -）')


def quality_command():
    """手动档位：'Q' —— FPGA 每收一次就切到手动并循环 0→1→2→3→0。

    ⚠ 正因为 FPGA 是"循环切档"，点 Q30 不代表立刻就是 Q30：界面上必须按
      /api/status 回读的 qsel 高亮，绝不能按点击猜（t7 契约 + t10 验收点）。
    """
    return b'Q'


def auto_command():
    """自适应开关：'A' —— 切到自适应码率模式（回读 /api/status.auto 确认）"""
    return b'A'


def bitrate_command():
    """目标码率：'B' —— 循环 280/320/360/400/440 KB/s（每档 40 KB/s，回读 /api/status.target_kbps 确认）"""
    return b'B'


def _qsel_int(value):
    """把 qsel 的各种写法压成 0~3 的 int；认不出来返回 None（未知就显示 --）"""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if 0 <= number < len(QSEL_NAMES) else None


def qsel_name(qsel):
    """qsel -> 'Q30'/'Q50'/'Q75'/'Q90'；未知返回 '--'"""
    index = _qsel_int(qsel)
    return QSEL_NAMES[index] if index is not None else '--'


def quality_checked_index(qsel):
    """qsel -> 应该高亮的档位按钮下标（0~3）；未知返回 None（四个都不高亮）"""
    return _qsel_int(qsel)


def plan_quality_presses(current, target):
    """
    从 current 档旋到 target 档需要按几次 'Q'（FPGA 每按一次 +1 且到 3 回 0）：
        0    = 已经在目标档，一发都不用发
        1~3  = 需要连发的次数
        None = 当前档位未知（先发 1 发把真实档位暴露出来，下一轮回读后再按需补发）
    """
    target_index = _qsel_int(target)
    if target_index is None:
        raise SerialCommandError('目标质量档位越界: %r（只允许 0~3）' % (target,))
    current_index = _qsel_int(current)
    if current_index is None:
        return None
    return (target_index - current_index) % len(QSEL_NAMES)


def next_bitrate_preset(current_kbps):
    """'B' 键循环的下一个目标码率（仅用于状态栏的乐观提示，真实值看回读）"""
    presets = BITRATE_PRESETS_KBPS
    current = _as_int(current_kbps)
    if current not in presets:
        return presets[0]
    return presets[(presets.index(current) + 1) % len(presets)]


def parse_serial_line(line):
    """
    解析 FPGA 回传的一行 ASCII（pre_ctrl.v 的 4 种消息）：
        "PRE-OK"  -> ('banner', None)   上电/心跳握手，说明串口与波特率都对
        "MODE=n"  -> ('mode', n)        当前模式
        "TH=nnn"  -> ('thresh', n)      Sobel 阈值
        "F=nn"    -> ('fps', n)         FPGA 侧帧率
    其它一律 ('unknown', 原文)，不抛异常（串口上什么都可能来）。
    """
    text = (line or '').strip()
    if not text:
        return ('empty', None)
    if text == 'PRE-OK':
        return ('banner', None)
    if text.startswith('MODE='):
        value = text[5:].strip()
        if value.isdigit():
            return ('mode', int(value))
        return ('unknown', text)
    if text.startswith('TH='):
        value = text[3:].strip()
        if value.isdigit():
            return ('thresh', int(value))
        return ('unknown', text)
    if text.startswith('F='):
        value = text[2:].strip()
        if value.isdigit():
            return ('fps', int(value))
        return ('unknown', text)
    return ('unknown', text)


# ============================================================================
# 2. 纯逻辑层：URL 构造
# ============================================================================

def normalize_board_ip(ip):
    """把用户填的各种写法统一成主机名/IP： '192.168.10.50' / 'http://192.168.10.50:8000/'"""
    text = str(ip or '').strip()
    if not text:
        raise ValueError('板卡 IP 不能为空')
    if '://' in text:
        text = text.split('://', 1)[1]
    text = text.split('/', 1)[0].strip()
    if not text:
        raise ValueError('板卡 IP 非法: %r' % (ip,))
    return text


def normalize_port(port):
    """端口校验，空值用默认 8000"""
    if port is None or str(port).strip() == '':
        return DEFAULT_BOARD_PORT
    try:
        value = int(str(port).strip())
    except (TypeError, ValueError):
        raise ValueError('端口必须是整数, 收到 %r' % (port,))
    if value <= 0 or value > 65535:
        raise ValueError('端口超出范围: %d' % value)
    return value


def build_base(ip=DEFAULT_BOARD_IP, port=DEFAULT_BOARD_PORT):
    """构造板卡基地址，如 http://192.168.10.50:8000（不带尾部斜杠）"""
    host = normalize_board_ip(ip)
    port_value = normalize_port(port)
    # 允许用户把端口写进 IP 栏（192.168.10.50:8000）
    if ':' in host:
        host_part, _, port_part = host.rpartition(':')
        if port_part.strip().isdigit() and host_part:
            host, port_value = host_part.strip(), normalize_port(port_part)
    return 'http://%s:%d' % (host, port_value)


def _join(base, path):
    return str(base).rstrip('/') + '/' + str(path).lstrip('/')


def url_status(base):
    return _join(base, 'api/status')


def url_snapshot(base):
    return _join(base, 'snap.jpg')


def url_stream(base):
    return _join(base, 'stream.mjpg')


def url_defects(base):
    return _join(base, 'api/defects')


def url_defect_file(base, name):
    """单张缺陷图 URL；文件名做 basename + URL 编码（防路径穿越、支持中文名）"""
    return _join(base, 'defects/' + quote(safe_basename(name), safe=''))


def url_events(base, since=0):
    """事件增量拉取 URL：since=N 表示"只要 seq > N 的事件"（契约）"""
    try:
        seq = int(since)
    except (TypeError, ValueError):
        seq = 0
    return _join(base, 'api/events?since=%d' % max(0, seq))


def url_snap(base):
    """让板卡把当前帧存进 SD 卡（返回 JSON，不是图片字节）"""
    return _join(base, 'api/snap')


def url_config(base, autosave=None, detect=None, dim=None):
    """
    板卡功能开关：`GET /api/config?autosave=0|1&detect=0|1&dim=0|1`
      - 三个参数都可选，**只改传了的那个**；都不传 = 只查询（契约 t11 / t18）
      - 参数顺序固定 autosave → detect → dim，便于日志/自检比对
      - dim 与 detect 在板卡侧**互斥**：dim=1 会自动 detect=0（反之亦然），
        界面按 exclusive_params() 把两个参数一次带齐，省一次往返
    """
    query = []
    for name, value in (('autosave', autosave), ('detect', detect), ('dim', dim)):
        if value is None:
            continue
        try:
            flag = int(value)
        except (TypeError, ValueError):
            raise SerialCommandError('%s 只能是 0 或 1, 收到 %r' % (name, value))
        if flag not in (0, 1):
            raise SerialCommandError('%s 只能是 0 或 1, 收到 %r' % (name, value))
        query.append('%s=%d' % (name, flag))
    path = 'api/config'
    if query:
        path += '?' + '&'.join(query)
    return _join(base, path)


def url_dim(base, run=None, calib=None):
    """
    尺寸测量结果：`GET /api/dim`（只读缓存，不测量、不阻塞 —— 界面轮询走这条）
      `?run=1`  = 用最新帧**立刻量一次**（点「测量」按钮走这条，板卡侧约 0.3~0.5 s）
      `?calib=1`= 最新帧后台重标一次（不阻塞）

    ⚠ 测量是**触发式**的：`measure()` 在 PC 上 ~0.46 s/帧，逐帧跑会把帧率打到 2~3 fps。
      所以定时轮询**绝不能**带 run=1（只读缓存就是 0 ms）；要新数据就让操作员点按钮。
    """
    query = []
    for name, value in (('run', run), ('calib', calib)):
        if value is None:
            continue
        try:
            flag = int(value)
        except (TypeError, ValueError):
            raise SerialCommandError('%s 只能是 0 或 1, 收到 %r' % (name, value))
        if flag not in (0, 1):
            raise SerialCommandError('%s 只能是 0 或 1, 收到 %r' % (name, value))
        query.append('%s=%d' % (name, flag))
    path = 'api/dim'
    if query:
        path += '?' + '&'.join(query)
    return _join(base, path)


def url_push(base, enabled=None, url=None, test=None):
    """
    板卡主动推送：`GET/POST /api/push?enabled=0|1&url=...&test=1`
      - 三个参数都可选，**只改传了的那个**；都不传 = 只查询（契约：GET 恰好 6 个字段）
      - 参数顺序固定 enabled → url → test，既方便日志也方便自检比对
      - url 里的 ':'、'/' 会被百分号编码（板卡会 unquote，等价于原文）
    """
    query = []
    if enabled is not None:
        try:
            flag = int(enabled)
        except (TypeError, ValueError):
            raise SerialCommandError('enabled 只能是 0 或 1, 收到 %r' % (enabled,))
        if flag not in (0, 1):
            raise SerialCommandError('enabled 只能是 0 或 1, 收到 %r' % (enabled,))
        query.append('enabled=%d' % flag)
    if url is not None:
        query.append('url=' + quote(str(url).strip(), safe=''))
    if test is not None:
        try:
            flag = int(test)
        except (TypeError, ValueError):
            raise SerialCommandError('test 只能是 0 或 1, 收到 %r' % (test,))
        if flag not in (0, 1):
            raise SerialCommandError('test 只能是 0 或 1, 收到 %r' % (test,))
        query.append('test=%d' % flag)
    path = 'api/push'
    if query:
        path += '?' + '&'.join(query)
    return _join(base, path)


def url_mobile(base):
    """板卡手机低带宽页：`http://<板卡IP>:<端口>/m`（大字状态 + 低带宽画面 + 告警提示音）"""
    return _join(base, MOBILE_PAGE_PATH)


def is_http_url(text):
    """是不是 http/https 的绝对地址（板卡 /api/push 只认这两种，其它一律 400 bad url）"""
    text = str(text or '').strip()
    for scheme in ('http://', 'https://'):
        if text.lower().startswith(scheme):
            return bool(text[len(scheme):].strip())
    return False


def local_ip_address():
    """
    猜本机对外通信用的那块网卡 IP（PC 接收端默认地址用），拿不到就返回 ''。

    做法：UDP "连接" 一个不会真的发包的地址（8.8.8.8:53），读内核选出来的源地址。
    全程 try/except：没网卡、被防火墙挡、沙箱里不许建 socket 都只是返回 ''，
    界面会退回到占位地址 PUSH_URL_PLACEHOLDER —— 绝不因为猜不到 IP 就崩。
    """
    sock = None
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.2)
        sock.connect(('8.8.8.8', 53))
        addr = sock.getsockname()[0]
        addr = '' if not isinstance(addr, str) else addr.strip()
        return '' if addr.startswith('127.') else addr
    except Exception:
        return ''
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass


def default_push_url(ip=None):
    """
    ⚠ **老口径（t15 起界面不再用它当默认值）**：本机"第一个网卡"的 IP。

    问题是那块网卡常常是 WiFi（如 192.168.1.106），而板卡是从**网线**过来的，
    根本回连不到这个地址 —— 用户报的"目标 URL 默认值填错"就是这个坑。
    界面现在用 push_url_for_board()（路由探测"板卡能到达的本机 IP"）；
    这个函数只为兼容老调用/自检保留。

    拿不到本机 IP 就用占位 `http://192.168.10.100:9000/upload`（网线直连那台 PC 的地址）。
    """
    addr = ip if ip is not None else local_ip_address()
    addr = str(addr or '').strip() or '192.168.10.100'
    return 'http://%s:%d%s' % (addr, PUSH_DEFAULT_PORT, PUSH_DEFAULT_PATH)


# ============================================================================
# 3. 纯逻辑层：MJPEG（multipart/x-mixed-replace）解析
# ============================================================================

class MjpegParser(object):
    """
    增量式 MJPEG 解析器：喂进任意切分的字节，吐出完整的 JPEG 帧。

    做法：在缓冲区里找 JPEG 的 SOI(FFD8) ... EOI(FFD9)。
    - 每段 multipart 里真正的图像数据就是 SOI 到 EOI，和 boundary/头部字段无关，
      所以直接按 SOI/EOI 取帧最稳（boundary 只是分段用的分隔线）；
    - 只找到 SOI、还没等到 EOI 的帧**不输出**（留在缓冲里等下一批数据补齐）；
    - 找不到 SOI 的垃圾字节直接丢；缓冲区超过上限也直接丢（板卡重启/错流保护）。
    """

    SOI = b'\xff\xd8'
    EOI = b'\xff\xd9'

    def __init__(self, boundary=BOUNDARY, max_buffer=MJPEG_MAX_BUFFER):
        self.boundary = boundary
        self.max_buffer = int(max_buffer)
        self.buf = bytearray()
        self.frames = 0            # 累计完整帧数
        self.dropped = 0           # 累计丢弃的垃圾字节数
        self.incomplete = 0        # 当前缓冲里是否有"半帧"

    def reset(self):
        self.buf = bytearray()
        self.incomplete = 0

    def feed(self, data):
        """喂一批字节，返回本批凑齐的完整 JPEG 列表（每个元素是 bytes）"""
        if data:
            self.buf.extend(data)
        out = []
        while True:
            soi = self.buf.find(self.SOI)
            if soi < 0:
                # 没有 SOI：只保留最后 1 字节（可能是被切开的 FFD8 的 FF）
                if len(self.buf) > 1:
                    self.dropped += len(self.buf) - 1
                    del self.buf[:len(self.buf) - 1]
                break
            if soi > 0:
                self.dropped += soi
                del self.buf[:soi]
            eoi = self.buf.find(self.EOI, len(self.SOI))
            if eoi < 0:
                break                      # 帧还不完整，等下一批
            frame = bytes(self.buf[:eoi + len(self.EOI)])
            del self.buf[:eoi + len(self.EOI)]
            self.frames += 1
            out.append(frame)
        self.incomplete = 1 if self.buf.find(self.SOI) >= 0 else 0
        if len(self.buf) > self.max_buffer:
            self.dropped += len(self.buf)
            del self.buf[:]
            self.incomplete = 0
        return out


# ============================================================================
# 4. 纯逻辑层：/api/status 与 /api/defects 的 JSON 解析
# ============================================================================

def _decode_text(payload):
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload).decode('utf-8', 'replace')
    return '' if payload is None else str(payload)


def _as_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value):
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_bool01(value):
    """auto 字段容错：True/1/'1'/'auto'/'on' -> 1；False/0/'manual'/'off' -> 0；认不出 -> None"""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        return 1 if int(float(value)) else 0
    except (TypeError, ValueError):
        pass
    text = str(value).strip().lower()
    if text in ('auto', 'true', 'on', 'yes', '自适应'):
        return 1
    if text in ('manual', 'false', 'off', 'no', '手动'):
        return 0
    return None


def parse_status_json(payload):
    """
    解析 /api/status 的 JSON，返回统一字段的 dict：
        ok(bool) ratio(0~1 或 None) ratio_pct(str, '85.6%' 或 '--')
        kbps(float) qsel(int|None) fps(float) blocks(int|None) nbytes(int|None)
        sd_free(int|None, 字节) save_dir(str, 板卡保存目录)
        auto(int 0/1|None) target_kbps(int|None)
        autosave(int 0/1|None) detect(int 0/1|None)
        cover(float 0~1|None) b_per_blk(float|None) warning(str) raw(原始 dict)
    容错：ratio 若被写成百分数(>1)自动 /100；旧版板卡脚本没有 auto/target_kbps 或
    autosave/detect 时这些字段给 None（界面上显示 '--'、按钮置灰），绝不因为缺字段报错。
    cover / b_per_blk 是 t1 新增的（`覆盖率` = 本窗口编码块数 / 一帧满块数 32400，即"这个窗口
    采到整幅图多少面积"；`字节/块`）：缺失或 null 都给 None，界面显示 '-- (旧版板卡无此字段)'
    —— 旧板卡（13 字段）必须照常工作，这是本文件最老的兼容底线。
    """
    text = _decode_text(payload)
    try:
        obj = json.loads(text)
    except Exception as exc:
        raise ValueError('板卡 /api/status 返回的不是合法 JSON (%s): %r'
                         % (exc, text[:120]))
    if not isinstance(obj, dict):
        raise ValueError('/api/status 应该是 JSON 对象, 实际是 %s' % type(obj).__name__)
    ratio = _as_float(obj.get('ratio'))
    if ratio is not None:
        if ratio > 1.0:            # 有的实现直接给百分数，兼容一下
            ratio = ratio / 100.0
        ratio = max(0.0, min(1.0, ratio))
    kbps = _as_float(obj.get('kbps'))
    fps = _as_float(obj.get('fps'))
    # ---- t1 新增：cover(单窗口抽样率 0~1) 与 字节/块 ----
    cover = _as_float(obj.get('cover'))
    if cover is not None:
        cover = max(0.0, min(1.0, cover))     # 板卡算的是估算值，越界只夹紧不报错
    b_per_blk = _as_float(obj.get('b_per_blk'))
    if b_per_blk is not None and b_per_blk < 0:
        b_per_blk = None                      # 负数无意义（blocks=0 板卡给 null）
    save_dir = obj.get('save_dir')
    warning = obj.get('warning')
    return {
        'ok': bool(obj.get('ok', True)),
        'ratio': ratio,
        'ratio_pct': '--' if ratio is None else '%.1f%%' % (ratio * 100.0),
        'kbps': 0.0 if kbps is None else kbps,
        'qsel': _as_int(obj.get('qsel')),
        'fps': 0.0 if fps is None else fps,
        'blocks': _as_int(obj.get('blocks')),
        'nbytes': _as_int(obj.get('nbytes')),
        'sd_free': _as_int(obj.get('sd_free')),
        'save_dir': '' if save_dir is None else str(save_dir),
        'auto': _as_bool01(obj.get('auto')),
        'target_kbps': _as_int(obj.get('target_kbps')),
        'autosave': _as_bool01(obj.get('autosave')),
        'detect': _as_bool01(obj.get('detect')),
        'cover': cover,
        'b_per_blk': b_per_blk,
        'warning': '' if warning is None else str(warning),
        'raw': obj,
    }


def safe_basename(name):
    """
    取安全文件名：去掉任何目录部分、禁止 '..'、清掉 Windows 非法字符。
    板卡侧也有同样的防护，PC 侧再做一次（下载落盘前的最后一道闸）。
    """
    text = str(name or '').replace('\\', '/')
    text = text.split('/')[-1].strip()
    text = ''.join(ch for ch in text if ord(ch) >= 32 and ch not in '"<>|*?:')
    if not text or text in ('.', '..'):
        raise ValueError('非法文件名: %r' % (name,))
    return text


def parse_defects_json(payload):
    """
    解析 /api/defects，返回 [{'name','size','mtime'}, ...]
    契约是 {"files":[{"name","size","mtime"}]}；同时容忍：
      - 直接给一个数组
      - 数组元素是纯文件名字符串
      - 字段名写成 "defects"
    非法 JSON / 结构不对 -> ValueError；条目缺 name -> 跳过该条目。
    """
    text = _decode_text(payload)
    try:
        obj = json.loads(text)
    except Exception as exc:
        raise ValueError('板卡 /api/defects 返回的不是合法 JSON (%s): %r'
                         % (exc, text[:120]))
    if isinstance(obj, dict):
        items = obj.get('files')
        if items is None:
            items = obj.get('defects')
        if items is None:
            items = []
    elif isinstance(obj, list):
        items = obj
    else:
        raise ValueError('/api/defects 结构不认识: %s' % type(obj).__name__)
    if not isinstance(items, list):
        raise ValueError('/api/defects 的 files 应该是数组, 实际是 %s'
                         % type(items).__name__)
    out = []
    for item in items:
        if isinstance(item, str):
            item = {'name': item}
        if not isinstance(item, dict):
            continue
        name = item.get('name')
        if not isinstance(name, str) or not name.strip():
            continue
        try:
            base = safe_basename(name)
        except ValueError:
            continue
        out.append({
            'name': base,
            'size': _as_int(item.get('size')) or 0,
            'mtime': _as_int(item.get('mtime')) or 0,
        })
    return out


def human_size(nbytes):
    """字节数 -> 人类可读（'84.3 KB' / '28.4 GB'），界面上的大小与 SD 余量都用它"""
    try:
        value = float(nbytes)
    except (TypeError, ValueError):
        return '--'
    if value < 0:
        return '--'
    if value < 1024:
        return '%d B' % int(value)
    if value < 1024 * 1024:
        return '%.1f KB' % (value / 1024.0)
    if value < 1024 * 1024 * 1024:
        return '%.1f MB' % (value / (1024.0 * 1024.0))
    if value < 1024.0 * 1024 * 1024 * 1024:
        return '%.1f GB' % (value / (1024.0 * 1024 * 1024))
    return '%.2f TB' % (value / (1024.0 * 1024 * 1024 * 1024))


def format_sd_text(sd_free, save_dir=''):
    """SD 余量显示：'SD 可用: 28.4 GB (/mnt/sdcard/defect)'；没有数据时给 '--'"""
    if sd_free is None:
        head = 'SD 可用: --'
    else:
        head = 'SD 可用: %s' % human_size(sd_free)
    save_dir = str(save_dir or '').strip()
    if save_dir:
        return '%s (%s)' % (head, save_dir)
    return head


def _cover_pct_text(pct):
    """百分比数字 -> '31%' / '24.9%'：整数就直给整数（31% 比 31.0% 好读），否则留 1 位小数。

    入参是**已经四舍五入到 1 位**的百分数（format_cover_line 里先 round 再判阈值），
    这样"显示值"与"是否报警"用的是同一个数，不会出现"显示 25% 却标抽样偏少"。
    """
    if abs(pct - round(pct)) < 0.05:       # 31.000000000000004 这类浮点尾巴也算整数
        return '%d%%' % int(round(pct))
    return '%.1f%%' % pct


def format_cover_line(status):
    """
    「覆盖率 / 字节每块」状态行（t4 新增，t6 改口径），返回 (文案, 是否该标橙)。

    ⚠ cover = 本窗口编码块数 / **一帧满块数 32400(240x135)**，即"这个窗口采到了整幅图多少面积"
      （t8 起分母从 4 帧预算 129600 改成一帧满块数；旧分母会把 p19 读成 7.7%）。
      它不是"整幅图覆盖率"：t2 条带轮转后单窗口约 31%，跨帧约 13 帧才走满整幅；
      旧位流(p18)重复编码会读 0.40，那是虚高、不是异常。
      所以这里只说明"这是抽样"，绝不暗示"越低越糟"。

    三档取值（板卡 t1 起才有这两个字段）：
        正常(≥25%) -> '覆盖率: 31% (本窗口抽样) ｜ 字节/块: 7.8 B'
        偏低(<25%) -> '覆盖率: 10% (本窗口抽样, 明显偏少) ｜ 字节/块: 7.8 B  (跨帧约 13 帧覆盖整幅)'
                      第二个返回值 True —— 界面据此把这行标橙（提醒而不是错误）
        缺失       -> '覆盖率: -- (旧版板卡无此字段) ｜ 字节/块: --'，不标橙
                      （旧板卡没有 cover/b_per_blk；blocks=0 时板卡给的 b_per_blk 是 null）
    """
    status = status or {}
    cover = _as_float(status.get('cover'))
    if cover is not None:
        cover = max(0.0, min(1.0, cover))
    b_per_blk = _as_float(status.get('b_per_blk'))
    if b_per_blk is not None and b_per_blk < 0:
        b_per_blk = None
    bpb_text = '字节/块: --' if b_per_blk is None else '字节/块: %.1f B' % b_per_blk
    if cover is None:
        return '覆盖率: -- (%s) ｜ %s' % (COVER_MISSING_HINT, bpb_text), False
    # 先按显示精度(0.1%)取整，再用同一个数判阈值：显示与报警口径永远一致
    pct = round(cover * 100.0, 1)
    low = pct < COVER_LOW_THRESHOLD * 100.0
    note = '本窗口抽样, 明显偏少' if low else '本窗口抽样'
    line = '覆盖率: %s (%s) ｜ %s' % (_cover_pct_text(pct), note, bpb_text)
    if low:
        # 说明"跨帧是够的"（满覆盖靠 RTL 条带轮转保证），而不是吓唬人"压缩率不代表整幅图"
        line += '  (%s)' % COVER_FULL_FRAMES_HINT
    return line, low


def format_quality_line(status):
    """
    压缩质量状态行（需求给的格式）：
        '质量 Q75 (手动)  目标 20 KB/s  实际 18.6 KB/s'
    板卡字段缺失时对应位置显示 '--'：
        · 旧板卡脚本没有 auto / target_kbps  -> '质量 Q75 (--)  目标 --  实际 18.6 KB/s'
        · 旧位流水印里 target5 恒 0（target_kbps=0） -> '目标 --'
        · qsel 都读不到 -> '质量 -- (手动)  目标 20 KB/s  实际 ...'
    """
    status = status or {}
    name = qsel_name(status.get('qsel'))
    auto = _as_bool01(status.get('auto'))
    if auto == 1:
        mode = '自适应'
    elif auto == 0:
        mode = '手动'
    else:
        mode = '--'
    target = _as_int(status.get('target_kbps'))
    target_text = '%d KB/s' % target if target else '--'
    kbps = _as_float(status.get('kbps'))
    actual_text = '--' if kbps is None else '%.1f KB/s' % kbps
    return '质量 %s (%s)  目标 %s  实际 %s' % (name, mode, target_text, actual_text)


# ============================================================================
# 4c. 纯逻辑层：板卡功能开关（/api/config：自动截图 autosave / YOLO 检测 detect）
# ============================================================================

SWITCH_AUTOSAVE = 'autosave'
SWITCH_DETECT = 'detect'
SWITCH_DIM = 'dim'                  # 尺寸测量(t18)：与 detect **互斥**
SWITCH_NAMES = {SWITCH_AUTOSAVE: '自动截图', SWITCH_DETECT: '目标检测',
                SWITCH_DIM: '尺寸测量'}
# 板卡没加载模型时 /api/config 会带这个 warning，界面据此把检测开关置灰（t11 契约）
_NO_MODEL_WARNING = 'no model loaded'
# 尺寸测量模式开着但板上没有 dim_measure.py（或缺 scipy）时板卡给的 warning
_NO_DIM_MODULE_WARNING = 'dim module not loaded'
# ---- 尺寸测量结果表（板卡 GET /api/dim，t18 冻结 schema dim-v1）----
DIM_POLL_INTERVAL = 2.0             # /api/dim 轮询周期（只读缓存，板卡侧 0 ms）
DIM_TIMEOUT = 3.0                   # /api/dim 请求超时
# 目标名 -> 中文列名（板卡按 dim_measure.TARGET_ORDER 给 name；认不出就原样显示）
# ⚠ 2026-10-26 S1 起板卡**只发 board_outline / hole_pitch 两项**（蓝牙模块尺寸停测）。
#   cap_c4 / res_r3 / bluetooth 保留成"旧板卡显示名"——本项目已经不发了，留着只为
#   连到旧位流/旧 dim_measure.py 时不至于显示成英文原名。**客户端不按数量做断言**。
DIM_TARGET_NAMES = {
    'board_outline': '板框',
    'board_len': '板框长',
    'board_wid': '板框宽',
    'hole_pitch': '孔距',
    'cap_c4': '电容⌀',
    'res_r3': '电阻长',
    'bluetooth': '蓝牙模块',
}
DIM_STATE_TEXT = {'idle': '未标定', 'calib': '标定中', 'ok': '已标定', 'bad': '不可信'}
DIM_TABLE_HEADERS = ('目标', '测量值', '真值', '误差', '判定')
DIM_OLD_BOARD_HINT = '板卡版本过旧（没有 /api/dim）'
# 「重新标定」结果行的颜色（t26）：失败红、成功绿、其它不染色
DIM_CALIB_OK_COLOR = '#1a7f37'
DIM_CALIB_FAIL_COLOR = '#c00000'


def next_switch_value(current):
    """开关点击后的目标值（取反）：1->0、0->1；当前状态未知（None）就不知道往哪切 -> None"""
    value = _as_bool01(current)
    if value is None:
        return None
    return 0 if value == 1 else 1


def format_switch_text(name, value):
    """开关按钮文字：'自动截图: 开' / '自动截图: 关' / '自动截图: --'（只认回读）"""
    label = SWITCH_NAMES.get(name, name)
    value = _as_bool01(value)
    if value is None:
        return '%s: --' % label
    return '%s: %s' % (label, '开' if value == 1 else '关')


def parse_config_json(payload, http_status=None):
    """
    解析 `GET /api/config` 的 JSON，返回：
        ok(bool) autosave(int|None) detect(int|None) dim(int|None)
        dim_auto_sec(int|None) changed(list) error(str)
        warning(str) http_status(int|None) raw(原始 dict)
    契约（t11）：成功 {"ok":true,"autosave":0|1,"detect":0|1,"changed":["detect"]}；
    契约（t18）：多一个 **dim**（0|1）与 dim_auto_sec（0=关），detect 与 dim **互斥**：
      dim=1 会自动 detect=0（反之亦然），被自动关掉的那项也出现在 changed 里 ——
      所以界面的"互斥"是**按回读确认**的，不是按点击猜的。
    非法值 HTTP 400 + {"ok":false,"error":"bad autosave=xx"}；
    detect=1 但没模型 → ok:true / detect 仍 0 + {"warning":"no model loaded"}。
    dim=1 但板上没有 dim_measure.py → ok:true + {"warning":"dim module not loaded"}。
    注意：**HTTP 400 也要能解析**（不是抛异常），否则界面拿不到 error 文案。
    旧板卡（没有 dim 字段）-> dim=None（界面把尺寸测量按钮置灰）。
    """
    text = _decode_text(payload)
    try:
        obj = json.loads(text)
    except Exception as exc:
        raise ValueError('/api/config 返回的不是合法 JSON (%s): %r'
                         % (exc, text[:160]))
    if not isinstance(obj, dict):
        raise ValueError('/api/config 应该是 JSON 对象, 实际是 %s' % type(obj).__name__)
    changed = obj.get('changed')
    if changed is None:
        changed = []
    if isinstance(changed, str):
        changed = [changed]
    if not isinstance(changed, list):
        changed = []
    error = obj.get('error')
    warning = obj.get('warning')
    ok = bool(obj.get('ok', True))
    if http_status is not None and int(http_status) >= 400:
        ok = False
        if not error:
            error = 'HTTP %d' % int(http_status)
    return {
        'ok': ok,
        'autosave': _as_bool01(obj.get('autosave')),
        'detect': _as_bool01(obj.get('detect')),
        'dim': _as_bool01(obj.get('dim')),
        'dim_auto_sec': _as_int(obj.get('dim_auto_sec')),
        'changed': [str(item) for item in changed],
        'error': '' if error is None else str(error),
        'warning': '' if warning is None else str(warning),
        'http_status': None if http_status is None else int(http_status),
        'raw': obj,
    }


def exclusive_params(name, target):
    """
    互斥请求参数：尺寸测量 与 缺陷检测 **不同时开**（用户明确不要求同时开）。

        点「尺寸测量 开」 -> {'detect': 0, 'dim': 1}   （两个参数一次带齐，省一次往返）
        点「尺寸测量 关」 -> {'dim': 0}
        点「目标检测 开」 -> {'detect': 1, 'dim': 0}
        点「目标检测 关」 -> {'detect': 0}
        点「自动截图 开/关」 -> {'autosave': 0|1}（与测量/检测无关）

    返回的 dict 直接**展开**给 url_config()：它的参数顺序固定 autosave→detect→dim，
    所以"尺寸测量开"实际发的是 `…/api/config?detect=0&dim=1`。
    """
    flag = 0 if int(target) == 0 else 1
    if name == SWITCH_DIM:
        return {'detect': 0, 'dim': 1} if flag else {'dim': 0}
    if name == SWITCH_DETECT:
        return {'dim': 0, 'detect': 1} if flag else {'detect': 0}
    return {str(name): flag}


def dim_blocked_reason(config):
    """
    尺寸测量开关"为什么点不了"，返回空字符串表示可点：
      · '板卡版本过旧（/api/config 没有 dim 字段）' -> 旧脚本：按钮置灰、文案 '--'
      · '板卡没有尺寸测量模块（dim module not loaded）' -> 板卡在但没装 dim_measure.py
    """
    config = config or {}
    if _as_bool01(config.get('dim')) is None:
        return '板卡版本过旧（/api/config 没有 dim 字段）'
    warning = str(config.get('warning') or '').lower()
    if _NO_DIM_MODULE_WARNING in warning:
        return '板卡没有尺寸测量模块（%s）' % config.get('warning')
    return ''


# ============================================================================
# 4e. 纯逻辑层：尺寸测量结果 /api/dim（t18 冻结 schema dim-v1）
# ============================================================================

def _dim_unsupported_state(http_status, error=''):
    """旧板卡（没有 /api/dim 路由）的降级结果：supported=False + 空表"""
    code = None if http_status is None else int(http_status)
    return {'ok': False, 'supported': False, 'schema': '', 'calib_ok': None,
            'calib_note': '', 'state': '', 'sigma_mm': None, 'accuracy_mm': None,
            'tol_kind': '', 'scale_mm_per_px': None, 'dim_available': None,
            'dim_module_error': '', 'busy': None, 'age_s': None,
            'recalib_queued': None, 'perf_ms': {}, 'targets': [],
            'error': error or ('HTTP %d' % code if code else 'unsupported'),
            'http_status': code, 'raw': {}}


def parse_dim_json(payload, http_status=None):
    """
    解析 `GET /api/dim`（板卡 t18，schema 由 tools/dim_measure.py 冻结成 dim-v1）：

        ok(bool) supported(bool) schema(str) calib_ok(bool|None) calib_note(str)
        state(str: idle/calib/ok/bad) sigma_mm/accuracy_mm(float|None) tol_kind(str)
        dim_available(bool|None) dim_module_error(str) busy/age_s/recalib_queued
        perf_ms(dict)
        targets(list[dict])：每项 {name, 中文名?, value_mm, nominal_mm, err_mm,
                                    err_pct, tol_mm, ok(bool|None), why}
        error(str) http_status(int|None) raw(dict)

    ⚠ 三条口径（照板卡契约，界面不许自己编）：
      · **calib_ok=false 时板卡一个 value_mm 都不给**（null）——这里也只照抄，不补 0
      · **tol_mm 是判定线（±0.3 mm / 5%），不是精度**；精度看 sigma_mm（可 null）
      · 旧板卡没有这个路由 -> HTTP 404（纯文本/HTML）：supported=False，不抛异常
    """
    text = _decode_text(payload)
    code = None if http_status is None else int(http_status)
    try:
        obj = json.loads(text)
    except Exception as exc:
        if code in (404, 501):
            return _dim_unsupported_state(code)
        raise ValueError('/api/dim 返回的不是合法 JSON (%s): %r' % (exc, text[:160]))
    if not isinstance(obj, dict):
        raise ValueError('/api/dim 应该是 JSON 对象, 实际是 %s' % type(obj).__name__)
    ok = bool(obj.get('ok', True))
    error = obj.get('error')
    if code is not None and code >= 400:
        ok = False
        if not error:
            error = 'HTTP %d' % code
    supported = code not in (404, 501)
    targets = []
    raw_targets = obj.get('targets')
    if isinstance(raw_targets, list):
        for item in raw_targets:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or '')
            value = _as_float(item.get('value_mm'))
            nominal = _as_float(item.get('nominal_mm'))
            err = _as_float(item.get('err_mm'))
            err_pct = None
            if err is not None and nominal:
                try:
                    err_pct = abs(err) / float(nominal) * 100.0
                except Exception:
                    err_pct = None
            targets.append({
                'name': name,
                'label': DIM_TARGET_NAMES.get(name, name),
                'value_mm': value,
                'nominal_mm': nominal,
                'err_mm': err,
                'err_pct': err_pct,
                'tol_mm': _as_float(item.get('tol_mm')),
                'ok': _as_bool01(item.get('ok')),
                'why': '' if item.get('why') is None else str(item.get('why')),
            })
    return {
        'ok': ok, 'supported': supported,
        'schema': str(obj.get('schema') or ''),
        'calib_ok': _as_bool01(obj.get('calib_ok')),
        'calib_note': '' if obj.get('calib_note') is None else str(obj.get('calib_note')),
        'state': str(obj.get('state') or ''),
        'sigma_mm': _as_float(obj.get('sigma_mm')),
        'accuracy_mm': _as_float(obj.get('accuracy_mm')),
        'tol_kind': str(obj.get('tol_kind') or ''),
        'scale_mm_per_px': _as_float(obj.get('scale_mm_per_px')),
        'dim_available': _as_bool01(obj.get('dim_available')),
        'dim_module_error': '' if obj.get('dim_module_error') is None
        else str(obj.get('dim_module_error')),
        'busy': _as_int(obj.get('busy')),
        'age_s': _as_float(obj.get('age_s')),
        'recalib_queued': _as_int(obj.get('recalib_queued')),
        'perf_ms': obj.get('perf_ms') if isinstance(obj.get('perf_ms'), dict) else {},
        'targets': targets,
        'error': '' if error is None else str(error),
        'http_status': code,
        'raw': obj,
    }


def format_dim_rows(dim):
    """
    /api/dim -> 表格行：[(目标, 测量值, 真值, 误差, 判定), ...]

      · 测量值/误差只照抄板卡：**calib_ok=false 时板卡给 null，这里就显示 '--'**
      · 真值（nominal_mm）与判定（OK/NG）板卡给了就显示；没给 '--'
      · 判定列：ok=1 -> 'OK'，ok=0 -> 'NG'，None -> '--'
      · 旧板卡（supported=False）-> [] （表格清空，由摘要行说明原因）
    """
    dim = dim or {}
    if dim.get('supported') is False:
        return []
    rows = []
    for item in (dim.get('targets') or []):
        value = item.get('value_mm')
        nominal = item.get('nominal_mm')
        err = item.get('err_mm')
        err_pct = item.get('err_pct')
        if err is None or err_pct is None:
            err_text = '--' if err is None else '%+.2f mm' % err
        else:
            err_text = '%+.2f mm (%.1f%%)' % (err, err_pct)
        judge = item.get('ok')
        rows.append((
            str(item.get('label') or item.get('name') or '?'),
            '--' if value is None else '%.2f mm' % value,
            '--' if nominal is None else '%.2f mm' % nominal,
            err_text,
            '--' if judge is None else ('OK' if judge == 1 else 'NG'),
        ))
    return rows


def format_dim_summary(dim):
    """
    尺寸测量摘要行（表格上方一行小字）：

        已标定  -> '尺寸测量: 已标定 ｜ 精度 ±1.26 mm ｜ 3 个目标: 2 OK 1 NG'
        未标定  -> '尺寸测量: 未标定 ｜ 板卡还没标定（calib_ok=false，不出测量值）'
        旧板卡  -> '尺寸测量: -- ｜ 板卡版本过旧（没有 /api/dim）'
        读不到  -> '尺寸测量: -- ｜ 还没读到 /api/dim'
    精度用 sigma_mm（可 null -> '--'），**不是** tol_mm（那是判定线）。
    """
    dim = dim or {}
    if dim.get('supported') is False:
        return '尺寸测量: -- ｜ %s' % DIM_OLD_BOARD_HINT
    if not dim:
        return '尺寸测量: -- ｜ 还没读到 /api/dim'
    state = str(dim.get('state') or '')
    state_text = DIM_STATE_TEXT.get(state, state or '--')
    sigma = dim.get('sigma_mm')
    acc_text = '精度 ±%.2f mm' % sigma if sigma is not None else '精度 --'
    calib_ok = _as_bool01(dim.get('calib_ok'))
    if calib_ok == 0:
        note = dim.get('calib_note') or '标定不可信'
        return '尺寸测量: %s ｜ %s（calib_ok=false，不出测量值）' % (state_text, note)
    targets = dim.get('targets') or []
    ok_count = sum(1 for it in targets if _as_bool01(it.get('ok')) == 1)
    ng_count = sum(1 for it in targets if _as_bool01(it.get('ok')) == 0)
    return '尺寸测量: %s ｜ %s ｜ %d 个目标: %d OK %d NG' % (
        state_text, acc_text, len(targets), ok_count, ng_count)


def dim_table_note(dim):
    """表格下方的红字原因（calib_ok=false / 读不到时）；正常返回空串"""
    dim = dim or {}
    if dim.get('supported') is False:
        return DIM_OLD_BOARD_HINT
    if _as_bool01(dim.get('calib_ok')) == 0:
        return dim.get('calib_note') or '标定不可信：板卡不给测量值'
    if dim.get('state') == 'bad':
        return dim.get('dim_module_error') or '板卡尺寸测量模块不可用'
    return ''


def dim_run_blocked_reason(dim):
    """点「测量一次」为什么点不了（旧板卡 / 模块不可用）；空串=可点"""
    dim = dim or {}
    if dim.get('supported') is False:
        return DIM_OLD_BOARD_HINT
    if _as_bool01(dim.get('dim_available')) == 0:
        return '板卡没有尺寸测量模块（%s）' % (dim.get('dim_module_error') or '未加载')
    return ''


def dim_calib_blocked_reason(dim, dim_enabled):
    """
    点「重新标定」为什么点不了，空串=可点（t26）：

      · '尺寸测量没开（标定只能在尺寸测量模式下做）' -> dim 回读不是 1（含还不知道）
      · '板卡版本过旧（没有 /api/dim）'              -> 旧板卡根本没这个路由

    ⚠ 只缺 `calib_ok` 字段（旧固件）**不算**点不了：那种情况按钮照样可点，
      结果行显示 '标定: --'（需求 3）。
    """
    dim = dim or {}
    if dim.get('supported') is False:
        return DIM_OLD_BOARD_HINT
    if _as_bool01(dim_enabled) != 1:
        return '尺寸测量没开（标定只能在尺寸测量模式下做）'
    return ''


def format_dim_calib_line(dim):
    """
    「重新标定」的结果行（一行短文本，**只写 /api/dim 回读值**），返回 (文案, 级别)：

        calib_ok=1        -> ('标定: OK  σ=0.42mm', 'ok')      精度取 sigma_mm（可 null -> σ=--）
        calib_ok=0        -> ('标定: 失败(孔位与已知布局不符)', 'fail')  级别 fail 时界面用红字
        state='calib'     -> ('标定: 标定中…', 'busy')
        缺 calib_ok（旧固件/旧板卡/还没读到） -> ('标定: --', 'none')，**不报错**
    """
    dim = dim or {}
    if dim.get('supported') is False:
        return '标定: --', 'none'
    calib_ok = _as_bool01(dim.get('calib_ok'))
    if str(dim.get('state') or '') == 'calib':
        return '标定: 标定中…', 'busy'
    if calib_ok is None:
        return '标定: --', 'none'
    if calib_ok == 1:
        sigma = _as_float(dim.get('sigma_mm'))
        if sigma is None:
            return '标定: OK  σ=--', 'ok'
        return '标定: OK  σ=%.2fmm' % sigma, 'ok'
    note = str(dim.get('calib_note') or '').strip()
    if len(note) > 40:                     # 一行短文本：太长就截断，完整原因看表格下方红字
        note = note[:39] + '…'
    return ('标定: 失败(%s)' % note) if note else '标定: 失败', 'fail'


def dim_calib_color(level):
    """结果行的颜色：失败红、成功绿、其它不染色（'--'/标定中）"""
    if level == 'fail':
        return DIM_CALIB_FAIL_COLOR
    if level == 'ok':
        return DIM_CALIB_OK_COLOR
    return ''


def switch_blocked_reason(status, name):
    """
    判断某个开关"为什么点不了"，返回空字符串表示可点：
      · '板卡版本过旧（/api/status 没有该字段）'  -> 老脚本，显示 '--' 且置灰
      · '板卡没加载模型（detect 恒 0）'          -> t11 的 no model warning
    """
    status = status or {}
    if _as_bool01(status.get(name)) is None:
        return '板卡版本过旧（/api/status 没有 %s 字段）' % name
    if name == SWITCH_DETECT:
        warning = str(status.get('warning') or '').lower()
        if _NO_MODEL_WARNING in warning:
            return '板卡没加载模型（%s）' % status.get('warning')
    return ''


# ============================================================================
# 4d. 纯逻辑层：远程推送 /api/push + 手机监看地址（+ 可选二维码）
# ============================================================================

PUSH_OLD_BOARD_HINT = '板卡版本过旧（没有 /api/push）'
_PUSH_UNSUPPORTED_ERRORS = ('not found', 'no pusher', '404', 'unsupported')


def _push_unsupported_state(http_status, error=''):
    """旧板卡（没有 /api/push 路由）的降级结果：ok=False + supported=False + 全是 '--'"""
    code = None if http_status is None else int(http_status)
    return {
        'ok': False, 'enabled': None, 'url': '', 'queued': None,
        'count_ok': None, 'count_fail': None, 'last_error': '',
        'changed': [], 'error': error or ('HTTP %d' % code if code else 'unsupported'),
        'warning': '', 'test_queued': None,
        'supported': False, 'http_status': code, 'raw': {},
    }


def parse_push_json(payload, http_status=None):
    """
    解析 `/api/push` 的 JSON，返回：
        ok(bool, 请求本身是否成功)  enabled(int 0/1|None)  url(str)
        queued(int|None)  count_ok(int|None)  count_fail(int|None)  last_error(str)
        changed(list)  error(str)  warning(str)  test_queued(int|None)
        supported(bool|None)  http_status(int|None)  raw(原始 dict)

    ⚠ 有个坑：GET 快照里的 **`ok` 是"成功推送次数"（整数）**，而 POST 的响应里
      `ok` 是"这次请求成功了吗"（布尔）—— 同一个键两种语义，所以按类型分开：
        布尔 -> 请求状态；整数 -> 计数（board 的 GET 契约）
    GET 契约恰好 6 个字段（enabled/url/queued/ok/fail/last_error），没有 ok 布尔字段；
    POST 响应是 {"ok":true,"enabled":..,"url":..,"changed":[...]}。
    旧板卡（没有这个路由）-> HTTP 404：这里**不抛异常**，给 supported=False + ok=False +
    error 文案，好让界面把整组置灰并提示"板卡版本过旧（没有 /api/push）"。
    """
    text = _decode_text(payload)
    code = None if http_status is None else int(http_status)
    try:
        obj = json.loads(text)
    except Exception as exc:
        if code in (404, 501):
            # 旧板卡：压根没有 /api/push 这个路由，回的是纯文本/HTML 404。
            # 这是"版本过旧"而不是"返回了坏 JSON" —— 必须降级（supported=False），
            # 否则界面会把它当网络错误、连"板卡版本过旧"的提示都说不出来。
            return _push_unsupported_state(code,
                                           'HTTP %d（%s）' % (code, text[:80].strip())
                                           if text.strip() else 'HTTP %d' % code)
        raise ValueError('/api/push 返回的不是合法 JSON (%s): %r'
                         % (exc, text[:160]))
    if not isinstance(obj, dict):
        raise ValueError('/api/push 应该是 JSON 对象, 实际是 %s' % type(obj).__name__)
    raw_ok = obj.get('ok')
    if isinstance(raw_ok, bool):
        ok = raw_ok
        count_ok = _as_int(obj.get('count_ok'))
    else:
        ok = True                       # GET 快照没有"请求状态"字段，能解析出来就算成功
        count_ok = _as_int(raw_ok)
    error = obj.get('error')
    warning = obj.get('warning')
    if http_status is not None and int(http_status) >= 400:
        ok = False
        if not error:
            error = 'HTTP %d' % int(http_status)
    changed = obj.get('changed')
    if changed is None:
        changed = []
    if isinstance(changed, str):
        changed = [changed]
    if not isinstance(changed, list):
        changed = []
    # 支持性判断：有契约字段 = 支持；404/501 或文案里带 not found = 旧板卡
    has_state = any(key in obj for key in PUSH_STATE_KEYS)
    supported = True
    if code in (404, 501):
        supported = False
    elif not ok and not has_state and \
            any(token in str(error).lower() for token in _PUSH_UNSUPPORTED_ERRORS):
        supported = False
    elif not ok and not has_state and code is not None and code >= 500:
        supported = None                # 板卡在但服务出问题：不确定是不是版本问题
    url = obj.get('url')
    last_error = obj.get('last_error')
    return {
        'ok': ok,
        'enabled': _as_bool01(obj.get('enabled')),
        'url': '' if url is None else str(url),
        'queued': _as_int(obj.get('queued')),
        'count_ok': count_ok,
        'count_fail': _as_int(obj.get('fail')),
        'last_error': '' if last_error is None else str(last_error),
        'changed': [str(item) for item in changed],
        'error': '' if error is None else str(error),
        'warning': '' if warning is None else str(warning),
        'test_queued': _as_int(obj.get('test_queued')),
        'supported': supported,
        'http_status': code,
        'raw': obj,
    }


def push_blocked_reason(state):
    """
    这一组控件"为什么点不了"，返回空字符串表示可点：
      · '板卡版本过旧（没有 /api/push）' -> 旧板卡：整组置灰、文案 --、点击只提示
      · '板卡 /api/push 出错: xxx'       -> 板卡在但接口报错（不是版本问题，仍提示不可用）
    """
    state = state or {}
    if state.get('supported') is False:
        return PUSH_OLD_BOARD_HINT
    if state.get('ok') is False:
        return '板卡 /api/push 出错: %s' % (state.get('error') or '未知错误')
    return ''


def push_url_issue(url):
    """「目标 URL」的形态检查：返回 '' 表示能发，否则返回给人看的一句话。

    [D29] 真机踩过的两种错：
      1) 把**手机监看页** `/m` 当成推送目标贴进来 —— 板卡会把缺陷图 POST 到那个 HTML 页
         （接收端是我们的 recv_upload.py，路径一般是 /upload），结果只会 404；
      2) 忘记写 `http://`，或用中文全角冒号。
    这里只做**阻断性**检查；像"路径不是 /upload"这种可疑情况只在提示行提醒、不拦。
    """
    u = str(url or '').strip()
    if not u:
        return ('目标 URL 不能为空（PC 接收端，如 http://<PC_IP>:9000/upload）')
    if not re.match(r'^https?://[^\s/]+', u, re.I):
        return '目标 URL 必须以 http:// 或 https:// 开头'
    tail = u.rstrip('/')
    if tail.endswith('/m'):
        return ('这是手机监看页地址（/m），不是推送目标；'
                '请填 PC 接收端地址，如 http://<PC_IP>:9000/upload')
    return ''


def push_pending_expired(at, now, timeout=PUSH_PENDING_TIMEOUT):
    """点了开/关之后等回读的「等待」是否已经超时（超时就必须解除置灰）。

    at = 点下去的时间戳（0/None 表示没有在等）；返回 True 表示该放弃等待。
    """
    try:
        at = float(at or 0.0)
        now = float(now)
    except (TypeError, ValueError):
        return False
    return bool(at) and (now - at) > float(timeout)


def format_push_line(state):
    """
    推送状态行（只写**回读值**，不写"刚点了什么"）：
        开   -> '推送: 开 ｜ 队列 0 ｜ 成功 12 ｜ 失败 0'
        关   -> '推送: 关 ｜ 队列 0 ｜ 成功 0 ｜ 失败 0'
        旧板卡/读不到 -> '推送: -- ｜ 队列 -- ｜ 成功 -- ｜ 失败 --'
    """
    state = state or {}
    blocked = push_blocked_reason(state)
    enabled = _as_bool01(state.get('enabled'))
    if blocked or enabled is None:
        return '推送: -- ｜ 队列 -- ｜ 成功 -- ｜ 失败 --'

    def _num(key):
        value = _as_int(state.get(key))
        return '--' if value is None else '%d' % value

    return '推送: %s ｜ 队列 %s ｜ 成功 %s ｜ 失败 %s' % (
        '开' if enabled == 1 else '关', _num('queued'), _num('count_ok'), _num('count_fail'))


def format_push_error_line(state):
    """last_error 非空时给一行小字（界面用红色）；空字符串表示"没有错误" """
    last_error = str((state or {}).get('last_error') or '').strip()
    if not last_error:
        return ''
    return '最近错误: %s' % last_error


def format_push_switch_text(state):
    """推送开关按钮文字：'推送: 开' / '推送: 关' / '推送: --'（只认 /api/push 回读）"""
    state = state or {}
    enabled = _as_bool01(state.get('enabled'))
    if push_blocked_reason(state) or enabled is None:
        return '推送: --'
    return '推送: %s' % ('开' if enabled == 1 else '关')


def format_mobile_line(base):
    """手机监看地址行（给一个板卡**基地址**）：'手机看: http://192.168.10.50:8000/m'"""
    return '手机看: %s' % url_mobile(base)


def format_mobile_url_line(url):
    """手机监看地址行（给一个**已经拼好的 /m 地址**）：'手机看: http://<ip>:<端口>/m'"""
    return '手机看: %s' % str(url or '')


def push_toggle_state(state):
    """
    远程推送「开 / 关」两个按钮该长什么样（**唯一依据是 /api/push 回读**）：

        {'enabled': 0|1|None,      # 回读值，None = 还不知道
         'on_checked': bool,        # 「开」按钮是否点亮
         'off_checked': bool,       # 「关」按钮是否点亮
         'usable': bool}            # 两个按钮是否可点（旧板卡/回读失败/等回读期间都不可点）

    注意：点按钮**不改** here 的任何值 —— 只有下一次 /api/push 回读才会让两个按钮换态，
    这样界面永远是"板卡现在到底开没开"，而不是"我刚点了什么"。
    """
    state = state or {}
    enabled = _as_bool01(state.get('enabled'))
    usable = not push_blocked_reason(state) and enabled is not None
    return {'enabled': enabled,
            'on_checked': enabled == 1,
            'off_checked': enabled == 0,
            'usable': usable}


def parse_net_json(payload, http_status=None):
    """
    解析 `GET /api/net`（板卡 t14，给二维码取板卡 IP 用），返回：
        ok(bool) ifs(list[{name,addr,up(int|None),carrier(int|None),wireless(int 0/1)}])
        mobile(list[str]) error(str) supported(bool|None) http_status(int|None) raw(dict)

    契约：{"ok":true,"ifs":[{name,addr,up,carrier,wireless},...],
            "urls":{"mobile":["http://<ip>:<端口>/m",...]}}（ifs / mobile 都无线优先）
    容错：ifs 缺字段给默认、条目不是 dict 就跳过、urls 缺失就给 []；
    旧板卡没有这个路由（404，回的是纯文本/HTML）-> supported=False + ok=False，
    **不抛异常**（界面退化成"用界面上的板卡 IP"，不得因此报错）。
    """
    text = _decode_text(payload)
    code = None if http_status is None else int(http_status)
    try:
        obj = json.loads(text)
    except Exception as exc:
        if code in (404, 501):
            return {'ok': False, 'ifs': [], 'mobile': [], 'supported': False,
                    'error': 'HTTP %d' % code, 'http_status': code, 'raw': {}}
        raise ValueError('/api/net 返回的不是合法 JSON (%s): %r' % (exc, text[:160]))
    if not isinstance(obj, dict):
        raise ValueError('/api/net 应该是 JSON 对象, 实际是 %s' % type(obj).__name__)
    ifs = []
    raw_ifs = obj.get('ifs')
    if isinstance(raw_ifs, list):
        for item in raw_ifs:
            if not isinstance(item, dict):
                continue
            addr = str(item.get('addr') or '').strip()
            if not addr:
                continue
            ifs.append({'name': str(item.get('name') or ''),
                        'addr': addr,
                        'up': _as_int(item.get('up')),
                        'carrier': _as_int(item.get('carrier')),
                        'wireless': _as_bool01(item.get('wireless')) or 0})
    mobile = []
    urls = obj.get('urls')
    if isinstance(urls, dict) and isinstance(urls.get('mobile'), list):
        mobile = [str(u) for u in urls['mobile'] if isinstance(u, str) and u.strip()]
    ok = bool(obj.get('ok', True))
    error = obj.get('error')
    if code is not None and code >= 400:
        ok = False
        if not error:
            error = 'HTTP %d' % code
    supported = True
    if code in (404, 501):
        supported = False
    return {'ok': ok, 'ifs': ifs, 'mobile': mobile, 'supported': supported,
            'error': '' if error is None else str(error),
            'http_status': code, 'raw': obj}


def choose_mobile_addr(net):
    """
    从 `/api/net` 的结果里挑"二维码该指向哪个地址"，返回 (addr, wireless, warn)：

      · 有无线 IP（wireless=1）-> 用第一条无线的（手机要连的就是它），warn=''
      · 没有无线 IP          -> 退第一条有线的 + `QR_NO_WIRELESS_WARN`（橙色警告文案）
      · 一个都没有/接口不可用 -> ('', False, '')，界面再退化成"用界面上的板卡 IP"
    """
    net = net or {}
    ifs = [it for it in (net.get('ifs') or []) if isinstance(it, dict) and it.get('addr')]
    for item in ifs:
        if _as_bool01(item.get('wireless')) == 1:
            return str(item['addr']), True, ''
    if ifs:
        return str(ifs[0]['addr']), False, QR_NO_WIRELESS_WARN
    return '', False, ''


def local_ip_for_board(board_ip, port=DEFAULT_BOARD_PORT, timeout=0.3):
    """
    路由探测：**板卡能到达的那个本机 IP**（推送目标 URL 的默认值就该用它）。

    做法：UDP socket 只 connect 不发包，内核会把源地址选成"打这个目的地址会走的那块网卡"，
    getsockname()[0] 就是那个本机 IP —— 这就是板卡能回连到的地址（不是"本机第一个网卡"）。
    任何失败（板卡地址非法/没有路由）都返回 ''，调用方退回占位地址。
    """
    try:
        import socket
        host = str(board_ip or '').strip()
        if not host:
            return ''
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(float(timeout))
            sock.connect((host, int(port)))
            addr = str(sock.getsockname()[0] or '').strip()
        finally:
            sock.close()
        return '' if addr.startswith('127.') else addr
    except Exception:
        return ''


def push_url_for_board(board_ip, board_port=DEFAULT_BOARD_PORT):
    """
    推送目标 URL 的**建议默认值**：`http://<板卡能到达的本机 IP>:9000/upload`。

    取不到路由（探测失败）就用占位 `http://192.168.10.100:9000/upload`。
    注意与老的 default_push_url() 的区别：老的是"本机第一个网卡 IP"（常常是 WiFi，
    板卡从有线过来根本回连不到），这一个是**路由探测**出来的地址。
    """
    addr = local_ip_for_board(board_ip, board_port) or '192.168.10.100'
    return 'http://%s:%d%s' % (addr, PUSH_DEFAULT_PORT, PUSH_DEFAULT_PATH)


def qr_png_bytes(text, scale=4, border=2):
    """
    把一段文本（手机监看地址）编成 **PNG** 二维码，返回 bytes；任何一步失败都返回 None。

    依赖是**可选**的（都不写进 requirements）：
      1) 首选 `segno`（纯 Python，画 PNG 最省事）：segno.make(url, micro=False).save(...)
      2) 退一步 `qrcode` + pillow（老环境可能只装了它）
      3) 两个都没有 / 画图失败 / 文本为空 -> 返回 None：界面显示占位文字
         "二维码不可用（pip install segno）"，明文 URL 与「复制」照常可用，绝不崩。
    """
    text = str(text or '').strip()
    if not text:
        return None
    buffer = io.BytesIO()
    try:
        import segno
        segno.make(text, micro=False).save(buffer, kind='png',
                                          scale=int(scale), border=int(border))
        data = buffer.getvalue()
        if data.startswith(b'\x89PNG'):
            return data
    except Exception:
        buffer = io.BytesIO()
    try:                                   # 退路：qrcode + pillow
        import qrcode
        image = qrcode.make(text)
        image.save(buffer, 'PNG')
        data = buffer.getvalue()
        return data if data.startswith(b'\x89PNG') else None
    except Exception:
        return None


# ============================================================================
# 4b. 纯逻辑层：/api/events 增量事件 + /api/snap 截图响应
# ============================================================================

EVENT_TYPE_DETECT = 'detect'
EVENT_TYPE_MANUAL = 'manual'


def normalize_event(raw):
    """
    把板卡给的一条事件 dict 规整成固定字段（缺字段/类型不对都不抛异常，返回 None 表示丢弃）：
        seq(int>0) t(float) type('detect'|'manual'|其它原样) file(str) size(int)
        cls(str|None) score(float|None) ratio(0~1|None) fps(float) mode(int|None)
    seq 缺失/非正整数的条目直接丢掉 —— 没有 seq 就无法做增量与去重。
    """
    if not isinstance(raw, dict):
        return None
    seq = _as_int(raw.get('seq'))
    if seq is None or seq <= 0:
        return None
    kind = raw.get('type')
    if kind is None:
        kind = 'unknown'
    kind = str(kind)
    if kind not in (EVENT_TYPE_DETECT, EVENT_TYPE_MANUAL):
        kind = 'unknown'
    name = raw.get('file')
    if isinstance(name, str) and name.strip():
        try:
            name = safe_basename(name)
        except ValueError:
            return None
    else:
        name = ''
    ratio = _as_float(raw.get('ratio'))
    if ratio is not None:
        if ratio > 1.0:                    # 容错：万一板卡给百分数
            ratio = ratio / 100.0
        ratio = max(0.0, min(1.0, ratio))
    cls = raw.get('cls')
    cls = None if cls is None else str(cls)
    stamp = _as_float(raw.get('t'))
    fps = _as_float(raw.get('fps'))
    return {
        'seq': seq,
        't': 0.0 if stamp is None else stamp,
        'type': kind,
        'file': name,
        'size': _as_int(raw.get('size')) or 0,
        'cls': cls,
        'score': _as_float(raw.get('score')),
        'ratio': ratio,
        'fps': 0.0 if fps is None else fps,
        'mode': _as_int(raw.get('mode')),
    }


def parse_events_json(payload):
    """
    解析 /api/events?since=N 的 JSON，返回 {'next': int|None, 'events': [规整后的事件...]}
    契约是 {"next": M, "events": [ ... ]}；容忍 events 为 null/缺失、next 缺失。
    """
    text = _decode_text(payload)
    try:
        obj = json.loads(text)
    except Exception as exc:
        raise ValueError('板卡 /api/events 返回的不是合法 JSON (%s): %r'
                         % (exc, text[:120]))
    if not isinstance(obj, dict):
        raise ValueError('/api/events 应该是 JSON 对象, 实际是 %s' % type(obj).__name__)
    raw_events = obj.get('events')
    if raw_events is None:
        raw_events = []
    if not isinstance(raw_events, list):
        raise ValueError('/api/events 的 events 应该是数组, 实际是 %s'
                         % type(raw_events).__name__)
    events = []
    for item in raw_events:
        event = normalize_event(item)
        if event is not None:
            events.append(event)
    next_seq = _as_int(obj.get('next'))
    if next_seq is not None and next_seq < 0:
        next_seq = None
    return {'next': next_seq, 'events': events}


class EventCursor(object):
    """
    事件游标的纯逻辑部分（不碰网络，便于自检）：
      - 记住 last_seq，下一次请求用 since=last_seq 做增量；
      - 用 seen 集合去重（同一事件即使被重复返回也只显示一次）；
      - 板卡重启导致 seq 回退（next < last_seq）时：判定为"新一轮"，清空去重表后
        照常接收，绝不因为计数器归零就一条都不显示；
      - 批内乱序（seq 倒序给出）按 seq 升序显示，显示顺序始终是时间顺序；
      - 无 new events 时返回 []，next 不变。
    """

    def __init__(self, since=0, max_seen=2000):
        self.last_seq = max(0, int(since or 0))
        self.max_seen = int(max_seen)
        self._seen = collections.OrderedDict()
        self.regressions = 0          # 检测到 seq 回退的次数（自检/排障用）
        self.delivered = 0            # 累计交付给界面的条数

    def since(self):
        return self.last_seq

    def _mark(self, seq):
        if seq in self._seen:
            return
        self._seen[seq] = True
        while len(self._seen) > self.max_seen:
            self._seen.popitem(last=False)

    def ingest(self, parsed):
        """吃进一次 /api/events 的解析结果，返回"需要显示的新事件"（按 seq 升序）"""
        if not isinstance(parsed, dict):
            return []
        events = [e for e in (parsed.get('events') or []) if isinstance(e, dict)]
        events.sort(key=lambda e: e.get('seq') or 0)
        next_seq = parsed.get('next')
        if next_seq is not None:
            next_seq = int(next_seq)
        highest = max([e.get('seq') or 0 for e in events] or [0])
        unseen = [e for e in events if e.get('seq') not in self._seen]

        # 什么时候才认定"seq 回退（板卡重启）"：
        #   ① 空批但 next 比本地游标小 -> 计数器归零了，游标要跟着回去，否则永远收不到新事件；
        #   ② 批里有"编号比本地游标还小、但没见过"的事件 -> 新一轮 seq 空间。
        # 只是同一批被重复返回（next 略小于本地游标，但事件都见过）**不算**回退，否则会重复刷屏。
        regression = False
        if next_seq is not None and next_seq < self.last_seq:
            if not events:
                regression = True
            elif any(e['seq'] < self.last_seq for e in unseen):
                regression = True
        if regression:
            self.regressions += 1
            self._seen.clear()
            new_events = list(events)
        else:
            new_events = unseen
        for event in events:
            self._mark(event['seq'])

        if regression:
            self.last_seq = max(0, next_seq if next_seq is not None else highest)
        elif next_seq is not None and next_seq >= self.last_seq:
            self.last_seq = max(next_seq, highest)
        else:
            # 滞后/重复的响应：游标只向前，绝不因为对方的旧 next 而倒退
            self.last_seq = max(self.last_seq, highest)
        self.delivered += len(new_events)
        return new_events


def _pretty_time(stamp):
    """unix 秒 -> 'HH:MM:SS'；stamp 为 0 时用当前时间（板卡没给时间也能看）"""
    try:
        value = float(stamp)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        value = time.time()
    return time.strftime('%H:%M:%S', time.localtime(value))


def format_event_line(event):
    """
    一条事件 -> 日志面板的一行（严格照需求给的样例格式）：
        detect: '14:22:33 [检出] defect_missing 0.91  → det_142233.jpg (86 KB)'
        manual: '14:23:07 [手动] 截图 → manual_142307.jpg (84 KB)'
    """
    stamp = _pretty_time(event.get('t'))
    kind = event.get('type')
    name = event.get('file') or '?'
    size = human_size(event.get('size') or 0)
    if kind == EVENT_TYPE_DETECT:
        cls = event.get('cls') or '?'
        score = event.get('score')
        score_text = '?' if score is None else '%.2f' % float(score)
        return '%s [检出] %s %s  → %s (%s)' % (stamp, cls, score_text, name, size)
    if kind == EVENT_TYPE_MANUAL:
        return '%s [手动] 截图 → %s (%s)' % (stamp, name, size)
    return '%s [事件] %s (%s)' % (stamp, name, size)


def parse_snap_json(payload):
    """
    解析 /api/snap 的 JSON，返回 {'ok','file','size','dir','error','http_status'}。
    - 成功：{"ok":true,"file":"manual_142307.jpg","size":86321,"dir":"/mnt/sdcard/defect"}
    - 无帧：{"ok":false,"error":"no frame"}（板卡同时返回 HTTP 503，由调用方填 http_status）
    ok=true 却没给文件名 -> ValueError（界面按失败处理，不假装成功）。
    """
    text = _decode_text(payload)
    try:
        obj = json.loads(text)
    except Exception as exc:
        raise ValueError('/api/snap 返回的不是合法 JSON (%s): %r'
                         % (exc, text[:120]))
    if not isinstance(obj, dict):
        raise ValueError('/api/snap 应该是 JSON 对象, 实际是 %s' % type(obj).__name__)
    ok = bool(obj.get('ok'))
    name = obj.get('file')
    error = obj.get('error')
    result = {
        'ok': ok,
        'file': '',
        'size': _as_int(obj.get('size')) or 0,
        'dir': '' if obj.get('dir') is None else str(obj.get('dir')),
        'error': '' if error is None else str(error),
        'http_status': _as_int(obj.get('http_status')),
    }
    if ok:
        if not isinstance(name, str) or not name.strip():
            raise ValueError('/api/snap 返回 ok=true 但没有 file 字段: %r' % text[:120])
        result['file'] = safe_basename(name)
    else:
        if not result['error']:
            result['error'] = '板卡未返回原因'
        if isinstance(name, str) and name.strip():
            try:
                result['file'] = safe_basename(name)
            except ValueError:
                pass
    return result


# ============================================================================
# 5. 纯逻辑层：下载路径与缩略图
# ============================================================================

def resolve_download_dir(download_dir=None):
    """把 './downloads' 一类的相对路径解析成绝对路径（GUI 里显示给用户看）"""
    path = download_dir if download_dir else DEFAULT_DOWNLOAD_DIR
    path = os.path.expanduser(str(path))
    return os.path.abspath(path)


def local_path_for(download_dir, name):
    """缺陷图落盘路径：<download_dir>/<basename>（目录不存在也先算出来）"""
    return os.path.join(resolve_download_dir(download_dir), safe_basename(name))


def unique_path(path):
    """同名文件已存在时，自动加 _1 / _2 …，绝不静默覆盖用户已下载的照片"""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    index = 1
    while True:
        candidate = '%s_%d%s' % (root, index, ext)
        if not os.path.exists(candidate):
            return candidate
        index += 1


def save_bytes(path, data, make_unique=True):
    """写文件（自动建目录、可选改名避让），返回真正写入的路径"""
    target = unique_path(path) if make_unique else path
    folder = os.path.dirname(target)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with open(target, 'wb') as fp:
        fp.write(data)
    return target


def make_thumbnail(jpeg_bytes, size=(THUMB_W, THUMB_H)):
    """
    JPEG 字节 -> 160x90 的 PNG 字节（等比缩放 + 黑边填充，绝不变形）。
    GUI 侧只需要 QPixmap.loadFromData(png) 即可；缩放在线程里做，不占 UI 线程。
    依赖 pillow：pip install pillow
    """
    try:
        from PIL import Image
    except Exception as exc:                      # pragma: no cover - 环境相关
        raise RuntimeError('生成缩略图需要 pillow（pip install pillow）: %s' % exc)
    src = Image.open(io.BytesIO(bytes(jpeg_bytes)))
    src = src.convert('RGB')
    target_w, target_h = int(size[0]), int(size[1])
    src.thumbnail((target_w, target_h))           # 等比缩放到框内
    canvas = Image.new('RGB', (target_w, target_h), (0, 0, 0))
    offset = ((target_w - src.size[0]) // 2, (target_h - src.size[1]) // 2)
    canvas.paste(src, offset)
    out = io.BytesIO()
    canvas.save(out, 'PNG')
    return out.getvalue()


# ============================================================================
# 6. 纯逻辑层：HTTP 取数（只用标准库 urllib，不用 requests）
# ============================================================================

def fetch_bytes(url, timeout=HTTP_TIMEOUT):
    """GET 一个 URL 返回 bytes（超时必设，绝不让请求把线程挂死）"""
    request = Request(url, headers={'User-Agent': _USER_AGENT})
    response = urlopen(request, timeout=timeout)
    try:
        return response.read()
    finally:
        try:
            response.close()
        except Exception:
            pass


def post_bytes(url, timeout=HTTP_TIMEOUT, data=b''):
    """
    POST 一个 URL 返回 bytes（/api/push 用）。

    板卡的 /api/push 参数全在 query 里（form body 也认），所以 body 默认空；
    空 body 的 POST 一定是合法的 HTTP 请求，且板卡侧 do_POST 会因为
    Content-Length=0 而不去读 rfile，不会有半截请求卡住的隐患。
    """
    request = Request(url, data=data if data is not None else b'',
                      headers={'User-Agent': _USER_AGENT}, method='POST')
    response = urlopen(request, timeout=timeout)
    try:
        return response.read()
    finally:
        try:
            response.close()
        except Exception:
            pass


def describe_net_error(exc):
    """把 urllib 的异常翻成给用户看的一句话（状态栏用）"""
    if isinstance(exc, HTTPError):
        return 'HTTP %s: %s' % (exc.code, exc.reason)
    if isinstance(exc, URLError):
        return '连不上板卡 (%s)' % (getattr(exc, 'reason', exc),)
    return '%s: %s' % (type(exc).__name__, exc)


# ============================================================================
# 7. PyQt5 导入（本机没有 PyQt5，所以必须有替身让 --selftest 能 import 本文件）
# ============================================================================

try:                                              # pragma: no cover - 环境相关
    from PyQt5.QtCore import QSize, Qt, QThread, QTimer, pyqtSignal
    from PyQt5.QtGui import QIcon, QImage, QPixmap
    from PyQt5.QtWidgets import (
        QAction, QApplication, QButtonGroup, QComboBox, QDialog, QGroupBox,
        QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
        QMainWindow, QPlainTextEdit, QPushButton, QRadioButton, QScrollArea,
        QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    )
    QT_OK = True
    QT_ERROR = ''
except Exception as _qt_exc:                      # 本机开发环境：没有 PyQt5
    QT_OK = False
    QT_ERROR = repr(_qt_exc)

    # 说明：下面这些替身**只**为了让本文件的类定义在缺少 PyQt5 时也能 import，
    # 从而让 `--selftest` 跑起来。真正的 GUI 只能在装了 PyQt5 的 PC 上运行。
    def pyqtSignal(*args, **kwargs):
        return None

    class _QtStub(object):
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name):
            def _noop(*a, **k):
                return None
            return _noop

    QThread = QWidget = QMainWindow = QLabel = QListWidget = QListWidgetItem = _QtStub
    QApplication = QComboBox = QPushButton = QRadioButton = QButtonGroup = _QtStub
    QGroupBox = QHBoxLayout = QVBoxLayout = QLineEdit = QAction = QDialog = _QtStub
    QTableWidget = QTableWidgetItem = QHeaderView = _QtStub
    QPlainTextEdit = _QtStub
    QScrollArea = _QtStub
    QSizePolicy = _QtStub
    QImage = QPixmap = QIcon = QSize = _QtStub
    Qt = _QtStub()


def _slot(func):
    """GUI 槽函数保护：任何异常都打印 traceback + 写状态栏，绝不让界面带着疑问卡死。

    同时做**参数裁剪**：Qt 的 `clicked` / `triggered` 信号会多传一个 `checked(False)`，
    而我们的槽函数大多只接受 `self` —— 不裁掉就会报
        TypeError: _on_xxx() takes 1 positional argument but 2 were given
    （这个坑只能在真机 PyQt5 上暴露，本机自检抓不到；已按槽函数实际参数个数裁剪）
    """
    try:
        _nargs = func.__code__.co_argcount          # 含 self
    except AttributeError:
        _nargs = 99
    _maxargs = max(0, _nargs - 1)

    def wrapper(self, *args, **kwargs):
        if len(args) > _maxargs:                     # 丢掉 Qt 多给的 checked
            args = args[:_maxargs]
        try:
            return func(self, *args, **kwargs)
        except Exception as exc:
            message = '内部错误 %s: %s' % (getattr(func, '__name__', 'slot'), exc)
            print('[ERR] %s' % message)
            traceback.print_exc()
            try:
                self.statusBar().showMessage(message, 8000)
                self._set_net_state(message, False)
            except Exception:
                pass
            return None
    wrapper.__name__ = getattr(func, '__name__', 'slot')
    wrapper.__doc__ = getattr(func, '__doc__', None)
    return wrapper


# ============================================================================
# 8. 工作线程（串口 / MJPEG / 状态轮询 / 缺陷列表与缩略图 / 下载）
#     全部放 QThread：UI 线程只负责画界面，任何一个请求卡住都不会冻结窗口。
# ============================================================================

class SerialWorker(QThread):
    """串口收发线程：115200 8N1；发送走队列，接收按 '\\n' 切行后 emit"""

    lineReceived = pyqtSignal(str)      # FPGA 回传的一行（已去掉 CRLF）
    opened = pyqtSignal(bool, str)      # (是否成功, 说明文字)
    failed = pyqtSignal(str)            # 出错信息（给状态栏）

    def __init__(self, port, baud=SERIAL_BAUD, parent=None):
        QThread.__init__(self, parent)
        self.port = str(port)
        self.baud = int(baud)
        self._stop = threading.Event()
        self._tx = collections.deque()          # 待发命令（deque 的 append/popleft 线程安全）
        self.ser = None

    # ---- 给 UI 线程调用 ----
    def send(self, payload):
        if not isinstance(payload, (bytes, bytearray)):
            payload = str(payload).encode('ascii', 'replace')
        self._tx.append(bytes(payload))

    def stop(self):
        self._stop.set()
        try:                                    # 关端口能把阻塞中的 read 唤醒
            if self.ser is not None:
                self.ser.close()
        except Exception:
            pass

    # ---- 线程体 ----
    def run(self):
        try:
            import serial
        except Exception as exc:
            self.failed.emit('未安装 pyserial，无法用串口切算法: pip install pyserial (%s)' % exc)
            self.opened.emit(False, '串口不可用')
            return
        try:
            self.ser = serial.Serial(self.port, self.baud,
                                     bytesize=8, parity='N', stopbits=1,
                                     timeout=SERIAL_TIMEOUT,
                                     write_timeout=SERIAL_WRITE_TIMEOUT)
        except Exception as exc:
            self.failed.emit('打开串口 %s 失败: %s' % (self.port, exc))
            self.opened.emit(False, '打开失败')
            return
        print('[SERIAL] 已打开 %s @%d 8N1' % (self.port, self.baud))
        self.opened.emit(True, '%s 已连接 @%d 8N1' % (self.port, self.baud))
        buf = bytearray()
        try:
            while not self._stop.is_set():
                # (a) 把队列里的命令发出去
                while True:
                    try:
                        payload = self._tx.popleft()
                    except IndexError:
                        break
                    try:
                        self.ser.write(payload)
                        self.ser.flush()
                        print('[SERIAL] -> %r' % payload)
                    except Exception as exc:
                        if self._stop.is_set():
                            return
                        self.failed.emit('串口发送失败: %s' % exc)
                        return
                # (b) 收数据，按行切
                try:
                    chunk = self.ser.read(64)
                except Exception as exc:
                    if self._stop.is_set():
                        return
                    self.failed.emit('串口读取失败: %s' % exc)
                    return
                if chunk:
                    buf.extend(chunk)
                    while True:
                        index = buf.find(b'\n')
                        if index < 0:
                            break
                        raw = bytes(buf[:index])
                        del buf[:index + 1]
                        line = raw.decode('ascii', 'replace').strip()
                        if line:
                            self.lineReceived.emit(line)
                    if len(buf) > 4096:         # 防御：没有换行的垃圾数据
                        buf = bytearray()
                else:
                    time.sleep(0.02)
        finally:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
            print('[SERIAL] 串口已关闭 %s' % self.port)
            self.opened.emit(False, '已断开')


class MjpegWorker(QThread):
    """MJPEG 预览线程：连 /stream.mjpg，解析出 JPEG，解码成 QImage 后 emit"""

    imageReady = pyqtSignal(object)     # QImage（跨线程传 QImage 是安全的）
    frameBytes = pyqtSignal(int)        # 单帧 JPEG 字节数
    netOK = pyqtSignal(str)
    netError = pyqtSignal(str)

    def __init__(self, url, timeout=STREAM_TIMEOUT, parent=None):
        QThread.__init__(self, parent)
        self.url = str(url)
        self.timeout = float(timeout)
        self._stop = threading.Event()
        self._resp = None

    def stop(self):
        self._stop.set()
        try:
            if self._resp is not None:
                self._resp.close()
        except Exception:
            pass

    def _nap(self, seconds):
        """可被打断的等待，返回 True 表示该退出了"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._stop.is_set():
                return True
            time.sleep(0.05)
        return self._stop.is_set()

    def run(self):
        while not self._stop.is_set():
            try:
                request = Request(self.url, headers={'User-Agent': _USER_AGENT})
                self._resp = urlopen(request, timeout=self.timeout)
            except Exception as exc:
                if self._stop.is_set():
                    return
                self.netError.emit('预览流连接失败: %s' % describe_net_error(exc))
                if self._nap(1.5):
                    return
                continue
            print('[MJPEG] 已连接 %s' % self.url)
            self.netOK.emit(self.url)
            parser = MjpegParser()
            try:
                while not self._stop.is_set():
                    chunk = self._resp.read(4096)
                    if not chunk:
                        break
                    for jpeg in parser.feed(chunk):
                        self.frameBytes.emit(len(jpeg))
                        image = QImage.fromData(jpeg)
                        if image is None or image.isNull():
                            self.netError.emit('预览帧解码失败（收到 %d 字节）' % len(jpeg))
                            continue
                        self.imageReady.emit(image)
            except Exception as exc:
                if self._stop.is_set():
                    return
                self.netError.emit('预览流中断: %s' % describe_net_error(exc))
            finally:
                try:
                    self._resp.close()
                except Exception:
                    pass
                self._resp = None
            if self._nap(1.0):
                return


class StatusWorker(QThread):
    """每 1 秒轮询 /api/status，更新压缩率 / 码率 / 板卡帧率"""

    statusReady = pyqtSignal(object)    # dict（parse_status_json 的结果）
    statusError = pyqtSignal(str)

    def __init__(self, base, interval=STATUS_POLL_INTERVAL,
                 timeout=HTTP_TIMEOUT, parent=None):
        QThread.__init__(self, parent)
        self.url = url_status(base)
        self.interval = float(interval)
        self.timeout = float(timeout)
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            started = time.time()
            try:
                payload = fetch_bytes(self.url, timeout=self.timeout)
                self.statusReady.emit(parse_status_json(payload))
            except Exception as exc:
                if self._stop.is_set():
                    return
                self.statusError.emit('/api/status 读取失败: %s' % describe_net_error(exc))
            remain = self.interval - (time.time() - started)
            deadline = time.time() + max(0.1, remain)
            while time.time() < deadline:
                if self._stop.is_set():
                    return
                time.sleep(0.05)


class DimPoller(QThread):
    """
    每 2 秒 `GET /api/dim` 读尺寸测量结果（**只读缓存**，板卡侧实测 0 ms 不阻塞）。

    ⚠⚠ **尺寸测量是"触发式"，不是每帧、也不是每次轮询都真去测**（设计意图，别改坏）：
      板卡 `measure()` 在 PC 上一帧要 ~0.46 s（板上 CPU 更慢），逐帧跑会把画面帧率打到
      2~3 fps。所以板卡侧只有两种情况真去量：① 进 dim 模式第一帧起一次**后台标定**；
      ② `dim_auto` 到点，或者我们主动 `GET /api/dim?run=1`（= 界面的「测量一次」按钮）。
      本线程**永远只读缓存**；千万不要把它改成"每次轮询都带 run=1" —— 那等于让板卡
      每 2 秒做一次 0.46 s 的测量，画面会直接卡住。要人工重测，走 `_on_dim_run()`。

    旧板卡没有这个路由 -> HTTP 404：照样把 supported=False 的结果 emit 出去，
    界面把表格清空 + 摘要行写"板卡版本过旧"，不当网络错误、不把板卡标离线。
    """

    dimReady = pyqtSignal(object)       # parse_dim_json 的结果
    dimError = pyqtSignal(str)

    def __init__(self, base, interval=DIM_POLL_INTERVAL,
                 timeout=DIM_TIMEOUT, parent=None):
        QThread.__init__(self, parent)
        self.url = url_dim(base)
        self.interval = float(interval)
        self.timeout = float(timeout)
        self._stop = threading.Event()
        self.polls = 0

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            started = time.time()
            self.polls += 1
            try:
                payload = fetch_bytes(self.url, timeout=self.timeout)
                self.dimReady.emit(parse_dim_json(payload))
            except HTTPError as exc:
                body = b''
                try:
                    body = exc.read() or b''
                except Exception:
                    body = b''
                if self._stop.is_set():
                    return
                try:
                    self.dimReady.emit(parse_dim_json(body, http_status=exc.code))
                except ValueError:
                    self.dimError.emit('/api/dim 读取失败: HTTP %s' % exc.code)
            except Exception as exc:
                if self._stop.is_set():
                    return
                self.dimError.emit('/api/dim 读取失败: %s' % describe_net_error(exc))
            remain = self.interval - (time.time() - started)
            deadline = time.time() + max(0.1, remain)
            while time.time() < deadline:
                if self._stop.is_set():
                    return
                time.sleep(0.05)


class DimRunWorker(QThread):
    """
    一次性 `GET /api/dim?run=1`：用最新帧**立刻量一次**（点「测量一次」按钮）。

    板卡侧没标定过会先起后台标定并返回 state=calib（不干等），所以这里还是按正常结果处理。
    """

    dimReady = pyqtSignal(object)
    dimError = pyqtSignal(str)

    def __init__(self, base, timeout=DIM_TIMEOUT, parent=None):
        QThread.__init__(self, parent)
        self.url = url_dim(base, run=1)
        self.timeout = float(timeout)
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            payload = fetch_bytes(self.url, timeout=self.timeout)
        except HTTPError as exc:
            body = b''
            try:
                body = exc.read() or b''
            except Exception:
                body = b''
            if self._stop.is_set():
                return
            try:
                self.dimReady.emit(parse_dim_json(body, http_status=exc.code))
            except ValueError:
                self.dimError.emit('/api/dim?run=1 失败: HTTP %s' % exc.code)
            return
        except Exception as exc:
            if self._stop.is_set():
                return
            self.dimError.emit('/api/dim?run=1 失败: %s' % describe_net_error(exc))
            return
        if self._stop.is_set():
            return
        try:
            self.dimReady.emit(parse_dim_json(payload))
        except ValueError as exc:
            self.dimError.emit('/api/dim 返回格式不对: %s' % exc)


class DimCalibWorker(QThread):
    """
    一次性 `GET /api/dim?calib=1`：让板卡用**最新帧后台重标定一次**（「重新标定」按钮，t26）。

    为什么不是 `/api/config?calib=1`：板卡 `_config()` 只认 autosave/detect/dim/dim_auto*，
    未知参数**直接忽略**（只会当成一次查询），真正触发重标的是 `/api/dim?calib=1`
    （t18 契约，也就是 url_dim(base, calib=1) 的既有语义）。所以这里走 url_dim。

    板卡侧是后台线程标定（不阻塞主循环/HTTP），所以本请求**很快返回**当前快照
    （通常 state=calib、recalib_queued=1）；标定结果要等随后的 /api/dim 轮询回读。
    不做自动连续标定、不做超时重试（需求如此）。
    """

    dimReady = pyqtSignal(object)
    dimError = pyqtSignal(str)

    def __init__(self, base, timeout=DIM_TIMEOUT, parent=None):
        QThread.__init__(self, parent)
        self.url = url_dim(base, calib=1)
        self.timeout = float(timeout)
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            payload = fetch_bytes(self.url, timeout=self.timeout)
        except HTTPError as exc:
            body = b''
            try:
                body = exc.read() or b''
            except Exception:
                body = b''
            if self._stop.is_set():
                return
            try:
                self.dimReady.emit(parse_dim_json(body, http_status=exc.code))
            except ValueError:
                self.dimError.emit('/api/dim?calib=1 失败: HTTP %s' % exc.code)
            return
        except Exception as exc:
            if self._stop.is_set():
                return
            self.dimError.emit('/api/dim?calib=1 失败: %s' % describe_net_error(exc))
            return
        if self._stop.is_set():
            return
        try:
            self.dimReady.emit(parse_dim_json(payload))
        except ValueError as exc:
            self.dimError.emit('/api/dim 返回格式不对: %s' % exc)


class EventsWorker(QThread):
    """
    事件增量轮询线程：每 1 秒 GET /api/events?since=<上次 next>，
    把整包交给 UI 线程用 EventCursor 去重/排序后追加到日志面板。
    增量游标由本线程持有（断线自动续上），UI 侧再有一层 seq 去重兜底。
    """

    batchReady = pyqtSignal(object)     # parse_events_json 的结果 dict
    eventsError = pyqtSignal(str)

    def __init__(self, base, since=0, interval=EVENTS_POLL_INTERVAL,
                 timeout=HTTP_TIMEOUT, parent=None):
        QThread.__init__(self, parent)
        self.base = str(base)
        self.since = max(0, int(since or 0))
        self.interval = float(interval)
        self.timeout = float(timeout)
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            started = time.time()
            try:
                payload = fetch_bytes(url_events(self.base, self.since),
                                      timeout=self.timeout)
                batch = parse_events_json(payload)
            except Exception as exc:
                if self._stop.is_set():
                    return
                self.eventsError.emit('事件日志读取失败: %s' % describe_net_error(exc))
            else:
                next_seq = batch.get('next')
                if next_seq is not None:
                    self.since = max(0, int(next_seq))
                self.batchReady.emit(batch)
            remain = self.interval - (time.time() - started)
            deadline = time.time() + max(0.1, remain)
            while time.time() < deadline:
                if self._stop.is_set():
                    return
                time.sleep(0.05)


class DefectPoller(QThread):
    """
    缺陷图墙轮询线程：默认每 2 秒拉一次 /api/defects；点"刷新列表"/截图后
    调 poll_now() 立刻拉一次（只发信号，UI 侧按文件名去重合并）。
    """

    listReady = pyqtSignal(object)      # list[dict{'name','size','mtime'}]
    error = pyqtSignal(str)

    def __init__(self, base, interval=DEFECT_POLL_INTERVAL,
                 timeout=HTTP_TIMEOUT, parent=None):
        QThread.__init__(self, parent)
        self.base = str(base)
        self.interval = float(interval)
        self.timeout = float(timeout)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._wake.set()                 # 启动后立刻拉一次，不用等 2 秒

    def poll_now(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def run(self):
        while not self._stop.is_set():
            self._wake.wait(self.interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                payload = fetch_bytes(url_defects(self.base), timeout=self.timeout)
                items = parse_defects_json(payload)
            except Exception as exc:
                if self._stop.is_set():
                    return
                self.error.emit('缺陷列表读取失败: %s' % describe_net_error(exc))
                continue
            self.listReady.emit(items)


class ThumbWorker(QThread):
    """
    缩略图队列线程：UI 把"新文件"的名字塞进队列，本线程逐个取 /defects/<name>，
    用 PIL 缩成 160x90，emit 给 UI 设图标。队列空时 0.25 秒醒一次看有没有新活。
    """

    thumbReady = pyqtSignal(object)     # dict{'name','thumb'(png bytes|None),'error'}

    def __init__(self, base, timeout=HTTP_TIMEOUT, parent=None):
        QThread.__init__(self, parent)
        self.base = str(base)
        self.timeout = float(timeout)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._queue = collections.deque()

    def enqueue(self, name):
        try:
            self._queue.append(str(name))
        except Exception:
            return
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def run(self):
        while not self._stop.is_set():
            try:
                name = self._queue.popleft()
            except IndexError:
                self._wake.wait(0.25)
                self._wake.clear()
                continue
            try:
                raw = fetch_bytes(url_defect_file(self.base, name), timeout=self.timeout)
                thumb = make_thumbnail(raw)
                self.thumbReady.emit({'name': name, 'thumb': thumb, 'error': ''})
            except Exception as exc:
                self.thumbReady.emit({'name': name, 'thumb': None, 'error': str(exc)})


class SnapWorker(QThread):
    """"📷 截图存 SD" 一次性线程：GET /api/snap，把 JSON 结果交给 UI"""

    snapReady = pyqtSignal(object)      # parse_snap_json 的结果 dict
    snapError = pyqtSignal(str)

    def __init__(self, base, timeout=DOWNLOAD_TIMEOUT, parent=None):
        QThread.__init__(self, parent)
        self.base = str(base)
        self.timeout = float(timeout)
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            payload = fetch_bytes(url_snap(self.base), timeout=self.timeout)
        except HTTPError as exc:
            # 板卡"无帧"会返回 503 + {"ok":false,"error":"no frame"}，先按 JSON 读
            body = b''
            try:
                body = exc.read() or b''
            except Exception:
                body = b''
            if self._stop.is_set():
                return
            try:
                result = parse_snap_json(body)
            except ValueError:
                self.snapError.emit('截图失败: HTTP %s %s'
                                    % (exc.code, getattr(exc, 'reason', '')))
                return
            result['http_status'] = exc.code
            self.snapReady.emit(result)
            return
        except Exception as exc:
            if self._stop.is_set():
                return
            self.snapError.emit('截图失败: %s' % describe_net_error(exc))
            return
        if self._stop.is_set():
            return
        try:
            result = parse_snap_json(payload)
        except ValueError as exc:
            self.snapError.emit('截图返回格式不对: %s' % exc)
            return
        self.snapReady.emit(result)


class ConfigWorker(QThread):
    """
    板卡功能开关线程（`/api/config`）：一次性请求，成功/失败都把结果交给 UI。
      · 不带参数 = 只查询（用来提前拿到 "no model loaded" / dim 回读 这类信息）
      · 带 autosave= / detect= / dim= = 只改传了的那些（契约 t11 与 t18）
    注意：HTTP 400（非法值）也是**正常结果**，要把 error 文案解析出来给状态栏，不能当网络错误。
    """

    configReady = pyqtSignal(object)    # parse_config_json 的结果
    configError = pyqtSignal(str)

    def __init__(self, base, autosave=None, detect=None, dim=None,
                 timeout=HTTP_TIMEOUT, parent=None):
        QThread.__init__(self, parent)
        self.url = url_config(base, autosave=autosave, detect=detect, dim=dim)
        self.query_only = (autosave is None and detect is None and dim is None)
        self.timeout = float(timeout)
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            payload = fetch_bytes(self.url, timeout=self.timeout)
        except HTTPError as exc:
            body = b''
            try:
                body = exc.read() or b''
            except Exception:
                body = b''
            if self._stop.is_set():
                return
            try:
                result = parse_config_json(body, http_status=exc.code)
            except ValueError:
                self.configError.emit('/api/config 失败: HTTP %s %s'
                                      % (exc.code, getattr(exc, 'reason', '')))
                return
            self.configReady.emit(result)
            return
        except Exception as exc:
            if self._stop.is_set():
                return
            self.configError.emit('/api/config 读取失败: %s' % describe_net_error(exc))
            return
        if self._stop.is_set():
            return
        try:
            result = parse_config_json(payload)
        except ValueError as exc:
            self.configError.emit('/api/config 返回格式不对: %s' % exc)
            return
        self.configReady.emit(result)


class PushPoller(QThread):
    """
    每 1 秒 `GET /api/push` 读推送状态（跟 /api/status 一起轮询，互不影响）。

    只读、不改任何东西：界面上的"推送: 开/关"永远来自这里回读的快照。
    旧板卡没有这个路由 -> HTTP 404：照样把 parse_push_json 的结果 emit 出去
    （supported=False），让界面把整组置灰，而不是当成网络错误把整个板卡标离线。
    """

    stateReady = pyqtSignal(object)     # parse_push_json 的结果（含 supported）
    pushError = pyqtSignal(str)         # 真的连不上/返回不是 JSON

    def __init__(self, base, interval=PUSH_POLL_INTERVAL,
                 timeout=HTTP_TIMEOUT, parent=None):
        QThread.__init__(self, parent)
        self.url = url_push(base)
        self.interval = float(interval)
        self.timeout = float(timeout)
        self._stop = threading.Event()
        self.polls = 0

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            started = time.time()
            self.polls += 1
            try:
                payload = fetch_bytes(self.url, timeout=self.timeout)
                self.stateReady.emit(parse_push_json(payload))
            except HTTPError as exc:
                body = b''
                try:
                    body = exc.read() or b''
                except Exception:
                    body = b''
                if self._stop.is_set():
                    return
                try:
                    self.stateReady.emit(parse_push_json(body, http_status=exc.code))
                except ValueError:
                    self.pushError.emit('/api/push 读取失败: HTTP %s'
                                        % exc.code)
            except Exception as exc:
                if self._stop.is_set():
                    return
                self.pushError.emit('/api/push 读取失败: %s' % describe_net_error(exc))
            remain = self.interval - (time.time() - started)
            deadline = time.time() + max(0.1, remain)
            while time.time() < deadline:
                if self._stop.is_set():
                    return
                time.sleep(0.05)


class PushWorker(QThread):
    """
    一次性推送操作线程：开关 / 改目标 URL / 发送测试（`POST /api/push?...`）。

    注意：**不在这里改界面文字**——按钮文字只认随后的 GET /api/push 回读
    （与 /api/config 的开关同一风格，避免"点了就显示开"的假象）。
    HTTP 400（非法 url）是**正常结果**：要把 error 文案交给界面提示行，不能当网络错误。
    """

    pushReady = pyqtSignal(object)      # parse_push_json 的结果
    pushError = pyqtSignal(str)

    def __init__(self, base, enabled=None, url=None, test=None,
                 timeout=PUSH_TIMEOUT, parent=None):
        QThread.__init__(self, parent)
        self.url = url_push(base, enabled=enabled, url=url, test=test)
        self.timeout = float(timeout)
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            payload = post_bytes(self.url, timeout=self.timeout)
        except HTTPError as exc:
            body = b''
            try:
                body = exc.read() or b''
            except Exception:
                body = b''
            if self._stop.is_set():
                return
            try:
                self.pushReady.emit(parse_push_json(body, http_status=exc.code))
            except ValueError:
                self.pushError.emit('/api/push 设置失败: HTTP %s %s'
                                    % (exc.code, getattr(exc, 'reason', '')))
            return
        except Exception as exc:
            if self._stop.is_set():
                return
            self.pushError.emit('/api/push 设置失败: %s' % describe_net_error(exc))
            return
        if self._stop.is_set():
            return
        try:
            self.pushReady.emit(parse_push_json(payload))
        except ValueError as exc:
            self.pushError.emit('/api/push 返回格式不对: %s' % exc)


class NetWorker(QThread):
    """
    一次性 `GET /api/net`（板卡 t14）：拿到各网卡 IPv4，给二维码挑"手机能连到的地址"。

    只读、每连一次板卡问一次（不是每秒轮询）。旧板卡没有这个路由 -> HTTP 404：
    也当**正常结果**交出去（supported=False），界面退化成"用界面上的板卡 IP"，不报错。
    """

    netReady = pyqtSignal(object)       # parse_net_json 的结果
    netError = pyqtSignal(str)

    def __init__(self, base, timeout=NET_TIMEOUT, parent=None):
        QThread.__init__(self, parent)
        self.url = _join(base, 'api/net')
        self.timeout = float(timeout)
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            payload = fetch_bytes(self.url, timeout=self.timeout)
        except HTTPError as exc:
            body = b''
            try:
                body = exc.read() or b''
            except Exception:
                body = b''
            if self._stop.is_set():
                return
            try:
                self.netReady.emit(parse_net_json(body, http_status=exc.code))
            except ValueError:
                self.netError.emit('/api/net 读取失败: HTTP %s' % exc.code)
            return
        except Exception as exc:
            if self._stop.is_set():
                return
            self.netError.emit('/api/net 读取失败: %s' % describe_net_error(exc))
            return
        if self._stop.is_set():
            return
        try:
            self.netReady.emit(parse_net_json(payload))
        except ValueError as exc:
            self.netError.emit('/api/net 返回格式不对: %s' % exc)


class DownloadWorker(QThread):
    """下载选中的缺陷图到 ./downloads/（同名自动改名，不覆盖）"""

    saved = pyqtSignal(str)             # 已保存的本地绝对路径
    failed = pyqtSignal(str)
    finishedAll = pyqtSignal(int)       # 成功张数

    def __init__(self, base, download_dir, names, timeout=DOWNLOAD_TIMEOUT, parent=None):
        QThread.__init__(self, parent)
        self.base = str(base)
        self.download_dir = download_dir
        self.names = list(names)
        self.timeout = float(timeout)
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        done = 0
        for name in self.names:
            if self._stop.is_set():
                break
            try:
                raw = fetch_bytes(url_defect_file(self.base, name), timeout=self.timeout)
                path = save_bytes(local_path_for(self.download_dir, name), raw)
                done += 1
                self.saved.emit(path)
            except Exception as exc:
                self.failed.emit('%s 下载失败: %s' % (name, describe_net_error(exc)))
        self.finishedAll.emit(done)


# ============================================================================
# 9. 界面
# ============================================================================

class OutlineWorker(QThread):
    ready = pyqtSignal(object, object)
    failed = pyqtSignal(str)

    def __init__(self, base, profile, parent=None):
        QThread.__init__(self, parent)
        self.base, self.profile = base, profile
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            from board_outline import measure_outline
            payload = fetch_bytes(self.base.rstrip('/') + '/snap.jpg', timeout=HTTP_TIMEOUT)
            result, png = measure_outline(payload, self.profile)
            if not self._stop.is_set():
                self.ready.emit(result, png)
        except Exception as exc:
            if not self._stop.is_set():
                self.failed.emit('板框测量失败：%s' % exc)


class AspectLabel(QLabel):
    """等比缩放显示 QImage 的 QLabel —— 只缩放、不拉伸变形"""

    def __init__(self, text='', parent=None):
        QLabel.__init__(self, text, parent)
        self._image = None
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(320, 180)
        self.setStyleSheet('background-color:#101010; color:#c8c8c8;')

    def set_image(self, image):
        self._image = image
        self.refresh()

    def refresh(self):
        if self._image is None:
            return
        pixmap = QPixmap.fromImage(self._image)
        if pixmap is None or pixmap.isNull():
            return
        target_w = max(1, self.width() - 4)
        target_h = max(1, self.height() - 4)
        self.setPixmap(pixmap.scaled(target_w, target_h,
                                     Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        QLabel.resizeEvent(self, event)
        try:
            self.refresh()
        except Exception:
            traceback.print_exc()


class MainWindow(QMainWindow):
    """上位机主窗口：左侧控制栏 + 右侧（上）预览 /（下）缺陷图墙"""

    def __init__(self, ip=DEFAULT_BOARD_IP, port=DEFAULT_BOARD_PORT,
                 download_dir=DEFAULT_DOWNLOAD_DIR, serial_port=None,
                 auto_net=True):
        QMainWindow.__init__(self)
        # ---- 运行时状态 ----
        self.board_base = build_base(ip, port)          # 非法 IP 会在这里抛 ValueError
        host_port = self.board_base.split('://', 1)[1]
        initial_host, _, initial_port = host_port.rpartition(':')
        self._initial_ip = initial_host or DEFAULT_BOARD_IP
        self._initial_port = initial_port or str(DEFAULT_BOARD_PORT)
        self.download_dir = resolve_download_dir(download_dir)
        self.serial_worker = None
        self.mjpeg_worker = None
        self.status_worker = None
        self.events_worker = None
        self.defect_poller = None
        self.thumb_worker = None
        self.snap_worker = None
        self.download_worker = None
        self.config_worker = None                       # 正在进行的开关设置请求
        self.config_probe = None                        # 连上板卡时的一次性 /api/config 查询
        self._retired = []                              # 已停但可能还没退出的线程
        self._preview_image = None
        self._fps_frames = 0
        self._fps_stamp = time.time()
        self._defect_items = {}
        self._thumb_requested = set()                   # 已排队取缩略图的名字
        self._thumb_done = set()                        # 缩略图已成功设上的名字
        self._thumb_attempts = {}                       # 名字 -> 尝试次数（失败不无限重试）
        self._thumb_fail_logged = set()                 # 已打印过缩略图失败的名字
        self._event_cursor = EventCursor()               # 增量 + 去重游标
        self._manual_announced = collections.OrderedDict()  # 已提示过的截图名（防重复打印）
        self._log_lines = 0
        self._online = False
        self._last_net_error = ''
        # ---- 压缩质量（串口 Q/A/B 发命令 + /api/status 回读） ----
        self._board_qsel = None            # 最近一次回读的档位 0~3（高亮只认它）
        self._qsel_estimate = None         # 规划连发次数用：回读值 + 已发出的 Q 数
        self._q_plan = None                # 正在执行的"旋到某档"计划（None=闲置）
        self._board_auto = None            # 最近一次回读的 auto(0/1/None)
        self._board_target_kbps = None     # 最近一次回读的目标码率
        # ---- 板卡功能开关（HTTP /api/config：自动截图 / YOLO 检测 / 尺寸测量） ----
        self._board_autosave = None        # 回读的 autosave(0/1/None)
        self._board_detect = None          # 回读的 detect(0/1/None)
        self._switch_warning = ''          # 板卡给的 warning（如 no model loaded）
        self._switch_pending = None        # 正在等回读确认的 {name: target}
        # ---- 尺寸测量（/api/config 的 dim + /api/dim 结果表） ----
        self._board_dim = None             # 回读的 dim(0/1/None)：**只从 /api/config 读**
        self._config_seen = None           # 最近一次 /api/config 回读（dim/dim_auto_sec 出处）
        self._dim_warning = ''             # 'dim module not loaded'
        self._dim_state = None             # 最近一次 /api/dim（表格数据源）
        self.dim_poller = None             # 2 s 轮询 /api/dim 的常驻线程
        self.dim_run_worker = None         # 一次性 /api/dim?run=1
        self.outline_worker = None
        self._outline_dialogs = []
        self.dim_calib_worker = None       # 一次性 /api/dim?calib=1（「重新标定」）
        self._dim_calib_pending = False    # 点了标定、还没等到回读 -> 按钮先置灰
        self._dim_calib_req_done = False   # 标定请求本身已返回（下一次回读就解除置灰）
        # ---- 远程推送（HTTP /api/push：板卡把缺陷图 POST 到 PC 接收端） ----
        self._push_state = None            # 最近一次 /api/push 回读（dict）
        self._push_supported = None        # True/False/None（None=还没问过板卡）
        self._push_pending = None          # 正在等回读确认的 {'enabled': 0|1}
        self._push_hint_msg = ''           # 一次性反馈（400 bad url / 请求失败…），不被轮询盖掉
        self.push_worker = None            # 一次性的设置请求（开关/URL/测试）
        self.push_poller = None            # 1 秒轮询 /api/push 的常驻线程
        self._mobile_url = ''              # 手机监看地址（板卡 /m）
        self._qr_ok = False                # 二维码是否真的画出来了（缺库时 False）
        self._qr_dialog = None             # 点二维码弹出的大图对话框（保持引用）
        self._net_state = None             # 最近一次 /api/net（网卡列表，给二维码挑地址）
        self.net_worker = None             # 一次性 /api/net 线程
        self._push_url_auto = ''           # 上一次自动填的目标 URL（判断是否被用户改过）
        self._auto_net = bool(auto_net)
        # ---- 建界面 ----
        self._build_ui()
        self._refresh_ports(serial_port)
        self._update_board_label()
        self._refresh_push_url_suggestion()          # 推送目标 URL 的建议值（路由探测）
        self._apply_net_state({'ifs': [], 'supported': None})   # 先按"界面上的板卡 IP"
        self._refresh_push_buttons()       # 推送组初始状态（都是灰的，等回读）
        if self._auto_net:
            self._start_net_workers()
        else:
            self._set_net_state('板卡: %s (已用 --no-net 关闭自动连接)' % self.board_base, False)

    # ------------------------------------------------------------------ 界面
    def _build_ui(self):
        self.setWindowTitle(APP_TITLE)
        self.resize(1280, 720)

        # 菜单：连接 / 帮助
        menu_connect = self.menuBar().addMenu('连接')
        action_reconnect = QAction('重连板卡', self)
        action_reconnect.triggered.connect(self._on_apply_board)
        menu_connect.addAction(action_reconnect)
        action_drop = QAction('断开预览', self)
        action_drop.triggered.connect(self._on_disconnect_preview)
        menu_connect.addAction(action_drop)
        menu_connect.addSeparator()
        action_quit = QAction('退出', self)
        action_quit.triggered.connect(self.close)
        menu_connect.addAction(action_quit)
        menu_help = self.menuBar().addMenu('帮助')
        action_about = QAction('关于', self)
        action_about.triggered.connect(self._on_about)
        menu_help.addAction(action_about)

        central = QWidget(self)
        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # ---------------- 左侧控制栏 ----------------
        # 控制栏里控件很多（串口/算法/阈值/板卡/状态），整列的自然高度比 720 大。
        # 直接放进窗口会把窗口撑到 970 多像素高，在 1366x768 的笔记本上底部按钮会被
        # 屏幕切掉。所以整列套一层 QScrollArea：1080p 上看不出滚动条，小屏上可以滚。
        left_panel = QWidget(central)
        left_col = QVBoxLayout(left_panel)
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.setSpacing(6)
        left = left_panel

        # (1) 串口
        grp_serial = QGroupBox('串口 (J30, 115200 8N1)', central)
        serial_col = QVBoxLayout(grp_serial)
        self.cbo_port = QComboBox(grp_serial)
        serial_col.addWidget(self.cbo_port)
        row_serial = QHBoxLayout()
        self.btn_refresh = QPushButton('刷新', grp_serial)
        self.btn_refresh.clicked.connect(self._on_refresh_ports)
        row_serial.addWidget(self.btn_refresh)
        self.btn_serial = QPushButton('连接串口', grp_serial)
        self.btn_serial.clicked.connect(self._on_serial_button)
        row_serial.addWidget(self.btn_serial)
        serial_col.addLayout(row_serial)
        self.btn_dump = QPushButton('码流输出: 关 (点一下开)', grp_serial)
        self.btn_dump.clicked.connect(self._on_dump_toggle)
        serial_col.addWidget(self.btn_dump)
        self._dump_on = False
        self.lbl_serial = QLabel('串口: 未连接', grp_serial)
        serial_col.addWidget(self.lbl_serial)
        left_col.addWidget(grp_serial)

        # (2) 算法模式（赛题要求：上位机切换 FPGA 图像算法）
        grp_mode = QGroupBox('图像算法 (发串口命令给 FPGA)', central)
        mode_col = QVBoxLayout(grp_mode)
        self.grp_mode = QButtonGroup(grp_mode)
        self.radios = []
        for index, name in enumerate(MODE_NAMES):
            radio = QRadioButton('%s : %s' % (MODE_KEYS[index], name), grp_mode)
            radio.toggled.connect(lambda checked, i=index: self._on_mode_toggled(i, checked))
            self.grp_mode.addButton(radio, index)
            mode_col.addWidget(radio)
            self.radios.append(radio)
        self.lbl_mode = QLabel('当前模式: 未知（未收到 MODE= 回传）', grp_mode)
        mode_col.addWidget(self.lbl_mode)
        left_col.addWidget(grp_mode)

        # (2b) 压缩质量（t7 RTL 契约：Q=手动并循环四档 / A=自适应 / B=目标码率）
        grp_quality = QGroupBox('压缩质量 (串口 Q/A/B)', central)
        quality_col = QVBoxLayout(grp_quality)
        row_q = QHBoxLayout()
        self.grp_quality = QButtonGroup(grp_quality)
        self.grp_quality.setExclusive(True)
        self.q_buttons = []
        for index, name in enumerate(QSEL_NAMES):
            button = QPushButton(name, grp_quality)
            button.setCheckable(True)
            # 4 个档位挤在一行里：给上下限，避免不同 DPI/字体下把 Q90 挤到看不见
            button.setMinimumWidth(56)
            button.setMaximumWidth(88)
            button.setToolTip('切到 %s（FPGA 是循环切档，程序会自动连发 Q 上限 4 发/轮，'
                              '再按板卡回读核对）' % name)
            button.clicked.connect(lambda checked=False, i=index: self._on_quality_pick(i))
            self.grp_quality.addButton(button, index)
            row_q.addWidget(button, 1)
            self.q_buttons.append(button)
        quality_col.addLayout(row_q)
        row_q2 = QHBoxLayout()
        self.btn_auto = QPushButton('自适应 AUTO', grp_quality)
        self.btn_auto.setCheckable(True)
        self.btn_auto.setMinimumWidth(0)
        self.btn_auto.setToolTip("发 'A'：切到自适应码率模式（高亮按板卡回读的 auto 字段）")
        self.btn_auto.clicked.connect(self._on_quality_auto)
        row_q2.addWidget(self.btn_auto, 1)
        self.btn_target = QPushButton('目标码率 ⏵', grp_quality)
        self.btn_target.setMinimumWidth(0)
        self.btn_target.setToolTip("发 'B'：循环 %s KB/s（状态行只显示回读的 target_kbps，"
                                   "界面不自己乘单位）"
                                   % "/".join(str(k) for k in BITRATE_PRESETS_KBPS))
        self.btn_target.clicked.connect(self._on_quality_target)
        row_q2.addWidget(self.btn_target, 1)
        quality_col.addLayout(row_q2)
        self.lbl_quality = QLabel('质量 -- (--)  目标 --  实际 --', grp_quality)
        self.lbl_quality.setWordWrap(True)
        self.lbl_quality.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        quality_col.addWidget(self.lbl_quality)
        # (t15) 这里原来有一段 3 行的长帮助文字（档位怎么旋 / AUTO 怎么回手动），用户要求删掉。
        # 语义仍有价值 -> 改成**悬停提示**（不占版面）；分组与状态行都挂上，鼠标悬停即见。
        quality_tip = ('档位=切到该档：自动连发 Q（≤4 发/轮，间隔 250ms）并按板卡回读核对；'
                       '高亮只跟回读（约 1 秒后刷新）。AUTO 想回手动请点任一档位。')
        self.grp_quality_box = grp_quality      # QGroupBox（self.grp_quality 是 QButtonGroup）
        grp_quality.setToolTip(quality_tip)
        self.lbl_quality.setToolTip(quality_tip)
        left_col.addWidget(grp_quality)
        self._set_quality_enabled(False)        # 串口没连时这组按钮全部禁用

        # (2c) 板卡功能开关（走 HTTP /api/config，与串口无关）
        grp_switch = QGroupBox('板卡功能开关 (HTTP /api/config)', central)
        switch_col = QVBoxLayout(grp_switch)
        row_switch = QHBoxLayout()
        self.btn_autosave = QPushButton('自动截图: --', grp_switch)
        self.btn_autosave.setMinimumWidth(0)
        self.btn_autosave.setToolTip('关掉后，检出缺陷不再自动存图，但 📷 手动截图仍然可用')
        self.btn_autosave.clicked.connect(self._on_toggle_autosave)
        row_switch.addWidget(self.btn_autosave, 1)
        self.btn_detect = QPushButton('目标检测: --', grp_switch)
        self.btn_detect.setMinimumWidth(0)
        self.btn_detect.setToolTip('关掉后板卡不再跑 YOLO 推理：画面上的检测框消失、帧率会上升')
        self.btn_detect.clicked.connect(self._on_toggle_detect)
        row_switch.addWidget(self.btn_detect, 1)
        switch_col.addLayout(row_switch)
        # (t19) 尺寸测量：与「目标检测」**互斥**（用户明确不要求同时开）。
        # 点"尺寸测量开"会一次带上 detect=0&dim=1；点"检测开"会带上 dim=0&detect=1。
        row_switch2 = QHBoxLayout()
        self.btn_dim = QPushButton('尺寸测量: --', grp_switch)
        self.btn_dim.setMinimumWidth(0)
        self.btn_dim.setToolTip('尺寸测量：与「目标检测」互斥（开测量会自动关掉检测，反之亦然）；'
                                '按钮文字只认板卡 /api/config 回读（约 1 秒）')
        self.btn_dim.clicked.connect(self._on_toggle_dim)
        row_switch2.addWidget(self.btn_dim, 1)
        switch_col.addLayout(row_switch2)
        # (t26) 「重新标定」：尺寸测量开关下面一行（标定只能在 dim 模式下做）。
        # 点完立刻置灰、按钮文字不变；结果行只认 /api/dim 回读（calib_ok/calib_note/sigma_mm）。
        row_switch3 = QHBoxLayout()
        self.btn_dim_calib = QPushButton('重新标定', grp_switch)
        self.btn_dim_calib.setMinimumWidth(0)
        self.btn_dim_calib.setMaximumWidth(96)
        self.btn_dim_calib.setToolTip(
            '让板卡用最新一帧重新标定一次（GET /api/dim?calib=1，后台线程，不卡画面）；'
            '尺寸测量没开时不可点；结果只认 /api/dim 回读')
        self.btn_dim_calib.clicked.connect(self._on_dim_calib)
        row_switch3.addWidget(self.btn_dim_calib)
        self.lbl_dim_calib = QLabel('标定: --', grp_switch)
        self.lbl_dim_calib.setWordWrap(True)
        self.lbl_dim_calib.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row_switch3.addWidget(self.lbl_dim_calib, 1)
        switch_col.addLayout(row_switch3)
        # (t15) 这里原来还有一段 2 行的长帮助文字（关自动截图≠关手动截图 / 按钮只认回读），
        # 用户要求删掉 -> 归纳进悬停提示；动态原因（未连接 / 没加载模型 / 等回读）由
        # _refresh_switch_buttons() 写进这两个按钮与分组的 toolTip。
        self.grp_switch = grp_switch
        left_col.addWidget(grp_switch)

        # (2c) 远程推送（板卡 -> PC 接收端）+ 手机监看地址
        # 契约：GET/POST /api/push?enabled=0|1&url=...&test=1（不传参 = 只查询）；
        #       旧板卡没有这个路由（404）-> 整组置灰、文案 '--'、点击只提示版本过旧。
        self.grp_push = QGroupBox('远程推送 (板卡主动 POST 到 PC)', central)
        push_col = QVBoxLayout(self.grp_push)
        # 「开 / 关」两个按钮：当前态只用 /api/push 回读点亮（粗体+颜色），
        # 点按钮只发请求、**不**立刻变样式（否则界面会撒谎"已经开了"）。
        row_push = QHBoxLayout()
        row_push.addWidget(QLabel('推送', self.grp_push))
        self.btn_push_on = QPushButton('开', self.grp_push)
        self.btn_push_on.setMinimumWidth(48)
        self.btn_push_on.setToolTip('让板卡开始主动 POST 缺陷图（点完等 /api/push 回读，约 1 秒）')
        self.btn_push_on.clicked.connect(self._on_push_enable)
        row_push.addWidget(self.btn_push_on)
        self.btn_push_off = QPushButton('关', self.grp_push)
        self.btn_push_off.setMinimumWidth(48)
        self.btn_push_off.setToolTip('让板卡停止推送（线程与队列都还在，重新开就能继续发）')
        self.btn_push_off.clicked.connect(self._on_push_disable)
        row_push.addWidget(self.btn_push_off)
        self.btn_push_test = QPushButton('发送测试', self.grp_push)
        self.btn_push_test.setMinimumWidth(0)
        self.btn_push_test.setToolTip('让板卡立刻推一张测试图（走同一条投递路径），'
                                      '用来验 PC 接收端收不收得到')
        self.btn_push_test.clicked.connect(self._on_push_test)
        row_push.addWidget(self.btn_push_test, 1)
        push_col.addLayout(row_push)
        row_push_url = QHBoxLayout()
        row_push_url.addWidget(QLabel('目标 URL', self.grp_push))
        self.edit_push_url = QLineEdit(self.grp_push)
        self._push_url_auto = push_url_for_board(*self._board_host_port())
        self.edit_push_url.setText(self._push_url_auto)   # 板卡能到达的本机 IP + :9000/upload
        self.edit_push_url.setToolTip('PC 接收端地址（tools/recv_upload.py 默认听 9000/upload）')
        row_push_url.addWidget(self.edit_push_url, 1)
        self.btn_push_url = QPushButton('应用', self.grp_push)
        self.btn_push_url.setMaximumWidth(56)
        self.btn_push_url.clicked.connect(self._on_push_apply_url)
        row_push_url.addWidget(self.btn_push_url)
        push_col.addLayout(row_push_url)
        self.lbl_push_url_hint = QLabel(PUSH_URL_AUTO_HINT, self.grp_push)
        self.lbl_push_url_hint.setWordWrap(True)
        self.lbl_push_url_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_push_url_hint.setStyleSheet('color:#666666;')
        push_col.addWidget(self.lbl_push_url_hint)
        self.lbl_push = QLabel(format_push_line(None), self.grp_push)
        self.lbl_push_err = QLabel('', self.grp_push)
        self.lbl_push_err.setStyleSheet('color:#c00000;')   # last_error：红色小字
        self.lbl_push_hint = QLabel(
            '板卡把缺陷图 POST 到 PC 接收端（tools/recv_upload.py）。'
            '开关/URL 都是"发请求 → 等 /api/push 回读"才改字。', self.grp_push)
        self.lbl_push_hint.setStyleSheet('color:#666666;')
        for label in (self.lbl_push, self.lbl_push_err, self.lbl_push_hint):
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            push_col.addWidget(label)
        # ---- 手机监看（板卡 /m）：左二维码、右明文地址（扫不动时手输） ----
        row_mobile = QHBoxLayout()
        self.lbl_qr = QLabel(self.grp_push)
        self.lbl_qr.setFixedSize(MOBILE_QR_SIZE, MOBILE_QR_SIZE)
        self.lbl_qr.setAlignment(Qt.AlignCenter)
        self.lbl_qr.setWordWrap(True)
        self.lbl_qr.setStyleSheet('color:#666666;')
        self.lbl_qr.setToolTip('手机扫码直接打开板卡 /m 手机页；点一下放大到 %dx%d'
                               % (MOBILE_QR_BIG_SIZE, MOBILE_QR_BIG_SIZE))
        self.lbl_qr.mousePressEvent = self._on_qr_click     # 点击弹大图
        row_mobile.addWidget(self.lbl_qr)
        col_mobile = QVBoxLayout()
        self.cbo_mobile = QComboBox(self.grp_push)
        self.cbo_mobile.setToolTip('二维码指向哪个板卡地址：默认选无线（手机能连到的那块）；'
                                   '板卡没连 WiFi 时只能用有线，会橙色警告')
        self.cbo_mobile.currentIndexChanged.connect(self._on_mobile_addr_changed)
        col_mobile.addWidget(self.cbo_mobile)
        self.lbl_mobile = QLabel('手机看: --', self.grp_push)
        self.lbl_mobile.setWordWrap(True)
        self.lbl_mobile.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_mobile.setToolTip('手机连同一个网络，浏览器打开这个地址即可看画面/告警')
        col_mobile.addWidget(self.lbl_mobile)
        self.lbl_net_warn = QLabel('', self.grp_push)
        self.lbl_net_warn.setWordWrap(True)
        self.lbl_net_warn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_net_warn.setStyleSheet('color:%s;' % COVER_WARN_COLOR)   # 橙色警告
        col_mobile.addWidget(self.lbl_net_warn)
        row_copy = QHBoxLayout()
        row_copy.addStretch(1)
        self.btn_copy_mobile = QPushButton('复制', self.grp_push)
        self.btn_copy_mobile.setMaximumWidth(56)
        self.btn_copy_mobile.setToolTip('把手机监看地址复制到剪贴板')
        self.btn_copy_mobile.clicked.connect(self._on_copy_mobile)
        row_copy.addWidget(self.btn_copy_mobile)
        col_mobile.addLayout(row_copy)
        row_mobile.addLayout(col_mobile, 1)
        push_col.addLayout(row_mobile)
        left_col.addWidget(self.grp_push)

        # (3) Sobel 阈值
        grp_thresh = QGroupBox('Sobel 阈值 (每次 ±%d)' % THRESH_STEP, central)
        thresh_col = QVBoxLayout(grp_thresh)
        row_thresh = QHBoxLayout()
        self.btn_thresh_minus = QPushButton('－  阈值 -%d' % THRESH_STEP, grp_thresh)
        self.btn_thresh_minus.clicked.connect(lambda: self._on_thresh(-1))
        row_thresh.addWidget(self.btn_thresh_minus)
        self.btn_thresh_plus = QPushButton('＋  阈值 +%d' % THRESH_STEP, grp_thresh)
        self.btn_thresh_plus.clicked.connect(lambda: self._on_thresh(1))
        row_thresh.addWidget(self.btn_thresh_plus)
        thresh_col.addLayout(row_thresh)
        self.lbl_thresh = QLabel('Sobel 阈值: --  (范围 8~2040)', grp_thresh)
        thresh_col.addWidget(self.lbl_thresh)
        left_col.addWidget(grp_thresh)

        # (4) 板卡地址
        grp_board = QGroupBox('板卡地址 (网线直连)', central)
        board_col = QVBoxLayout(grp_board)
        row_ip = QHBoxLayout()
        row_ip.addWidget(QLabel('IP', grp_board))
        self.edit_ip = QLineEdit(grp_board)
        self.edit_ip.setText(self._initial_ip)
        row_ip.addWidget(self.edit_ip)
        row_ip.addWidget(QLabel('端口', grp_board))
        self.edit_port = QLineEdit(grp_board)
        self.edit_port.setText(self._initial_port)
        self.edit_port.setMaximumWidth(70)
        row_ip.addWidget(self.edit_port)
        board_col.addLayout(row_ip)
        self.btn_apply = QPushButton('应用并重连', grp_board)
        self.btn_apply.clicked.connect(self._on_apply_board)
        board_col.addWidget(self.btn_apply)
        self.lbl_board = QLabel('板卡: %s' % self.board_base, grp_board)
        board_col.addWidget(self.lbl_board)
        left_col.addWidget(grp_board)

        # (5) 只读状态
        grp_state = QGroupBox('状态 (只读)', central)
        state_col = QVBoxLayout(grp_state)
        self.lbl_last_cmd = QLabel('最近发送: --', grp_state)
        self.lbl_last_reply = QLabel('最近应答: --', grp_state)
        self.lbl_ratio = QLabel('压缩率: --', grp_state)
        self.lbl_kbps = QLabel('码率: --', grp_state)
        self.lbl_fpga_fps = QLabel('FPGA 帧率: --', grp_state)
        self.lbl_preview_fps = QLabel('预览帧率: --', grp_state)
        self.lbl_defect_count = QLabel('缺陷图: --', grp_state)
        self.lbl_sd = QLabel('SD 可用: --', grp_state)
        # 覆盖率 / 字节每块（t1 新增字段）：紧贴 SD 那行。
        # 注意 cover = 本窗口编码块数 / 一帧满块数 32400（t2 轮转后约 31%，跨帧约 13 帧满覆盖），
        # 不是"整幅图覆盖率"；只有抽样明显偏少(<25%)才橙色提醒
        self.lbl_cover = QLabel(format_cover_line(None)[0], grp_state)
        for label in (self.lbl_last_cmd, self.lbl_last_reply, self.lbl_ratio,
                      self.lbl_kbps, self.lbl_fpga_fps, self.lbl_preview_fps,
                      self.lbl_defect_count, self.lbl_sd, self.lbl_cover):
            label.setWordWrap(True)
            # 这些标签里会出现很长的路径（SD 可用: … (C:\Users\…\downloads)），
            # QLabel 自动换行时会把"整段不可断的路径"当作最小宽度 → 把左栏撑宽、
            # 让按钮被裁掉。Ignored 让布局忽略它的宽度诉求，按可用宽度换行。
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            state_col.addWidget(label)
        left_col.addWidget(grp_state)
        left_col.addStretch(1)

        left_scroll = QScrollArea(central)
        left_scroll.setWidgetResizable(True)
        left_scroll.setFixedWidth(400)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_scroll.setWidget(left_panel)
        self.left_scroll = left_scroll
        root.addWidget(left_scroll)

        # ---------------- 右侧 ----------------
        right = QWidget(central)
        right_col = QVBoxLayout(right)
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(6)

        self.lbl_video = AspectLabel('等待板卡 MJPEG 画面…\n（连不上时请看状态栏提示）', right)
        right_col.addWidget(self.lbl_video, 3)

        grp_outline = QGroupBox('板框测量 · 棋盘格平面标定', right)
        outline_col = QVBoxLayout(grp_outline)
        outline_row = QHBoxLayout()
        self.cmb_outline_profile = QComboBox(grp_outline)
        self.cmb_outline_profile.addItem('自动定位当前 PCB 板框', 'rotated')
        outline_row.addWidget(self.cmb_outline_profile)
        self.btn_outline = QPushButton('测量板框', grp_outline)
        self.btn_outline.clicked.connect(self._on_outline_run)
        outline_row.addWidget(self.btn_outline)
        outline_col.addLayout(outline_row)
        self.lbl_outline = QLabel('点击测量，查看带四边框线的定格图和长宽。', grp_outline)
        self.lbl_outline.setWordWrap(True)
        outline_col.addWidget(self.lbl_outline)
        outline_note = QLabel('初测结果；相机、焦距和板面高度保持标定状态。板子大致水平、四边完整入镜。', grp_outline)
        outline_note.setWordWrap(True)
        outline_note.setStyleSheet('color:#a06000;')
        outline_col.addWidget(outline_note)
        right_col.addWidget(grp_outline)

        # ---- 尺寸测量结果表（板卡 GET /api/dim，只读；t19）----
        # 列：目标 / 测量值 / 真值 / 误差 / 判定；calib_ok=false 时整表置灰 + 红字原因。
        grp_dim = QGroupBox('尺寸测量结果 (板卡 /api/dim, 只读)', right)
        dim_col = QVBoxLayout(grp_dim)
        row_dim_top = QHBoxLayout()
        self.lbl_dim = QLabel(format_dim_summary(None), grp_dim)
        self.lbl_dim.setWordWrap(True)
        self.lbl_dim.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row_dim_top.addWidget(self.lbl_dim, 1)
        self.btn_dim_run = QPushButton('测量一次', grp_dim)
        self.btn_dim_run.setMaximumWidth(80)
        self.btn_dim_run.setToolTip('让板卡用最新一帧立刻测一次（GET /api/dim?run=1，约 0.3 s）')
        self.btn_dim_run.clicked.connect(self._on_dim_run)
        row_dim_top.addWidget(self.btn_dim_run)
        dim_col.addLayout(row_dim_top)
        self.tbl_dim = QTableWidget(0, len(DIM_TABLE_HEADERS), grp_dim)
        self.tbl_dim.setHorizontalHeaderLabels(list(DIM_TABLE_HEADERS))
        self.tbl_dim.setEditTriggers(QTableWidget.NoEditTriggers)   # 只读
        self.tbl_dim.setSelectionMode(QTableWidget.NoSelection)
        self.tbl_dim.verticalHeader().setVisible(False)
        self.tbl_dim.setWordWrap(False)
        try:
            self.tbl_dim.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        except Exception:
            traceback.print_exc()
        self.tbl_dim.setMaximumHeight(140)
        dim_col.addWidget(self.tbl_dim)
        self.lbl_dim_note = QLabel('', grp_dim)
        self.lbl_dim_note.setWordWrap(True)
        self.lbl_dim_note.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_dim_note.setStyleSheet('color:#c00000;')    # 标定不可信：红字原因
        dim_col.addWidget(self.lbl_dim_note)
        self.grp_dim = grp_dim
        right_col.addWidget(grp_dim)
        grp_dim.hide()  # 旧板端几何测量保留兼容接口，操作界面使用新板框测量。

        # 右下：缺陷图墙 + 事件日志（左右并排，都在同一个窗口里）
        bottom_row = QHBoxLayout()
        grp_defect = QGroupBox('缺陷图墙 (下载到 %s)' % self.download_dir, right)
        defect_row = QHBoxLayout(grp_defect)
        self.list_defects = QListWidget(grp_defect)
        self.list_defects.setViewMode(QListWidget.IconMode)
        self.list_defects.setIconSize(QSize(THUMB_W, THUMB_H))
        self.list_defects.setGridSize(QSize(THUMB_W + 24, THUMB_H + 48))
        self.list_defects.setResizeMode(QListWidget.Adjust)
        self.list_defects.setMovement(QListWidget.Static)
        self.list_defects.setSpacing(6)
        self.list_defects.setWordWrap(True)
        self.list_defects.setSelectionMode(QListWidget.ExtendedSelection)
        defect_row.addWidget(self.list_defects, 1)

        defect_buttons = QVBoxLayout()
        self.btn_snap = QPushButton('📷 截图存 SD', grp_defect)
        self.btn_snap.clicked.connect(self._on_snap)
        defect_buttons.addWidget(self.btn_snap)
        self.btn_list = QPushButton('刷新列表', grp_defect)
        self.btn_list.clicked.connect(self._on_refresh_defects)
        defect_buttons.addWidget(self.btn_list)
        self.btn_download = QPushButton('下载选中', grp_defect)
        self.btn_download.clicked.connect(self._on_download)
        defect_buttons.addWidget(self.btn_download)
        defect_buttons.addStretch(1)
        defect_row.addLayout(defect_buttons)
        bottom_row.addWidget(grp_defect, 3)

        grp_log = QGroupBox('事件日志 (板卡 /api/events, 每 1 秒增量)', right)
        log_col = QVBoxLayout(grp_log)
        self.log = QPlainTextEdit(grp_log)
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(LOG_MAX_LINES)   # 超过 2000 行自动删最老的
        self.log.setLineWrapMode(QPlainTextEdit.NoWrap)
        log_col.addWidget(self.log, 1)
        row_log = QHBoxLayout()
        self.btn_log_clear = QPushButton('清空日志', grp_log)
        self.btn_log_clear.clicked.connect(self._on_clear_log)
        row_log.addWidget(self.btn_log_clear)
        self.lbl_events = QLabel('事件: --', grp_log)
        row_log.addWidget(self.lbl_events)
        row_log.addStretch(1)
        log_col.addLayout(row_log)
        bottom_row.addWidget(grp_log, 2)

        right_col.addLayout(bottom_row, 2)
        root.addWidget(right, 1)

        self.setCentralWidget(central)

        # ---------------- 状态栏 ----------------
        self.lbl_net = QLabel('板卡: %s' % self.board_base, self)
        self.statusBar().addPermanentWidget(self.lbl_net)
        # 帧率单独放大显示：关掉 YOLO 检测后帧率会明显上升，演示时要一眼看出来
        self.lbl_fps_big = QLabel('FPGA 帧率 -- fps', self)
        self.lbl_fps_big.setStyleSheet('font-weight:bold;')
        self.statusBar().addPermanentWidget(self.lbl_fps_big)
        self._set_net_state('板卡: %s' % self.board_base, True)
        self.statusBar().showMessage('就绪', 5000)

        # 帧率显示用 1 秒定时器（UI 线程里的轻量统计，不涉及网络）
        self.timer_fps = QTimer(self)
        self.timer_fps.setInterval(1000)
        self.timer_fps.timeout.connect(self._on_fps_tick)
        self.timer_fps.start()

        # 压缩质量"旋档"用的单次定时器：连发 Q（250ms 间隔）与回读核对都排在它上面，
        # 全程在 UI 线程里做状态机，不阻塞界面（每次只发 1 个字节，没有网络请求）。
        self.timer_q = QTimer(self)
        self.timer_q.setSingleShot(True)
        self.timer_q.timeout.connect(self._on_quality_tick)

        # 断线时按钮禁用（首次连上板卡前也保持禁用），连上后自动恢复
        self._set_online(False)
        self._append_log('%s [系统] 上位机就绪，板卡 %s（下载目录 %s）'
                         % (time.strftime('%H:%M:%S'), self.board_base, self.download_dir))

    # ------------------------------------------------------- 状态栏 / 提示
    def _set_online(self, ok):
        """板卡在线/离线 -> 启用或禁用"需要板卡"的按钮（连不上时只能点重连）"""
        self._online = bool(ok)
        for button in (getattr(self, 'btn_snap', None),
                       getattr(self, 'btn_list', None),
                       getattr(self, 'btn_download', None)):
            if button is None:
                continue
            try:
                button.setEnabled(self._online)
            except Exception:
                traceback.print_exc()
        self._refresh_switch_buttons()      # 功能开关也跟着在线状态开关

    def _append_log(self, text):
        """往事件日志面板追加一行（只追加 + 自动滚到底；超过 2000 行由控件自动截断）"""
        try:
            self.log.appendPlainText(text)
            # appendPlainText 会把视图横向滚到"光标所在行尾"。只要有一行很长
            # （例如带完整下载路径的启动行），面板就会停在最右边，后面那些短行
            # 全都跑到视野左边之外 —— 看上去就像"日志面板是空的"（真机上实测会这样）。
            # 所以：如果横向滚动条正好停在最右端（说明是自动跟随），就拉回行首；
            # 用户自己手动横向拖过就不动它。
            bar = self.log.horizontalScrollBar()
            if bar is not None and bar.maximum() > 0 and bar.value() >= bar.maximum():
                bar.setValue(0)
            self._log_lines += 1
            self.lbl_events.setText('事件: %d 条 (seq %d)'
                                    % (self._log_lines, self._event_cursor.since()))
        except Exception:
            print('[ERR] 追加日志失败: %s' % text)
            traceback.print_exc()

    @_slot
    def _on_clear_log(self):
        """清空日志面板（不影响板卡的 events.jsonl，只清界面）"""
        try:
            self.log.clear()
        except Exception:
            traceback.print_exc()
        self._log_lines = 0
        self.lbl_events.setText('事件: 0 条 (seq %d)' % self._event_cursor.since())
        self._notify('事件日志已清空')

    def _event_is_announced(self, name):
        """板卡事件里"手动截图"这条，如果界面已经提示过就不再重复打印"""
        return bool(name) and name in self._manual_announced

    def _mark_announced(self, name):
        if not name:
            return
        self._manual_announced[name] = True
        while len(self._manual_announced) > 200:        # 别无限长
            self._manual_announced.popitem(last=False)
    def _set_net_state(self, text, ok):
        """状态栏右侧的板卡状态：绿色=正常，红色=连不上（不弹阻塞对话框）"""
        try:
            self.lbl_net.setText(text)
            self.lbl_net.setStyleSheet('color:#1a7f37;' if ok else 'color:#c00000;')
        except Exception:
            traceback.print_exc()

    def _notify(self, message, msec=5000):
        print('[UI] %s' % message)
        try:
            self.statusBar().showMessage(message, msec)
        except Exception:
            traceback.print_exc()

    def _update_board_label(self):
        try:
            self.lbl_board.setText('板卡: %s' % self.board_base)
            self.setWindowTitle('%s — %s' % (APP_TITLE, self.board_base))
        except Exception:
            traceback.print_exc()

    def _read_board_base(self):
        try:
            return build_base(self.edit_ip.text(), self.edit_port.text())
        except ValueError as exc:
            self._set_net_state('板卡地址不合法: %s' % exc, False)
            print('[ERR] 板卡地址不合法: %s' % exc)
            return None

    # ------------------------------------------------------------- 串口部分
    @_slot
    def _refresh_ports(self, prefer=None):
        """列出 pyserial 能看到的串口（没装 pyserial 就只提示，不崩）"""
        print('[SERIAL] 刷新串口列表…')
        try:
            from serial.tools import list_ports
        except Exception as exc:
            self.cbo_port.clear()
            self.cbo_port.addItem('(未安装 pyserial)')
            self.lbl_serial.setText('串口: 未安装 pyserial → pip install pyserial')
            print('[WARN] 未安装 pyserial: %s' % exc)
            return
        ports = list(list_ports.comports())
        self.cbo_port.clear()
        for port in ports:
            self.cbo_port.addItem(port.device)
            index = self.cbo_port.count() - 1
            self.cbo_port.setItemData(index, str(getattr(port, 'description', '') or ''),
                                      Qt.ToolTipRole)
            print('[SERIAL] 发现 %s  (%s)' % (port.device, getattr(port, 'description', '')))
        if not ports:
            self.cbo_port.addItem('(未发现串口)')
            self.lbl_serial.setText('串口: 未发现任何串口（插好 Type-C 后点"刷新"）')
            print('[WARN] 没有发现任何串口')
            return
        wanted = (prefer or '').strip()
        if wanted:
            index = self.cbo_port.findText(wanted)
            if index < 0:
                self.cbo_port.addItem(wanted)
                index = self.cbo_port.count() - 1
            self.cbo_port.setCurrentIndex(index)
        self.lbl_serial.setText('串口: 发现 %d 个端口（%s）'
                                % (len(ports), ports[0].device))

    @_slot
    def _on_refresh_ports(self):
        self._refresh_ports()

    @_slot
    def _on_serial_button(self):
        if self.serial_worker is not None:
            self._close_serial()
            return
        port = (self.cbo_port.currentText() or '').strip()
        if not port or port.startswith('('):
            self._notify('没有可用串口：请插好 USB Type-C，再点"刷新"')
            return
        self.serial_worker = SerialWorker(port)
        self.serial_worker.opened.connect(self._on_serial_opened)
        self.serial_worker.lineReceived.connect(self._on_serial_line)
        self.serial_worker.failed.connect(self._on_serial_failed)
        self.serial_worker.start()
        self.btn_serial.setText('断开串口')
        self.lbl_serial.setText('串口: 正在打开 %s …' % port)

    def _close_serial(self):
        worker = self.serial_worker
        self.serial_worker = None
        if worker is not None:
            try:
                worker.stop()
            except Exception:
                traceback.print_exc()
            self._retired.append(worker)
        try:
            self.btn_serial.setText('连接串口')
            self.lbl_serial.setText('串口: 已断开')
        except Exception:
            traceback.print_exc()
        self._set_quality_enabled(False)          # 串口没了，Q/A/B 按钮同步禁用
        self._refresh_quality_ui_from_board()
        print('[SERIAL] 用户断开串口')

    @_slot
    def _on_serial_opened(self, ok, message):
        self.lbl_serial.setText('串口: %s' % message)
        self._notify('串口: %s' % message)
        self._set_quality_enabled(bool(ok))       # 串口连上才允许切压缩质量
        if not ok:
            self.btn_serial.setText('连接串口')

    @_slot
    def _on_serial_failed(self, message):
        print('[ERR] %s' % message)
        self.lbl_serial.setText('串口: 出错')
        self._set_quality_enabled(False)
        self._set_net_state(message, False)
        self._notify(message, 8000)

    @_slot
    def _on_serial_line(self, line):
        kind, value = parse_serial_line(line)
        print('[SERIAL] <- %s  (%s)' % (line, kind))
        self.lbl_last_reply.setText('最近应答: %s' % line)
        if kind == 'mode':
            self._sync_mode_radio(value)
            if 0 <= value < len(MODE_NAMES):
                self.lbl_mode.setText('当前模式: %s = %s' % (MODE_KEYS[value], MODE_NAMES[value]))
        elif kind == 'thresh':
            self.lbl_thresh.setText('Sobel 阈值: %d  (范围 8~2040)' % value)
        elif kind == 'fps':
            self.lbl_fpga_fps.setText('FPGA 帧率: %d fps' % value)
        elif kind == 'banner':
            self.lbl_serial.setText('串口: 已连接（收到 PRE-OK 握手）')

    def _sync_mode_radio(self, index):
        """板卡回传的模式 -> 同步单选按钮（blockSignals 防止来回发命令）"""
        if index < 0 or index >= len(getattr(self, 'radios', [])):
            return
        radio = self.radios[index]
        try:
            radio.blockSignals(True)
            radio.setChecked(True)
        finally:
            radio.blockSignals(False)

    @_slot
    def _on_mode_toggled(self, index, checked):
        if not checked:
            return
        payload = mode_command(index)
        self._send_command(payload, '模式 %s (%s)' % (MODE_KEYS[index], MODE_NAMES[index]))
        self.lbl_mode.setText('当前模式: %s = %s (已发送)' % (MODE_KEYS[index], MODE_NAMES[index]))

    @_slot
    def _on_dump_toggle(self):
        """切换 JPEG 码流是否从串口输出（给'码流真从 FPGA 出来'取证用）"""
        worker = getattr(self, 'serial_worker', None)
        if worker is None or not worker.isRunning():   # 线程没跑 = 串口没打开
            self.statusBar().showMessage('串口未连接，先连接串口', 5000)
            return
        self.serial_worker.send(dump_command())
        self._dump_on = not self._dump_on
        self.btn_dump.setText('码流输出: %s (点一下%s)'
                              % ('开' if self._dump_on else '关',
                                 '关' if self._dump_on else '开'))
        self.statusBar().showMessage(
            '已发送 J：码流输出 %s（打开后终端会刷二进制，属正常）'
            % ('开' if self._dump_on else '关'), 6000)

    @_slot
    def _on_thresh(self, step):
        self._send_command(thresh_command(step), 'Sobel 阈值 %+d' % (step * THRESH_STEP))

    def _send_command(self, payload, note):
        """所有串口命令的唯一出口：没连串口就提示，绝不假装发出去了"""
        text = payload.decode('ascii', 'replace')
        if self.serial_worker is None:
            self.lbl_last_cmd.setText('最近发送: %s (未发送: 串口未连接)' % text)
            self._notify('串口未连接，命令 %r 没有发出去（先点"连接串口"）' % text)
            return False
        try:
            self.serial_worker.send(payload)
        except Exception as exc:
            print('[ERR] 命令入队失败: %s' % exc)
            traceback.print_exc()
            self._notify('命令入队失败: %s' % exc)
            return False
        self.lbl_last_cmd.setText('最近发送: %s  (%s)' % (text, note))
        print('[SERIAL] 入队命令 %r  (%s)' % (text, note))
        return True

    # ------------------------------------------------- 压缩质量 (Q/A/B)
    def _set_quality_enabled(self, enabled):
        """串口没连时禁用质量区按钮（Q/A/B 全走串口，没串口点了也没用）"""
        buttons = list(getattr(self, 'q_buttons', []))
        buttons.append(getattr(self, 'btn_auto', None))
        buttons.append(getattr(self, 'btn_target', None))
        for button in buttons:
            if button is None:
                continue
            try:
                button.setEnabled(bool(enabled))
            except Exception:
                traceback.print_exc()

    def _refresh_quality_ui_from_board(self):
        """把高亮拉回板卡真实值（未连串口 / 点击失败时用，避免"点了就亮"的假象）"""
        self._apply_quality_highlight(self._board_qsel)
        try:
            self.btn_auto.setChecked(self._board_auto == 1)
        except Exception:
            traceback.print_exc()

    def _apply_quality_highlight(self, qsel):
        """档位按钮高亮只跟回读的 qsel 走（FPGA 循环切档，绝不能按点击猜）"""
        index = quality_checked_index(qsel)
        for position, button in enumerate(getattr(self, 'q_buttons', [])):
            try:
                button.blockSignals(True)
                button.setChecked(position == index)
            finally:
                try:
                    button.blockSignals(False)
                except Exception:
                    pass

    def _apply_quality_state(self, status):
        """按 /api/status 回读刷新：档位高亮 / AUTO 高亮 / 状态行"""
        self._board_qsel = _qsel_int(status.get('qsel'))
        self._board_auto = _as_bool01(status.get('auto'))
        self._board_target_kbps = _as_int(status.get('target_kbps'))
        # 没有旋档计划在跑时，规划用的估算值直接跟随回读值；
        # 旋档期间只更新"回读值"（核对用），估算值由连发计数自己推进。
        if self._q_plan is None:
            self._qsel_estimate = self._board_qsel
        self._refresh_quality_ui_from_board()
        try:
            self.lbl_quality.setText(format_quality_line(status))
        except Exception:
            traceback.print_exc()

    @_slot
    def _on_quality_pick(self, index):
        """点 Q30/Q50/Q75/Q90：把 FPGA 旋到该档（按需连发 Q，再按板卡回读核对）"""
        if self.serial_worker is None:
            self._notify('串口未连接：压缩质量档位只能走串口（Q/A/B）')
            self._refresh_quality_ui_from_board()      # 高亮拉回真实值
            return
        target = _qsel_int(index)
        if target is None:
            return
        self._q_plan = {'target': target, 'attempt': 1, 'phase': 'send',
                        'remaining': None, 'sent': 0, 'deadline': time.time() + 8.0}
        self._refresh_quality_ui_from_board()          # 等回读再变高亮
        self._notify('正在切到 %s…（连发 Q 后按板卡回读核对）' % QSEL_NAMES[target])
        self._on_quality_tick()

    @_slot
    def _on_quality_auto(self):
        """'A' -> 切到自适应模式（高亮等 auto 字段回读）"""
        if self.serial_worker is None:
            self._notify('串口未连接：自适应开关只能走串口（Q/A/B）')
            self._refresh_quality_ui_from_board()
            return
        self._q_plan = None
        try:
            self.timer_q.stop()
        except Exception:
            pass
        if self._send_command(auto_command(), '压缩质量：切到自适应 (A)'):
            self._refresh_quality_ui_from_board()
            self._notify("已发送 'A'：切到自适应模式（约 1 秒后按板卡回读刷新高亮）")

    @_slot
    def _on_quality_target(self):
        """'B' -> 目标码率循环下一档（显示等 target_kbps 回读）"""
        if self.serial_worker is None:
            self._notify('串口未连接：目标码率只能走串口（Q/A/B）')
            return
        if self._send_command(bitrate_command(), '目标码率：切下一档 (B)'):
            self._notify("已发送 'B'：目标码率循环到下一档（预设 %s KB/s，每档 %d KB/s，"
                         "疑似 -> %d KB/s，以板卡回读为准）"
                         % ("/".join(str(k) for k in BITRATE_PRESETS_KBPS),
                            TARGET_STEP_KBPS,
                            next_bitrate_preset(self._board_target_kbps)))

    @_slot
    def _on_quality_tick(self):
        """
        旋档状态机（全程非阻塞）：
            send   阶段：按"目标-当前"算出的次数连发 'Q'（间隔 250ms）
            verify 阶段：等一轮 /api/status 回读，qsel == 目标则收工；
                        不一致就再算一遍补发，最多 QUALITY_MAX_ATTEMPTS 轮
        """
        plan = self._q_plan
        if plan is None:
            return
        if time.time() > plan['deadline']:
            self._finish_quality_plan(False, '超时')
            return
        if plan['phase'] == 'send':
            if plan['remaining'] is None:
                presses = plan_quality_presses(self._qsel_estimate, plan['target'])
                plan['remaining'] = 1 if presses is None else presses
            if plan['remaining'] <= 0:
                plan['phase'] = 'verify'
                self.timer_q.start(QUALITY_VERIFY_DELAY_MS)
                return
            if not self._send_command(quality_command(), '压缩质量：切下一档 (Q)'):
                self._q_plan = None
                self._notify('串口命令没发出去，切档取消')
                return
            plan['remaining'] -= 1
            plan['sent'] += 1
            if self._qsel_estimate is not None:
                self._qsel_estimate = (self._qsel_estimate + 1) % len(QSEL_NAMES)
            if plan['remaining'] > 0:
                self.timer_q.start(QUALITY_PRESS_GAP_MS)
            else:
                plan['phase'] = 'verify'
                self.timer_q.start(QUALITY_VERIFY_DELAY_MS)
            return
        # ---- verify：拿板卡回读的 qsel 和目标比对 ----
        if self._board_qsel == plan['target']:
            self._finish_quality_plan(True)
            return
        if plan['attempt'] >= QUALITY_MAX_ATTEMPTS:
            self._finish_quality_plan(False, '回读 qsel=%s' % (self._board_qsel,))
            return
        plan['attempt'] += 1
        presses = plan_quality_presses(self._qsel_estimate, plan['target'])
        plan['remaining'] = 1 if presses is None else presses
        plan['phase'] = 'send'
        if plan['remaining'] == 0:
            # 估算已经到了，但回读不一致（可能有人手动改过）：再等一轮
            plan['phase'] = 'verify'
            self.timer_q.start(QUALITY_VERIFY_DELAY_MS)
            return
        self._on_quality_tick()

    def _finish_quality_plan(self, ok, reason=''):
        plan = self._q_plan or {}
        target = plan.get('target')
        sent = plan.get('sent', 0)
        self._q_plan = None
        try:
            self.timer_q.stop()
        except Exception:
            pass
        self._qsel_estimate = self._board_qsel
        self._refresh_quality_ui_from_board()
        if ok:
            self._notify('已切到 %s（板卡回读确认，共发 %d 次 Q）' % (qsel_name(target), sent),
                         6000)
        else:
            self._notify('切档未确认：目标 %s，板卡回读 qsel=%s（%s）。请确认串口通、'
                         'FPGA 位流是 p17、且队列没被手动改动'
                         % (qsel_name(target), self._board_qsel,
                            reason or '多次重试仍不一致'), 9000)

    # ------------------------------------------- 板卡功能开关 (HTTP /api/config)
    def _refresh_switch_buttons(self):
        """
        三个开关（自动截图 / 目标检测 / 尺寸测量）的可用性与文字统一在这里算：
          可用 = 板卡在线 且 对应回读字段存在 且 没有"没加载模型/没测量模块"这类硬限制
          文字 = 只写回读值（自动截图: 开/关/-- …），点击本身不改文字
        注意回读来源不同：autosave/detect 在 `/api/status`；**dim 在 `/api/config`**
        （15 字段契约不许加 dim，所以尺寸测量的回读走 config 的 _config_seen）。
        """
        status = getattr(self, '_last_status', None) or {}
        config = getattr(self, '_config_seen', None) or {}
        online = bool(self._online and status)
        reason_auto = switch_blocked_reason(status, SWITCH_AUTOSAVE) if status else ''
        reason_detect = switch_blocked_reason(status, SWITCH_DETECT) if status else ''
        if self._switch_warning:
            reason_detect = '板卡没加载模型（%s）' % self._switch_warning
        reason_dim = ''
        if config:
            reason_dim = dim_blocked_reason(config)
            if self._dim_warning:
                reason_dim = '板卡没有尺寸测量模块（%s）' % self._dim_warning
        else:
            reason_dim = '还没读到 /api/config（dim 字段未知）'
        pending = self._switch_pending or {}
        try:
            self.btn_autosave.setText(format_switch_text(SWITCH_AUTOSAVE, self._board_autosave))
            self.btn_detect.setText(format_switch_text(SWITCH_DETECT, self._board_detect))
            self.btn_dim.setText(format_switch_text(SWITCH_DIM, self._board_dim))
            self.btn_autosave.setEnabled(online and not reason_auto
                                         and SWITCH_AUTOSAVE not in pending)
            self.btn_detect.setEnabled(online and not reason_detect
                                       and SWITCH_DETECT not in pending)
            self.btn_dim.setEnabled(online and not reason_dim
                                    and SWITCH_DIM not in pending)
        except Exception:
            traceback.print_exc()
        # 「重新标定」的可用性跟着 dim 回读走（t26）：dim=关 -> 置灰
        self._refresh_dim_calib_ui()
        notes = []
        if not online:
            notes.append('板卡未连接')
        if reason_auto:
            notes.append(reason_auto)
        if reason_detect:
            notes.append(reason_detect)
        if reason_dim:
            notes.append(reason_dim)
        if pending:
            notes.append('正在等板卡回读确认…')
        # (t15) 原来这下面有一段常驻的长帮助文字（关自动截图≠关手动截图…），用户要求删掉。
        # 语义挪到**悬停提示**：基础说明 + 当前为什么点不了（未连接/没加载模型/等回读）。
        base_tip = ('关自动截图 ≠ 关手动截图（📷 仍可用）；关 YOLO 后不画框、帧率上升。'
                    '按钮文字只认板卡回读（约 1 秒）。')
        tip = base_tip + ('（%s）' % '；'.join(notes) if notes else '')
        try:
            self.grp_switch.setToolTip(tip)
            self.btn_autosave.setToolTip(
                '自动截图：关掉后检出不再自动存图，但 📷 手动截图仍然可用。' + tip)
            self.btn_detect.setToolTip(
                '目标检测：关掉后板卡不跑 YOLO、画面上的框消失、帧率上升。' + tip)
            self.btn_dim.setToolTip(
                '尺寸测量：与「目标检测」互斥（点开会自动关掉检测，反之亦然）；'
                '开了以后下方表格显示测量结果。' + tip)
        except Exception:
            traceback.print_exc()

    def _update_fps_big(self, fps):
        """状态栏上的醒目帧率：检测关掉后加"(检测已关)"并变绿，演示对比用"""
        detect_off = (self._board_detect == 0)
        text = 'FPGA 帧率 %.1f fps' % float(fps or 0.0)
        if detect_off:
            text += '  (检测已关)'
        try:
            self.lbl_fps_big.setText(text)
            self.lbl_fps_big.setStyleSheet('font-weight:bold; color:#1a7f37;'
                                           if detect_off else 'font-weight:bold;')
        except Exception:
            traceback.print_exc()

    def _apply_switch_state(self, status):
        """按 /api/status 回读刷新 autosave/detect（dim 的回读走 /api/config）"""
        previous_detect = self._board_detect
        self._board_autosave = _as_bool01(status.get('autosave'))
        self._board_detect = _as_bool01(status.get('detect'))
        warning = str(status.get('warning') or '')
        if warning:
            self._switch_warning = warning
        # 等回读确认的请求：回读值到位就清掉 pending
        # 注意 dim 的 pending 由 /api/config 的回读清（见 _apply_config_state）
        pending = self._switch_pending or {}
        for name, target in list(pending.items()):
            if name == SWITCH_DIM:
                continue
            current = (self._board_autosave if name == SWITCH_AUTOSAVE
                       else self._board_detect)
            if current == target:
                self._switch_pending = None
                self._notify('板卡已确认：%s' % format_switch_text(name, current), 6000)
                break
        self._refresh_switch_buttons()
        fps = float(status.get('fps') or 0.0)
        if self._board_detect == 0 and previous_detect == 1:
            self._append_log('%s [系统] YOLO 检测已关闭 → 画面不再画框，帧率会上升（当前 %.1f fps）'
                             % (time.strftime('%H:%M:%S'), fps))
        elif self._board_detect == 1 and previous_detect == 0:
            self._append_log('%s [系统] YOLO 检测已开启（当前 %.1f fps）'
                             % (time.strftime('%H:%M:%S'), fps))
        self._update_fps_big(fps)

    def _apply_config_state(self, result):
        """
        按 `/api/config` 的回读刷新 **dim**（尺寸测量）这一路：

        · dim 字段只出现在 /api/config（/api/status 必须保持 15 字段）——所以尺寸测量的
          "文字只认回读"就是认这里的回读，不是认点击；
        · 互斥：板卡侧 dim=1 自动 detect=0，回读里 detect 会是 0；这里只记录，
          真正把按钮文字改掉仍由 /api/status 的 autosave/detect 回读负责（各自认各自的口径）。
        """
        result = result or {}
        self._config_seen = result
        self._board_dim = _as_bool01(result.get('dim'))
        warning = str(result.get('warning') or '')
        if warning:
            if _NO_DIM_MODULE_WARNING in warning.lower():
                self._dim_warning = warning
                print('[CONFIG] 板卡 dim warning: %s' % warning)
            else:
                self._switch_warning = warning
        pending = self._switch_pending or {}
        if pending.get(SWITCH_DIM) is not None and self._board_dim == pending[SWITCH_DIM]:
            self._switch_pending = None
            self._notify('板卡已确认：%s' % format_switch_text(SWITCH_DIM, self._board_dim), 6000)
        self._refresh_switch_buttons()

    @_slot
    def _on_toggle_autosave(self):
        self._send_switch(SWITCH_AUTOSAVE)

    @_slot
    def _on_toggle_detect(self):
        self._send_switch(SWITCH_DETECT)

    @_slot
    def _on_toggle_dim(self):
        self._send_switch(SWITCH_DIM)

    def _send_switch(self, name):
        """
        点开关 -> `GET /api/config?<name>=<取反>`（尺寸测量/目标检测按互斥一次带齐两个参数）。
        **不在这里改按钮文字**：等下一次 /api/status（或 dim 走 /api/config）回读才更新显示。
        """
        label = SWITCH_NAMES.get(name, name)
        if not self._online:
            self._notify('板卡未连接，无法切换%s（先点"应用并重连"）' % label)
            return
        if name == SWITCH_DIM:
            current = self._board_dim
            blocked = dim_blocked_reason(self._config_seen or {})
        elif name == SWITCH_DETECT:
            current = self._board_detect
            blocked = ''
        else:
            current = self._board_autosave
            blocked = ''
        if _as_bool01(current) is None:
            hint = blocked or ('板卡版本过旧：没有 %s 字段' % name)
            self._notify('无法切换%s：%s' % (label, hint), 8000)
            return
        if self.config_worker is not None and self.config_worker.isRunning():
            self._notify('上一个开关请求还没返回，请稍候…')
            return
        target = next_switch_value(current)
        if target is None:
            return
        params = exclusive_params(name, target)
        self._switch_pending = {name: target}
        self._refresh_switch_buttons()
        try:
            self.config_worker = ConfigWorker(self.board_base, **params)
        except Exception as exc:
            print('[ERR] 构造 /api/config 请求失败: %s' % exc)
            self._switch_pending = None
            self._refresh_switch_buttons()
            self._notify('开关请求构造失败: %s' % exc, 8000)
            return
        self.config_worker.configReady.connect(self._on_config_ready)
        self.config_worker.configError.connect(self._on_config_error)
        self.config_worker.start()
        extra = ''
        if name in (SWITCH_DIM, SWITCH_DETECT) and len(params) > 1:
            extra = '（互斥：%s）' % '&'.join('%s=%d' % kv for kv in sorted(params.items()))
        self._notify('已请求 %s → %s%s（等板卡回读确认）'
                     % (label, '开' if target else '关', extra))

    @_slot
    def _on_config_ready(self, result):
        self.config_worker = None
        ok = bool(result.get('ok'))
        warning = result.get('warning') or ''
        if warning:
            if _NO_DIM_MODULE_WARNING in warning.lower():
                self._dim_warning = warning
            else:
                self._switch_warning = warning
            print('[CONFIG] 板卡 warning: %s' % warning)
        # 先按这次响应刷新 dim 的回读与置灰（config 的响应本身就带 dim）
        self._apply_config_state(result)
        if not ok:
            self._switch_pending = None
            message = '开关设置失败: %s' % (result.get('error') or '板卡拒绝')
            if result.get('http_status'):
                message += ' (HTTP %s)' % result['http_status']
            print('[CONFIG] %s' % message)
            self._set_net_state(message, False)
            self._notify(message, 8000)
            self._refresh_switch_buttons()
            return
        if result.get('changed'):
            print('[CONFIG] 板卡已改变: %s' % ', '.join(result['changed']))
        # 板卡"接受了请求但没应用"（典型：detect=1 但没加载模型）——别让按钮一直等
        pending = self._switch_pending or {}
        for name, target in list(pending.items()):
            actual = result.get(name)
            if actual is None or actual != target:
                self._switch_pending = None
                note = warning or ('板卡当前 %s' % format_switch_text(name, actual))
                self._notify('板卡没有应用 %s → %s（%s）'
                             % (SWITCH_NAMES.get(name, name), '开' if target else '关', note),
                             9000)
                break
        self._refresh_switch_buttons()
        # 注意：这里不刷新 autosave/detect 的按钮文字，等 /api/status 回读
        # （需求要求"只认回读"）；dim 的口径是 /api/config，已在上面刷过。

    @_slot
    def _on_config_error(self, message):
        self.config_worker = None
        self._switch_pending = None
        print('[CONFIG] %s' % message)
        self._set_net_state(message, False)
        self._notify(message, 8000)
        self._refresh_switch_buttons()

    @_slot
    def _on_config_probe(self, result):
        """连上板卡时的一次性 /api/config 查询：拿 warning（没加载模型/dim 模块）与 dim 回读"""
        self.config_probe = None
        warning = result.get('warning') or ''
        if warning:
            if _NO_DIM_MODULE_WARNING in warning.lower():
                self._dim_warning = warning
            else:
                self._switch_warning = warning
            print('[CONFIG] 板卡 warning: %s' % warning)
        self._apply_config_state(result)

    @_slot
    def _on_net_ready(self, net):
        """连上板卡时的一次性 /api/net 查询：给二维码挑"手机能连到的板卡 IP" """
        self.net_worker = None
        self._apply_net_state(net)
        addrs = [it.get('addr') for it in (net or {}).get('ifs') or []]
        print('[NET] /api/net 网卡: %s' % (', '.join(a for a in addrs if a) or '(空)'))

    @_slot
    def _on_net_probe_error(self, message):
        """/api/net 读取失败（连不上/坏 JSON）：退化成"用界面上的板卡 IP"，不打扰用户"""
        self.net_worker = None
        print('[NET] %s' % message)
        self._apply_net_state({'ifs': [], 'supported': None})

    # ------------------------------------------- 尺寸测量结果表 (/api/dim)
    def _apply_dim_state(self, dim):
        """
        按 `GET /api/dim` 回读刷新测量结果表（唯一改表格内容的地方）。

        红线（照板卡契约，界面不改口径）：
          · calib_ok=false -> 板卡一个 value_mm 都不给：表格整表置灰 + 红字原因
          · tol_mm 是**判定线**，精度看 sigma_mm —— 摘要行只写精度，不把 tol 当精度
          · 旧板卡（404/supported=False）-> 表格清空 + 摘要行"板卡版本过旧"
        """
        self._dim_state = dim or {}
        # 标定请求已经回来过 -> 这一次回读就是"点击后的第一次回读"，可以解除置灰
        if getattr(self, '_dim_calib_pending', False) and \
                getattr(self, '_dim_calib_req_done', False):
            self._dim_calib_pending = False
            self._dim_calib_req_done = False
        self._refresh_dim_table()
        self._refresh_dim_calib_ui()

    def _refresh_dim_calib_ui(self):
        """
        「重新标定」按钮 + 结果行的可用性/文字（**只写 /api/dim 回读值**，t26）：

          · dim=关（或还不知道 dim）-> 按钮置灰（标定只能在尺寸测量模式下做）
          · 点完 pending 期间 -> 置灰（等下一次 /api/dim 回读再放开，不做乐观更新）
          · 结果行：'标定: OK  σ=0.42mm' / '标定: 失败(原因)'（红）/ '标定: 标定中…' / '标定: --'
          · 旧固件缺 calib_ok 字段 -> '标定: --'，按钮照样可点、不报错
        """
        dim = getattr(self, '_dim_state', None) or {}
        blocked = dim_calib_blocked_reason(dim, getattr(self, '_board_dim', None))
        pending = bool(getattr(self, '_dim_calib_pending', False))
        text, level = format_dim_calib_line(dim)
        try:
            self.btn_dim_calib.setEnabled(bool(self._online) and not blocked and not pending)
            self.lbl_dim_calib.setText(text)
            color = dim_calib_color(level)
            self.lbl_dim_calib.setStyleSheet(('color:%s;' % color) if color else '')
            self.btn_dim_calib.setToolTip(
                '让板卡用最新一帧重新标定一次（GET /api/dim?calib=1，后台线程，不卡画面）；'
                + ('当前不可点：%s' % blocked if blocked else '点完等 /api/dim 回读才更新结果'))
        except Exception:
            traceback.print_exc()

    def _refresh_dim_table(self):
        """把 self._dim_state 画进表格 + 摘要 + 红字原因（不改任何回读值）"""
        dim = getattr(self, '_dim_state', None) or {}
        rows = format_dim_rows(dim)
        note = dim_table_note(dim)
        calib_ok = _as_bool01(dim.get('calib_ok'))
        # 标定不可信 / 旧板卡 -> 整表置灰（表还在，只是灰的，一眼看出"数不可用"）
        grey = bool(dim) and (calib_ok == 0 or dim.get('supported') is False)
        try:
            self.lbl_dim.setText(format_dim_summary(dim))
            self.tbl_dim.setRowCount(len(rows))
            for r, row in enumerate(rows):
                for c, text in enumerate(row):
                    self.tbl_dim.setItem(r, c, QTableWidgetItem(text))
            self.lbl_dim_note.setText(note)
            self.lbl_dim_note.setVisible(bool(note))
            self.tbl_dim.setEnabled(not grey)
            self.btn_dim_run.setEnabled(
                bool(self._online) and dim_run_blocked_reason(dim) == '')
            self.grp_dim.setToolTip('尺寸测量结果：%s%s'
                                    % (format_dim_summary(dim),
                                       ('（%s）' % note) if note else ''))
        except Exception:
            traceback.print_exc()

    @_slot
    def _on_dim_state(self, dim):
        """/api/dim 的 2 s 轮询结果（只读缓存）"""
        self._apply_dim_state(dim)

    @_slot
    def _on_dim_ready(self, dim):
        """/api/dim?run=1（「测量一次」）的结果"""
        self.dim_run_worker = None
        self._apply_dim_state(dim)

    @_slot
    def _on_dim_error(self, message):
        """/api/dim 读取失败（连不上/坏 JSON）：只提示，不清掉已有表格"""
        self.dim_run_worker = None
        print('[DIM] %s' % message)
        try:
            self.lbl_dim_note.setText(message)
            self.lbl_dim_note.setVisible(True)
        except Exception:
            traceback.print_exc()

    @_slot
    def _on_dim_run(self):
        """点「测量一次」-> GET /api/dim?run=1（板卡用最新帧立刻量一次，约 0.3 s）"""
        dim = getattr(self, '_dim_state', None) or {}
        blocked = dim_run_blocked_reason(dim)
        if blocked:
            self._notify('无法测量：%s' % blocked, 8000)
            return
        if not self._online:
            self._notify('板卡未连接，无法测量')
            return
        if self.dim_run_worker is not None and self.dim_run_worker.isRunning():
            self._notify('上一次测量还没返回，请稍候…')
            return
        try:
            self.dim_run_worker = DimRunWorker(self.board_base)
        except Exception as exc:
            self._notify('测量请求构造失败: %s' % exc, 8000)
            return
        self.dim_run_worker.dimReady.connect(self._on_dim_ready)
        self.dim_run_worker.dimError.connect(self._on_dim_error)
        self.dim_run_worker.start()
        self._notify('已请求板卡测一次（GET /api/dim?run=1）')

    def _on_outline_run(self):
        if self.outline_worker is not None and self.outline_worker.isRunning():
            return
        self.btn_outline.setEnabled(False)
        self.lbl_outline.setText('正在读取当前帧并拟合板框…')
        worker = OutlineWorker(self.board_base, self.cmb_outline_profile.currentData(), self)
        self.outline_worker = worker
        worker.ready.connect(self._on_outline_ready)
        worker.failed.connect(self._on_outline_failed)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(self._outline_finished)
        worker.start()

    def _outline_finished(self):
        self.outline_worker = None
        self.btn_outline.setEnabled(True)

    def _on_outline_failed(self, message):
        self.lbl_outline.setText(message)
        self._notify(message, 10000)

    def _on_outline_ready(self, result, png):
        text = '板框长 %.2f mm × 宽 %.2f mm（初测）' % (result['long_mm'], result['short_mm'])
        self.lbl_outline.setText(text)
        dialog = QDialog(self)
        dialog.setWindowTitle('板框测量定格图 · ' + text)
        dialog.resize(1100, 720)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(text + '\n绿色：板框拟合线；黄色：采样边点。此图为点击时的定格图。', dialog))
        view = AspectLabel(parent=dialog)
        view.set_image(QImage.fromData(png))
        layout.addWidget(view, 1)
        close = QPushButton('关闭，返回实时画面', dialog)
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        self._outline_dialogs.append(dialog)
        dialog.finished.connect(lambda _code: self._outline_dialogs.remove(dialog))
        dialog.finished.connect(dialog.deleteLater)
        dialog.show()

    @_slot
    def _on_dim_calib(self):
        """
        点「重新标定」-> GET /api/dim?calib=1（板卡用最新帧**后台**重标一次）。

        ⚠ 为什么不是 /api/config?calib=1：板卡 `_config()` 只认 autosave/detect/dim/
          dim_auto*，未知参数会被忽略（等于只查一次），真触发重标的端点是
          `/api/dim?calib=1`（t18 契约，也就是 url_dim() 的既有语义）。

        点完立刻置灰、**不改按钮文字**；结果行只认随后的 /api/dim 回读（calib_ok/note/sigma）。
        不做自动连续标定、不做超时重试。
        """
        dim = getattr(self, '_dim_state', None) or {}
        blocked = dim_calib_blocked_reason(dim, getattr(self, '_board_dim', None))
        if blocked:
            self._notify('无法重新标定：%s' % blocked, 8000)
            return
        if not self._online:
            self._notify('板卡未连接，无法重新标定')
            return
        if self.dim_calib_worker is not None and self.dim_calib_worker.isRunning():
            self._notify('上一次标定请求还没返回，请稍候…')
            return
        try:
            self.dim_calib_worker = DimCalibWorker(self.board_base)
        except Exception as exc:
            self._notify('标定请求构造失败: %s' % exc, 8000)
            return
        self._dim_calib_pending = True
        self._dim_calib_req_done = False
        self._refresh_dim_calib_ui()           # 立刻置灰（等回读）
        self.dim_calib_worker.dimReady.connect(self._on_dim_calib_ready)
        self.dim_calib_worker.dimError.connect(self._on_dim_calib_error)
        self.dim_calib_worker.start()
        self._notify('已请求板卡重新标定（GET /api/dim?calib=1，后台线程，不卡画面）')

    @_slot
    def _on_dim_calib_ready(self, dim):
        """标定请求的即时响应：通常 state=calib / recalib_queued=1，结果要等轮询回读"""
        self.dim_calib_worker = None
        self._dim_calib_req_done = True
        dim = dim or {}
        if _as_int(dim.get('recalib_queued')) == 0 and str(dim.get('state') or '') != 'calib':
            # 板卡没排队（正在标定 / 没帧 / 模块不可用）：别让按钮一直灰着
            self._dim_calib_pending = False
            self._dim_calib_req_done = False
            self._notify('板卡没有排队标定（可能正在标定/没有帧），看结果行为准', 9000)
        self._apply_dim_state(dim)

    @_slot
    def _on_dim_calib_error(self, message):
        """标定请求失败（连不上/坏 JSON）：解除置灰 + 写结果行，不弹模态框"""
        self.dim_calib_worker = None
        self._dim_calib_pending = False
        self._dim_calib_req_done = False
        print('[DIM] %s' % message)
        try:
            self.lbl_dim_calib.setText('标定: 请求失败')
            self.lbl_dim_calib.setStyleSheet('color:%s;' % DIM_CALIB_FAIL_COLOR)
        except Exception:
            traceback.print_exc()
        self._notify(message, 8000)
        self._refresh_dim_calib_ui()

    # ------------------------------------------- 远程推送 (/api/push) + 手机监看
    def _board_host_port(self):
        """当前板卡的 (host, port) —— 从 self.board_base 里拆（换过地址也准）"""
        host_port = str(self.board_base).split('://', 1)[-1]
        host, _sep, port = host_port.rpartition(':')
        if not host:
            return DEFAULT_BOARD_IP, DEFAULT_BOARD_PORT
        try:
            port_no = int(port)
        except (TypeError, ValueError):
            port_no = DEFAULT_BOARD_PORT
        return host, port_no

    def _refresh_push_buttons(self):
        """
        推送组「开 / 关」两个按钮的可用性与当前态（与 /api/config 开关同一套规矩）：
          可用 = 板卡在线 且 /api/push 支持 且 没有请求在等回读
          态   = 只写 /api/push 回读值（点亮/粗体表示"板卡现在就是开/关"），点击本身不变态
        旧板卡（404）-> 整组置灰、文案 '--'、点击只提示"板卡版本过旧（没有 /api/push）"。
        """
        state = self._push_state or {}
        blocked = push_blocked_reason(state)
        online = bool(self._online)
        pending = bool(self._push_pending)
        usable = online and bool(self._push_supported) and not blocked and not pending
        view = push_toggle_state(state)
        try:
            self.btn_push_on.setEnabled(usable)
            self.btn_push_off.setEnabled(usable)
            # 当前态只用回读点亮：开=绿粗体、关=灰粗体；未知则两个都不亮
            self.btn_push_on.setStyleSheet(
                'font-weight:bold; color:#1a7f37;' if view['on_checked'] else '')
            self.btn_push_off.setStyleSheet(
                'font-weight:bold; color:#666666;' if view['off_checked'] else '')
            self.btn_push_test.setEnabled(usable)
            self.btn_push_url.setEnabled(usable)
            self.edit_push_url.setEnabled(usable)
        except Exception:
            traceback.print_exc()
        notes = []
        if not online:
            notes.append('板卡未连接')
        if blocked:
            notes.append(blocked)
        if pending:
            notes.append('正在等板卡回读确认…')
        base_hint = ('板卡把缺陷图 POST 到 PC 接收端（tools/recv_upload.py）。'
                     '开关/URL 都是"发请求 → 等 /api/push 回读"才改字。')
        if notes:
            base_hint += '（%s）' % '；'.join(notes)
        # 一次性的反馈（400 bad url / 请求失败 / 测试图未排队）留在这行末尾，
        # 否则每秒一次的 _refresh_push_buttons() 会把它立刻盖掉。
        extra = str(getattr(self, '_push_hint_msg', '') or '').strip()
        if extra:
            base_hint += ' ｜ %s' % extra
        try:
            self.lbl_push_hint.setText(base_hint)
        except Exception:
            traceback.print_exc()

    def _set_push_hint(self, message):
        """写一次性反馈（会被下一次 _refresh_push_buttons 保留在行尾）"""
        self._push_hint_msg = str(message or '').strip()
        self._refresh_push_buttons()

    def _refresh_push_url_suggestion(self):
        """
        目标 URL 的建议值跟着**板卡地址**走，而不是"本机第一个网卡 IP"。

        只在输入框还是"上一次自动建议值"（或空）时才覆盖，用户手改过就不动他的输入，
        免得每秒刷新把正在编辑的地址覆盖掉。
        """
        board_ip, board_port = self._board_host_port()
        suggested = push_url_for_board(board_ip, board_port)
        previous = getattr(self, '_push_url_auto', '')
        try:
            current = str(self.edit_push_url.text() or '').strip()
            if not current or current == previous:
                self.edit_push_url.setText(suggested)
        except Exception:
            traceback.print_exc()
        self._push_url_auto = suggested

    def _apply_push_state(self, state):
        """按 GET /api/push 回读刷新推送组（唯一改按钮态/状态行的地方）"""
        state = state or {}
        self._push_state = state
        supported = state.get('supported')
        if supported is None:
            if self._push_supported is None:
                self._push_supported = True     # 老板卡脚本没这个字段时按"支持"处理，别误灰
        else:
            self._push_supported = bool(supported)
        pending = self._push_pending or {}
        if pending and 'enabled' in pending:
            if _as_bool01(state.get('enabled')) == pending['enabled']:
                self._push_pending = None
                self._push_pending_at = 0.0
                self._notify('板卡已确认: %s' % format_push_switch_text(state), 6000)
            elif push_pending_expired(getattr(self, '_push_pending_at', 0.0),
                                      time.time()):
                # [D29] 板卡没确认（最常见：目标 URL 为空被拒）-> 解除等待，别让按钮永久置灰
                self._push_pending = None
                self._push_pending_at = 0.0
                self._notify('板卡未确认推送状态（目标 URL 是否为空？先在「目标 URL」'
                             '填 PC 接收端地址并点「应用」），已解除等待', 9000)
        try:
            self.lbl_push.setText(format_push_line(state))
            self.lbl_push_err.setText(format_push_error_line(state))
        except Exception:
            traceback.print_exc()
        try:
            self._refresh_push_url_suggestion()   # 建议值跟着板卡地址/路由走
        except Exception:
            traceback.print_exc()
        self._refresh_push_buttons()

    # ------------------------------------------- 手机监看地址 + 二维码 (/api/net)
    def _mobile_targets(self):
        """
        二维码/明文地址可选的候选：[(下拉显示文字, ip)]，**无线优先**。

        来源顺序：① `GET /api/net` 的 ifs（板卡真实网卡）；② 没有 ifs（旧板卡/枚举为空）
        -> 退化成"用界面上的板卡 IP"，下拉显示 '--'（不报错）。
        """
        net = self._net_state or {}
        targets = []
        for item in (net.get('ifs') or []):
            addr = str((item or {}).get('addr') or '').strip()
            if not addr:
                continue
            mark = '无线' if _as_bool01((item or {}).get('wireless')) == 1 else '有线'
            targets.append(('%s (%s)' % (addr, mark), addr))
        if not targets:
            board_ip, _port = self._board_host_port()
            targets.append(('--', board_ip))
        return targets

    def _selected_mobile_ip(self):
        """下拉里选中的板卡 IP（没得选就用界面上的板卡 IP）"""
        try:
            data = self.cbo_mobile.currentData()
        except Exception:
            data = None
        if data:
            return str(data)
        board_ip, _port = self._board_host_port()
        return board_ip

    def _mobile_url_now(self):
        """当前二维码/明文地址：`http://<选中的板卡 IP>:<板卡端口>/m`"""
        _board_ip, board_port = self._board_host_port()
        return 'http://%s:%d/%s' % (self._selected_mobile_ip(), board_port,
                                    MOBILE_PAGE_PATH)

    @_slot
    def _on_mobile_addr_changed(self, _index=None):
        """下拉换地址 -> 重画二维码与明文地址"""
        self._refresh_mobile_line()

    def _apply_net_state(self, net):
        """收下 /api/net 的结果：重建下拉（默认无线优先）+ 刷新二维码/警告"""
        self._net_state = net or {}
        targets = self._mobile_targets()
        pick = 0
        addr, wireless, warn = choose_mobile_addr(self._net_state)
        for index, (_label, ip) in enumerate(targets):
            if addr and ip == addr:
                pick = index
                break
        try:
            self.cbo_mobile.blockSignals(True)
            self.cbo_mobile.clear()
            for label, ip in targets:
                self.cbo_mobile.addItem(label, ip)     # 显示文字 + 真实 IP（currentData 取它）
            self.cbo_mobile.setCurrentIndex(pick if targets else -1)
            self.cbo_mobile.blockSignals(False)
        except Exception:
            traceback.print_exc()
        # 没有无线 IP（或 /api/net 不可用）-> 橙色警告；有无线就不警告
        if self._net_state.get('supported') is False:
            text = '旧版板卡没有 /api/net：二维码用界面上的板卡 IP'
        elif (self._net_state.get('ifs') and not wireless):
            text = warn or QR_NO_WIRELESS_WARN
        else:
            text = ''
        try:
            self.lbl_net_warn.setText(text)
        except Exception:
            traceback.print_exc()
        self._refresh_mobile_line()

    def _refresh_mobile_line(self):
        """
        刷新「手机看: http://<板卡IP>:端口/m」明文 + 120x120 二维码（点开 400x400 大图）。

        segno/qrcode 都是**可选**依赖：缺库就只显示占位文字与明文地址，
        「复制」照常可用，绝不崩、绝不阻塞。
        """
        url = self._mobile_url_now()
        self._mobile_url = url
        png = qr_png_bytes(url)                 # 缺库/画图失败 -> None
        pixmap = None
        if png:
            try:
                candidate = QPixmap()
                if candidate.loadFromData(png) and not candidate.isNull():
                    pixmap = candidate
            except Exception:
                pixmap = None
        try:
            self.lbl_mobile.setText(format_mobile_url_line(url))
            if pixmap is None:
                # 不隐藏、不塌陷：同一个 120x120 位置显示占位文字，布局高度不变
                self.lbl_qr.clear()
                self.lbl_qr.setText(QR_UNAVAILABLE_HINT)
                self._qr_ok = False
            else:
                self.lbl_qr.setText('')
                self.lbl_qr.setPixmap(pixmap.scaled(QSize(MOBILE_QR_SIZE, MOBILE_QR_SIZE),
                                                   Qt.KeepAspectRatio,
                                                   Qt.SmoothTransformation))
                self._qr_ok = True
            self.lbl_qr.setVisible(True)
        except Exception:
            traceback.print_exc()
            self._qr_ok = False
        print('[PUSH] 手机监看地址 %s（二维码: %s）'
              % (url, '已显示 120x120' if self._qr_ok else QR_UNAVAILABLE_HINT))

    @_slot
    def _on_qr_click(self, _event=None):
        """点二维码 -> 弹 400x400 大图（屏幕远一点也能扫）"""
        if not self._qr_ok:
            self._notify('%s；请手动输入 %s' % (QR_UNAVAILABLE_HINT, self._mobile_url), 9000)
            return
        png = qr_png_bytes(self._mobile_url, scale=10, border=4)
        if not png:
            self._notify('二维码大图生成失败，请手动输入 %s' % self._mobile_url, 8000)
            return
        pixmap = QPixmap()
        try:
            if not pixmap.loadFromData(png) or pixmap.isNull():
                raise ValueError('loadFromData failed')
        except Exception:
            self._notify('二维码大图生成失败，请手动输入 %s' % self._mobile_url, 8000)
            return
        try:
            dialog = QDialog(self)
            dialog.setWindowTitle('手机扫码打开板卡 /m（%s）' % self._mobile_url)
            box = QVBoxLayout(dialog)
            big = QLabel(dialog)
            big.setAlignment(Qt.AlignCenter)
            big.setPixmap(pixmap.scaled(QSize(MOBILE_QR_BIG_SIZE, MOBILE_QR_BIG_SIZE),
                                        Qt.KeepAspectRatio, Qt.SmoothTransformation))
            box.addWidget(big)
            tip = QLabel(self._mobile_url, dialog)
            tip.setAlignment(Qt.AlignCenter)
            tip.setWordWrap(True)
            box.addWidget(tip)
            dialog.setModal(True)
            self._qr_dialog = dialog           # 保持引用，别被 GC 掉
            dialog.show()
        except Exception:
            traceback.print_exc()

    @_slot
    def _on_push_enable(self):
        """点「开」-> POST ?enabled=1（态等 /api/push 回读）"""
        self._send_push_enabled(1)

    @_slot
    def _on_push_disable(self):
        """点「关」-> POST ?enabled=0（态等 /api/push 回读）"""
        self._send_push_enabled(0)

    def _send_push_enabled(self, target):
        state = self._push_state or {}
        if push_blocked_reason(state) or not self._push_supported:
            self._notify(PUSH_OLD_BOARD_HINT + '，无法开关推送', 8000)
            return False
        if not self._online:
            self._notify('板卡未连接，无法开关推送（先点"应用并重连"）')
            return False
        if _as_bool01(state.get('enabled')) is None:
            self._notify('还不知道板卡当前的推送状态（等 /api/push 回读）', 6000)
            return False
        # [D29] 开推送前先看有没有目标 URL：没有就直说怎么修，
        #   而不是发一个会被板卡 400 拒掉的请求（旧板卡甚至是静默 no-op）。
        #   注意判据要宽松：回读可能滞后 1 秒，只要**输入框里是合法地址**就放行，
        #   真没设上时板卡会返回 400（新板）或 pending 超时（老板），用户都能看到原因。
        if target:
            cur_url = str(state.get('url') or '').strip()
            entry = str(self.edit_push_url.text() or '').strip()
            if not cur_url and push_url_issue(entry):
                self._push_hint_msg = ('板卡上还没有目标 URL —— 先在「目标 URL」填 '
                                       'http://<PC_IP>:9000/upload 并点「应用」，再开推送')
                self._refresh_push_buttons()
                self._notify(self._push_hint_msg, 9000)
                return False
        if self.push_worker is not None and self.push_worker.isRunning():
            self._notify('上一个推送请求还没返回，请稍候…')
            return False
        self._push_pending = {'enabled': target}
        self._push_pending_at = time.time()   # [D29] 等回读的起始时间（超时就解除置灰）
        self._refresh_push_buttons()          # 立刻置灰，等回读
        return self._send_push(enabled=target,
                               note='推送 → %s' % ('开' if target else '关'))

    @_slot
    def _on_push_apply_url(self):
        """点「应用」-> POST ?url=<输入框内容>；非法 URL 的 400 文案照板卡的 error 显示"""
        state = self._push_state or {}
        if push_blocked_reason(state) or not self._push_supported:
            self._notify(PUSH_OLD_BOARD_HINT + '，无法设置目标 URL', 8000)
            return
        if not self._online:
            self._notify('板卡未连接，无法设置目标 URL')
            return
        url = str(self.edit_push_url.text() or '').strip()
        issue = push_url_issue(url)
        if issue:
            # [D29] 拦住"把手机页 /m 当推送目标"这类明显错误，别让板卡一直 fail
            self._push_hint_msg = issue
            self._refresh_push_buttons()
            self._notify(issue, 9000)
            return
        if '/upload' not in url:
            self._push_hint_msg = ('提示：目标路径不是 /upload —— 只有当你的接收端'
                                   '换了路径时才这样；tools/recv_upload.py 默认是 /upload')
        self._send_push(url=url, note='目标 URL → %s' % url)

    @_slot
    def _on_push_test(self):
        """点「发送测试」-> POST ?test=1（板卡立刻推一张测试图）"""
        state = self._push_state or {}
        if push_blocked_reason(state) or not self._push_supported:
            self._notify(PUSH_OLD_BOARD_HINT + '，无法发送测试', 8000)
            return
        if not self._online:
            self._notify('板卡未连接，无法发送测试')
            return
        self._send_push(test=1, note='发送测试图')

    def _send_push(self, enabled=None, url=None, test=None, note=''):
        """把一次 /api/push 设置请求丢给一次性线程（不阻塞 UI，不在这里改文字）"""
        if self.push_worker is not None and self.push_worker.isRunning():
            self._notify('上一个推送请求还没返回，请稍候…')
            return False
        try:
            self.push_worker = PushWorker(self.board_base, enabled=enabled,
                                           url=url, test=test)
        except Exception as exc:
            print('[ERR] 构造 /api/push 请求失败: %s' % exc)
            self._push_pending = None
            self._refresh_push_buttons()
            self._notify('推送请求构造失败: %s' % exc, 8000)
            return False
        self.push_worker.pushReady.connect(self._on_push_ready)
        self.push_worker.pushError.connect(self._on_push_error)
        self.push_worker.start()
        if note:
            self._notify('已请求 %s（等板卡 /api/push 回读确认）' % note)
        return True

    @_slot
    def _on_push_ready(self, result):
        """一次设置请求的响应：成功就提示"等回读"，400/其它错误把 error 文案写在提示行"""
        self.push_worker = None
        if not result.get('ok'):
            self._push_pending = None
            message = '%s%s' % (result.get('error') or '板卡拒绝',
                                ' (HTTP %s)' % result['http_status']
                                if result.get('http_status') else '')
            self._push_hint_msg = '推送设置失败: %s' % message
            print('[PUSH] 设置失败: %s' % message)
            self._notify('推送设置失败: %s' % message, 9000)
            self._refresh_push_buttons()
            return
        changed = result.get('changed') or []
        if changed:
            print('[PUSH] 板卡已改变: %s' % ', '.join(changed))
        if result.get('test_queued') == 0:
            note = result.get('warning') or '板卡没排队（推送可能没开/没帧）'
            self._push_hint_msg = '测试图未排队: %s' % note
            self._notify('测试图未排队: %s' % note, 9000)
        elif result.get('test_queued'):
            self._notify('板卡已把测试图排进推送队列（看 PC 接收端有没有收到）', 8000)
        # 请求成功也**不**在这里改按钮文字：等下一次 /api/push 回读（≤1 秒）
        self._refresh_push_buttons()

    @_slot
    def _on_push_error(self, message):
        """/api/push 请求失败（连不上/格式不对）：只提示，不改板卡在线状态"""
        self.push_worker = None
        self._push_pending = None
        print('[PUSH] %s' % message)
        self._push_hint_msg = message
        self._notify(message, 8000)
        self._refresh_push_buttons()

    @_slot
    def _on_push_state(self, state):
        """轮询线程送来的 /api/push 快照（1 秒一次）"""
        self._apply_push_state(state)

    @_slot
    def _on_push_poll_error(self, message):
        """轮询失败（多半是板卡离线）：只写提示行，别把整个板卡标成离线"""
        print('[PUSH] %s' % message)
        self._push_hint_msg = message
        self._refresh_push_buttons()

    @_slot
    def _on_copy_mobile(self):
        """「复制」-> 手机监看地址进剪贴板（拿不到就提示，不崩）"""
        url = self._mobile_url or url_mobile(self.board_base)
        try:
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(url)
                self._notify('已复制手机监看地址: %s' % url, 6000)
                return
        except Exception:
            traceback.print_exc()
        self._notify('复制失败，请手动输入: %s' % url, 9000)

    # ------------------------------------------------------------- 网络部分
    @_slot
    def _on_apply_board(self):
        base = self._read_board_base()
        if base is None:
            return
        changed = (base != self.board_base)
        self.board_base = base
        self._update_board_label()
        # 换了板卡：/api/net 与推送目标 URL 的建议值都要重来（路由探测依赖板卡地址）
        self._net_state = None
        self._refresh_push_url_suggestion()
        self._apply_net_state({'ifs': [], 'supported': None})
        self._stop_net_workers()
        if changed:
            # 换了板卡：图墙/事件游标属于"另一台设备"，全部重来
            self._reset_wall()
            self._event_cursor = EventCursor()
            self._manual_announced.clear()
            self._append_log('%s [系统] 板卡地址切换为 %s'
                             % (time.strftime('%H:%M:%S'), base))
        self._set_net_state('板卡: %s (连接中…)' % base, True)
        self._set_online(False)
        self._start_net_workers()
        self._notify('板卡地址已切换: %s' % base)

    @_slot
    def _on_disconnect_preview(self):
        self._stop_net_workers()
        self._set_online(False)
        self._set_net_state('板卡: %s (已断开)' % self.board_base, False)
        self._notify('已断开预览、状态、事件与图墙轮询')

    def _retire(self, worker):
        """让线程自己退出（不阻塞 UI），但保留引用防止运行中被回收"""
        if worker is None:
            return
        try:
            worker.stop()
        except Exception:
            traceback.print_exc()
        self._retired.append(worker)

    def _stop_net_workers(self):
        """把网络线程统一停掉并置空（重连/切换地址/退出时都走这里）"""
        for attr in ('mjpeg_worker', 'status_worker', 'events_worker',
                     'defect_poller', 'thumb_worker', 'snap_worker',
                     'config_worker', 'config_probe', 'net_worker',
                     'dim_poller', 'dim_run_worker', 'dim_calib_worker',
                     'push_poller', 'push_worker'):
            worker = getattr(self, attr, None)
            if worker is not None:
                self._retire(worker)
                setattr(self, attr, None)
        self._switch_pending = None
        self._push_pending = None

    def _start_net_workers(self):
        """
        启动 5 个常驻网络线程（互不影响，任何一个失败其它照常）：
          预览 MJPEG / 状态 1s / 事件 1s / 图墙 2s / 缩略图队列
        另外发**一次** `/api/config` 纯查询（不改任何开关），只为提前拿到
        "no model loaded" / "dim module not loaded" 这类 warning 与 **dim 回读**
        （尺寸测量的文字只认它），好把对应开关置灰；
        再发**一次** `/api/net` 拿板卡各网卡 IP（给二维码挑手机能连到的地址）；
        再加一个 1 秒一次的 `/api/push` 轮询（推送开关/计数只认它的回读；
        旧板卡 404 也只是把推送组置灰，不会把整个板卡标离线）；
        以及一个 2 秒一次的 `/api/dim` 轮询（尺寸测量结果表，只读缓存）。
        """
        try:
            self.mjpeg_worker = MjpegWorker(url_stream(self.board_base))
            self.mjpeg_worker.imageReady.connect(self._on_preview_image)
            self.mjpeg_worker.frameBytes.connect(self._on_frame_bytes)
            self.mjpeg_worker.netOK.connect(self._on_stream_ok)
            self.mjpeg_worker.netError.connect(self._on_net_error)
            self.mjpeg_worker.start()

            self.status_worker = StatusWorker(self.board_base)
            self.status_worker.statusReady.connect(self._on_status)
            self.status_worker.statusError.connect(self._on_net_error)
            self.status_worker.start()

            # 事件日志：从上次的 seq 继续（断线重连不重复、不丢事件）
            self.events_worker = EventsWorker(self.board_base,
                                              since=self._event_cursor.since())
            self.events_worker.batchReady.connect(self._on_events)
            self.events_worker.eventsError.connect(self._on_net_error)
            self.events_worker.start()

            self.defect_poller = DefectPoller(self.board_base)
            self.defect_poller.listReady.connect(self._on_defect_list)
            self.defect_poller.error.connect(self._on_net_error)
            self.defect_poller.start()

            self.thumb_worker = ThumbWorker(self.board_base)
            self.thumb_worker.thumbReady.connect(self._on_defect_thumb)
            self.thumb_worker.start()

            self._switch_warning = ''
            self._retire(self.config_probe)
            self.config_probe = ConfigWorker(self.board_base)      # 只查询，不改开关
            self.config_probe.configReady.connect(self._on_config_probe)
            self.config_probe.configError.connect(self._on_net_error)
            self.config_probe.start()

            # 网卡列表（板卡 /api/net）：给二维码挑"手机能连到的地址"，连上都问一次
            self._net_state = None
            self._retire(self.net_worker)
            self.net_worker = NetWorker(self.board_base)
            self.net_worker.netReady.connect(self._on_net_ready)
            self.net_worker.netError.connect(self._on_net_probe_error)
            self.net_worker.start()

            # 尺寸测量结果：2 秒一次 GET /api/dim（只读缓存；旧板卡 404 只把表格清空）
            self._dim_state = None
            self._board_dim = None
            self._config_seen = None
            self._dim_warning = ''
            self._retire(self.dim_poller)
            self.dim_poller = DimPoller(self.board_base)
            self.dim_poller.dimReady.connect(self._on_dim_state)
            self.dim_poller.dimError.connect(self._on_dim_error)
            self.dim_poller.start()
            self._refresh_dim_table()

            # 远程推送状态：1 秒一次 GET /api/push（只读快照）
            self._push_state = None
            self._push_supported = None
            self._push_pending = None
            self._retire(self.push_poller)
            self.push_poller = PushPoller(self.board_base)
            self.push_poller.stateReady.connect(self._on_push_state)
            self.push_poller.pushError.connect(self._on_push_poll_error)
            self.push_poller.start()
            self._refresh_push_buttons()

            self._thumb_requested.clear()
            print('[NET] 已启动预览/状态/事件/图墙/推送线程: %s' % self.board_base)
        except Exception as exc:
            print('[ERR] 启动网络线程失败: %s' % exc)
            traceback.print_exc()
            self._set_net_state('启动网络线程失败: %s' % exc, False)

    @_slot
    def _on_stream_ok(self, url):
        self._set_net_state('板卡: %s (预览已连接)' % self.board_base, True)
        self._set_online(True)
        self._last_net_error = ''
        self._notify('预览流已连接: %s' % url)

    @_slot
    def _on_net_error(self, message):
        # 同一个错误只打印一次，避免每秒刷屏；状态栏始终显示最新错误
        if message != self._last_net_error:
            print('[ERR] %s' % message)
            self._last_net_error = message
            # 真正连不上时把日志面板也标一行，方便事后回看
            self._append_log('%s [错误] %s' % (time.strftime('%H:%M:%S'), message))
        self._set_net_state(message, False)
        self._set_online(False)
        self.statusBar().showMessage(message, 6000)

    @_slot
    def _on_preview_image(self, image):
        if image is None:
            return
        try:
            if image.isNull():
                return
        except Exception:
            pass
        self._preview_image = image
        self._fps_frames += 1
        try:
            self.lbl_video.set_image(image)
        except Exception:
            traceback.print_exc()

    @_slot
    def _on_frame_bytes(self, nbytes):
        # 单帧 JPEG 大小可作为"链路是否真的在动"的旁证，放在状态栏提示里意义不大，
        # 这里只记到窗口标题下方的状态显示（低频，不刷屏）
        self._last_frame_bytes = int(nbytes)

    @_slot
    def _on_fps_tick(self):
        now = time.time()
        frames = self._fps_frames
        span = now - self._fps_stamp
        self._fps_frames = 0
        self._fps_stamp = now
        fps = (frames / span) if span > 0.1 else 0.0
        self.lbl_preview_fps.setText('预览帧率: %.1f fps' % fps)

    @_slot
    def _on_status(self, status):
        ratio_text = status.get('ratio_pct', '--')
        # 先落存"最新状态"：后面的 _refresh_switch_buttons() 要读它判断字段是否齐全，
        # 顺序放晚了会用上一轮的旧状态（真渲染时踩过：旧板卡已经缺字段了按钮却还是可点）。
        self._last_status = status
        self.lbl_ratio.setText('压缩率: %s' % ratio_text)
        self.lbl_kbps.setText('码率: %.1f KB/s' % float(status.get('kbps') or 0.0))
        self.lbl_fpga_fps.setText('FPGA 帧率: %.1f fps' % float(status.get('fps') or 0.0))
        self.lbl_sd.setText(format_sd_text(status.get('sd_free'), status.get('save_dir')))
        self._apply_cover_line(status)             # 覆盖率 / 字节块（偏低时橙色提醒）
        self._apply_quality_state(status)          # 档位/AUTO/目标码率 全按回读刷新
        self._apply_switch_state(status)           # 自动截图/目标检测 也全按回读刷新
        self._set_online(True)
        if status.get('ratio') is None:
            self._set_net_state('板卡: %s (在线, 暂无压缩数据)' % self.board_base, True)
        else:
            self._set_net_state('板卡: %s (压缩率 %s)' % (self.board_base, ratio_text), True)
        self._last_net_error = ''

    def _apply_cover_line(self, status):
        """
        刷新「覆盖率 / 字节每块」那一行（t4 新增，t6 改口径，t8 分母改一帧满块数）。

        cover = 本窗口编码块数 / **一帧满块数 32400**（即"这个窗口采到整幅图多少面积"）：
        抽样明显偏少（< COVER_LOW_THRESHOLD=25%，对应修前的 10%；t2 轮转后约 31% 不报警）时
        整行转橙色，并已带 "(跨帧约 13 帧覆盖整幅)" 后缀 —— 说明满覆盖靠 RTL 条带轮转保证，
        不暗示"越低越糟"。⚠ 旧位流(p18)重复编码会读 40%（虚高），也不报警。
        这只是一个事实提示：不弹窗、不写日志、不碰网络/串口。
        cover 缺失（旧板卡 13 字段）时 format_cover_line 会给 '-- (旧版板卡无此字段)'，且不标橙。
        """
        line, low = format_cover_line(status)
        try:
            self.lbl_cover.setText(line)
            self.lbl_cover.setStyleSheet(('color:%s;' % COVER_WARN_COLOR) if low else '')
        except Exception:
            traceback.print_exc()

    # --------------------------------------------------------- 事件日志面板
    @_slot
    def _on_events(self, batch):
        """把一批 /api/events 交给游标去重排序，然后把新事件逐行追加到日志面板"""
        new_events = self._event_cursor.ingest(batch)
        if not new_events:
            self.lbl_events.setText('事件: %d 条 (seq %d)'
                                    % (self._log_lines, self._event_cursor.since()))
            return
        for event in new_events:
            if event['type'] == EVENT_TYPE_MANUAL and self._event_is_announced(event['file']):
                continue                                  # 截图成功的提示已经打过
            self._append_log(format_event_line(event))
        print('[EVENTS] 新增 %d 条事件, next seq=%d'
              % (len(new_events), self._event_cursor.since()))
        self._request_wall_refresh()

    # ------------------------------------------------- 「📷 截图存 SD」按钮
    @_slot
    def _on_snap(self):
        """让板卡把当前帧存进 SD 卡；成功后立刻刷新图墙并在日志里留一行"""
        if not self._online:
            self._notify('板卡未连接，无法截图（先点"应用并重连"）')
            return
        if self.snap_worker is not None and self.snap_worker.isRunning():
            self._notify('上一次截图还没返回，请稍候…')
            return
        try:
            self.btn_snap.setEnabled(False)
        except Exception:
            pass
        self.snap_worker = SnapWorker(self.board_base)
        self.snap_worker.snapReady.connect(self._on_snap_ready)
        self.snap_worker.snapError.connect(self._on_snap_error)
        self.snap_worker.start()
        self._notify('已请求板卡截图（存到板卡 SD）…')

    @_slot
    def _on_snap_ready(self, result):
        self.snap_worker = None
        try:
            self.btn_snap.setEnabled(self._online)
        except Exception:
            pass
        if not result.get('ok'):
            message = '截图失败: %s%s' % (
                result.get('error') or '板卡无帧',
                ' (HTTP %s)' % result['http_status'] if result.get('http_status') else '')
            print('[SNAP] %s' % message)
            self._set_net_state(message, False)
            self._notify(message, 8000)
            return
        name = result.get('file') or '?'
        size = result.get('size') or 0
        line = '%s [手动] 已存到板卡 SD: %s (%s)' % (
            time.strftime('%H:%M:%S'), name, human_size(size))
        self._append_log(line)
        self._mark_announced(name)                        # events 里同一条不再重复打印
        print('[SNAP] %s' % line)
        self._notify('截图已存到板卡 SD: %s (%s)' % (name, human_size(size)), 8000)
        self._set_net_state('板卡: %s (已截图 %s)' % (self.board_base, name), True)
        self._set_online(True)
        if result.get('dir'):
            self.lbl_sd.setText(format_sd_text(self._last_sd_free(), result['dir']))
        self._request_wall_refresh()                      # 截完立刻刷新图墙

    @_slot
    def _on_snap_error(self, message):
        self.snap_worker = None
        try:
            self.btn_snap.setEnabled(self._online)
        except Exception:
            pass
        print('[SNAP] %s' % message)
        self._set_net_state(message, False)
        self._notify(message, 8000)

    def _last_sd_free(self):
        status = getattr(self, '_last_status', None) or {}
        return status.get('sd_free')

    # ----------------------------------------------------------- 缺陷图墙
    @_slot
    def _reset_wall(self):
        """清空图墙与缩略图缓存（换板卡时调用）"""
        try:
            self.list_defects.clear()
        except Exception:
            traceback.print_exc()
        self._defect_items = {}
        self._thumb_requested = set()
        self._thumb_done = set()
        self._thumb_attempts = {}
        self.lbl_defect_count.setText('缺陷图: --')

    def _request_wall_refresh(self):
        """让图墙线程立刻拉一次列表（截图/新事件后马上可见）"""
        poller = self.defect_poller
        if poller is None:
            return
        try:
            poller.poll_now()
        except Exception:
            traceback.print_exc()

    def _enqueue_thumb(self, name):
        """给新文件排队做缩略图（同一个名字不重复排队，失败最多重试 1 次）"""
        if not name or name in self._thumb_done or name in self._thumb_requested:
            return
        if self._thumb_attempts.get(name, 0) >= 2:
            return
        worker = self.thumb_worker
        if worker is None:
            return
        self._thumb_requested.add(name)
        self._thumb_attempts[name] = self._thumb_attempts.get(name, 0) + 1
        try:
            worker.enqueue(name)
        except Exception:
            traceback.print_exc()

    @_slot
    def _on_refresh_defects(self):
        if self.defect_poller is None:
            self._notify('板卡未连接：先"应用并重连"再刷新列表')
            return
        self._request_wall_refresh()
        self._notify('正在刷新缺陷列表…')

    @_slot
    def _on_defect_list(self, items):
        """图墙增量合并：按文件名去重，只有新文件才入墙 + 排队缩略图"""
        added = []
        for item in items:
            name = item['name']
            if name in self._defect_items:
                continue
            list_item = QListWidgetItem('%s\n%s' % (name, human_size(item['size'])))
            list_item.setData(Qt.UserRole, name)
            list_item.setSizeHint(QSize(THUMB_W + 16, THUMB_H + 40))
            list_item.setToolTip('%s\n%s\n%r' % (name, human_size(item['size']),
                                                 item.get('mtime')))
            self.list_defects.addItem(list_item)
            self._defect_items[name] = list_item
            added.append(name)
        if added:
            preview = ', '.join(added[:4]) + (' …' if len(added) > 4 else '')
            self._append_log('%s [图墙] 新增 %d 张: %s'
                             % (time.strftime('%H:%M:%S'), len(added), preview))
            print('[DEFECT] 新增 %d 张缺陷图' % len(added))
        for name in self._defect_items:
            if name not in self._thumb_done:
                self._enqueue_thumb(name)
        self.lbl_defect_count.setText('缺陷图: %d 张' % len(self._defect_items))
        if not self._defect_items:
            self._notify('缺陷列表为空：板卡 SD 里还没有保存的缺陷照片')

    @_slot
    def _on_defect_thumb(self, payload):
        name = payload.get('name')
        self._thumb_requested.discard(name)
        list_item = self._defect_items.get(name)
        if list_item is None:
            return
        data = payload.get('thumb')
        if not data:
            if name not in self._thumb_fail_logged:
                self._thumb_fail_logged.add(name)
                print('[WARN] 缩略图失败 %s: %s' % (name, payload.get('error', '')))
            list_item.setToolTip('%s\n缩略图失败: %s' % (name, payload.get('error', '')))
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            print('[WARN] 缩略图 QPixmap 解码失败: %s' % name)
            return
        list_item.setIcon(QIcon(pixmap))
        self._thumb_done.add(name)

    @_slot
    def _on_download(self):
        if self.download_worker is not None and self.download_worker.isRunning():
            self._notify('上一批下载还没结束，请稍候…')
            return
        selected = self.list_defects.selectedItems()
        names = []
        for list_item in selected:
            name = list_item.data(Qt.UserRole)
            if name:
                names.append(name)
        if not names:
            self._notify('请先在缺陷图墙里选中要下载的图片')
            return
        self.download_worker = DownloadWorker(self.board_base, self.download_dir, names)
        self.download_worker.saved.connect(self._on_download_saved)
        self.download_worker.failed.connect(self._on_net_error)
        self.download_worker.finishedAll.connect(self._on_download_done)
        self.download_worker.start()
        self._notify('开始下载 %d 张缺陷图到 %s' % (len(names), self.download_dir))

    @_slot
    def _on_download_saved(self, path):
        print('[DOWNLOAD] 已保存 %s' % path)
        self._notify('已保存: %s' % path)

    @_slot
    def _on_download_done(self, count):
        self._notify('下载完成：%d 张，目录 %s' % (count, self.download_dir), 8000)

    @_slot
    def _on_about(self):
        self._notify('PCB 缺陷检测上位机 · FPGA 5 模式串口切换 + MJPEG 预览 + 缺陷图墙 '
                     '(板卡 %s)' % self.board_base, 8000)

    # --------------------------------------------------------------- 收尾
    def closeEvent(self, event):
        print('[EXIT] 正在关闭线程…')
        if self.outline_worker is not None:
            self.outline_worker.stop()
            self.outline_worker.wait()
        try:
            for worker in (self.serial_worker, self.mjpeg_worker, self.status_worker,
                           self.events_worker, self.defect_poller, self.thumb_worker,
                           self.snap_worker, self.download_worker,
                           self.config_worker, self.config_probe, self.net_worker,
                           self.dim_poller, self.dim_run_worker, self.dim_calib_worker,
                           self.push_poller, self.push_worker):
                self._retire(worker)
            self.serial_worker = None
            self.mjpeg_worker = None
            self.status_worker = None
            self.events_worker = None
            self.defect_poller = None
            self.thumb_worker = None
            self.snap_worker = None
            self.download_worker = None
            self.config_worker = None
            self.config_probe = None
            self.net_worker = None
            self.dim_poller = None
            self.dim_run_worker = None
            self.dim_calib_worker = None
            self.push_poller = None
            self.push_worker = None
            try:
                self.timer_fps.stop()
            except Exception:
                pass
            try:
                self.timer_q.stop()                 # 别让旋档定时器在关窗后再触发
            except Exception:
                pass
            self._q_plan = None
            for worker in list(self._retired):
                try:
                    if worker.isRunning():
                        worker.wait(3000)
                except Exception:
                    traceback.print_exc()
            self._retired = []
        except Exception:
            traceback.print_exc()
        print('[EXIT] 已退出')
        event.accept()


# ============================================================================
# 10. 自检（--selftest）：不需要 PyQt5、不需要板卡、不需要串口硬件
# ============================================================================

# 一段"假 JPEG"：只有 SOI/EOI 和中间的假数据，足够验证解析器切帧逻辑
FAKE_JPEG = b'\xff\xd8' + b'FAKE-JPEG-PAYLOAD-0123456789' + b'\xff\xd9'


def _multipart_part(payload, boundary=BOUNDARY):
    """按板卡契约拼一段 multipart（解析器不依赖头部，但真实流一定长这样）"""
    return (b'--' + boundary + b'\r\n'
            b'Content-Type: image/jpeg\r\n'
            b'Content-Length: ' + str(len(payload)).encode() + b'\r\n'
            b'\r\n' + payload + b'\r\n')


def _make_test_jpeg(width=320, height=240):
    """用 pillow 造一张真 JPEG（没有 pillow 时返回 None，自检会跳过缩略图项）"""
    try:
        from PIL import Image
    except Exception:
        return None
    image = Image.new('RGB', (width, height), (30, 60, 200))
    buffer = io.BytesIO()
    image.save(buffer, 'JPEG', quality=80)
    return buffer.getvalue()


def _test_serial_commands():
    """串口命令映射：5 种算法 + 阈值 ±，必须与 pre_ctrl.v 逐字符一致"""
    expected = {0: b'0', 1: b'1', 2: b'2', 3: b'3', 4: b'4'}
    for index, payload in expected.items():
        got = mode_command(index)
        if got != payload:
            raise AssertionError('模式 %d 应发 %r, 实际 %r' % (index, payload, got))
    for bad in (-1, 5, 'x', None):
        try:
            mode_command(bad)
        except SerialCommandError:
            continue
        raise AssertionError('模式号 %r 应该被拒绝' % (bad,))
    if thresh_command(1) != b'+' or thresh_command(-1) != b'-':
        raise AssertionError('阈值命令应为 + / -')
    try:
        thresh_command(0)
    except SerialCommandError:
        pass
    else:
        raise AssertionError('阈值步进 0 应该被拒绝')
    return '5 模式 0-4 + 阈值 +/- 共 7 条命令逐个核对通过'


def _test_serial_reply_parse():
    """FPGA 回传解析：PRE-OK / MODE=n / TH=nnn / F=nn"""
    cases = [
        ('PRE-OK', ('banner', None)),
        ('PRE-OK\r', ('banner', None)),
        ('MODE=3', ('mode', 3)),
        ('MODE=0\r', ('mode', 0)),
        ('TH=200', ('thresh', 200)),
        ('TH=8', ('thresh', 8)),
        ('TH=2040', ('thresh', 2040)),
        ('F=14', ('fps', 14)),
        ('hello', ('unknown', 'hello')),
        ('MODE=z', ('unknown', 'MODE=z')),
        ('', ('empty', None)),
    ]
    for line, expected in cases:
        got = parse_serial_line(line)
        if got != expected:
            raise AssertionError('解析 %r 应为 %r, 实际 %r' % (line, expected, got))
    return '%d 种回传格式全部解析正确' % len(cases)


def _test_url_build():
    """URL 构造：默认值、各种写法归一化、缺陷文件名编码"""
    if build_base() != 'http://192.168.10.50:8000':
        raise AssertionError('默认地址不对: %r' % build_base())
    variants = [
        ('192.168.10.50', 8000, 'http://192.168.10.50:8000'),
        ('http://192.168.10.50:8000/', None, 'http://192.168.10.50:8000'),
        ('192.168.10.50:9000', None, 'http://192.168.10.50:9000'),
        (' 10.0.0.2 ', '8080', 'http://10.0.0.2:8080'),
    ]
    for ip, port, expected in variants:
        got = build_base(ip, port)
        if got != expected:
            raise AssertionError('build_base(%r, %r) = %r, 期望 %r'
                                 % (ip, port, got, expected))
    base = 'http://192.168.10.50:8000'
    expect_paths = {
        url_status(base): 'http://192.168.10.50:8000/api/status',
        url_snapshot(base): 'http://192.168.10.50:8000/snap.jpg',
        url_stream(base): 'http://192.168.10.50:8000/stream.mjpg',
        url_defects(base): 'http://192.168.10.50:8000/api/defects',
        url_snap(base): 'http://192.168.10.50:8000/api/snap',
        url_events(base, 0): 'http://192.168.10.50:8000/api/events?since=0',
        url_events(base, 7): 'http://192.168.10.50:8000/api/events?since=7',
        url_events(base, -5): 'http://192.168.10.50:8000/api/events?since=0',
    }
    for got, expected in expect_paths.items():
        if got != expected:
            raise AssertionError('URL 拼错: %r != %r' % (got, expected))
    got = url_defect_file(base, '中文 缺陷.jpg')
    expected = base + '/defects/%E4%B8%AD%E6%96%87%20%E7%BC%BA%E9%99%B7.jpg'
    if got != expected:
        raise AssertionError('缺陷图 URL 编码错: %r != %r' % (got, expected))
    if not url_defect_file(base, '../etc/passwd').endswith('/defects/passwd'):
        raise AssertionError('缺陷图 URL 未做 basename 防护')
    for bad in ('', '   ', 'http://'):
        try:
            build_base(bad)
        except ValueError:
            continue
        raise AssertionError('非法地址 %r 应该被拒绝' % (bad,))
    return ('4 种地址写法 + status/snap/stream/defects/snap/events 路径 + '
            'events?since 边界(-5→0) + 缺陷文件名编码/防护 全部正确')


def _test_status_json():
    """/api/status JSON 解析（含 ratio 为 null、百分数容错、坏 JSON）"""
    good = (b'{"ok":true,"ratio":0.856,"kbps":10.8,"qsel":2,"fps":14.0,'
            b'"blocks":1440,"nbytes":13200}')
    status = parse_status_json(good)
    if abs(status['ratio'] - 0.856) > 1e-9:
        raise AssertionError('ratio 解析错: %r' % status['ratio'])
    if status['ratio_pct'] != '85.6%':
        raise AssertionError('ratio_pct 应为 85.6%%, 实际 %r' % status['ratio_pct'])
    if abs(status['kbps'] - 10.8) > 1e-9 or abs(status['fps'] - 14.0) > 1e-9:
        raise AssertionError('kbps/fps 解析错: %r' % status)
    if status['qsel'] != 2 or status['blocks'] != 1440 or status['nbytes'] != 13200:
        raise AssertionError('整数字段解析错: %r' % status)
    null_ratio = parse_status_json('{"ok":true,"ratio":null}')
    if null_ratio['ratio'] is not None or null_ratio['ratio_pct'] != '--':
        raise AssertionError('ratio=null 时应该显示 --: %r' % null_ratio)
    pct = parse_status_json('{"ratio":85.6,"kbps":"3.5"}')
    if abs(pct['ratio'] - 0.856) > 1e-9:
        raise AssertionError('百分数容错失效: %r' % pct['ratio'])
    if abs(pct['kbps'] - 3.5) > 1e-9:
        raise AssertionError('字符串数字容错失效: %r' % pct['kbps'])
    for bad in (b'', b'not json', b'[1,2,3]'):
        try:
            parse_status_json(bad)
        except ValueError:
            continue
        raise AssertionError('坏 JSON %r 应该抛 ValueError' % bad)
    return '分子/分母字段、ratio=null、百分数容错、坏 JSON 全部符合预期'


def _test_defects_json():
    """/api/defects JSON 解析 + 文件名安全化"""
    good = (b'{"files":[{"name":"det_142233.jpg","size":123456,"mtime":1712345678},'
            b'{"name":"../evil.jpg","size":1},{"size":9}]}')
    items = parse_defects_json(good)
    if len(items) != 2:
        raise AssertionError('应解析出 2 条有效条目, 实际 %r' % items)
    if items[0] != {'name': 'det_142233.jpg', 'size': 123456, 'mtime': 1712345678}:
        raise AssertionError('首条解析错: %r' % items[0])
    if items[1]['name'] != 'evil.jpg':
        raise AssertionError('路径穿越名应被压成 basename, 实际 %r' % items[1]['name'])
    if parse_defects_json('{"files":[]}') != []:
        raise AssertionError('空列表应返回 []')
    if [i['name'] for i in parse_defects_json('["a.jpg","b.jpg"]')] != ['a.jpg', 'b.jpg']:
        raise AssertionError('纯数组/纯字符串的容错失效')
    try:
        parse_defects_json('{"files":"oops"}')
    except ValueError:
        pass
    else:
        raise AssertionError('files 不是数组时应抛 ValueError')
    try:
        parse_defects_json('xxx')
    except ValueError:
        pass
    else:
        raise AssertionError('坏 JSON 应抛 ValueError')
    return '契约格式 + 路径穿越 + 空列表 + 容错格式全部符合预期'


def _test_mjpeg_parser():
    """MJPEG 多段流解析：任意切分都能凑齐整帧，半帧不输出"""
    frames_in = [FAKE_JPEG, FAKE_JPEG[:-2] + b'X' + b'\xff\xd9', FAKE_JPEG]
    stream = b'HTTP-HEADER-GARBAGE\r\n'
    for payload in frames_in:
        stream += _multipart_part(payload)
    stream += b'--' + BOUNDARY + b'--\r\n'

    # (1) 一次性喂完
    parser = MjpegParser()
    frames = parser.feed(stream)
    if len(frames) != 3:
        raise AssertionError('应解析出 3 帧, 实际 %d' % len(frames))
    for index, frame in enumerate(frames):
        if not frame.startswith(b'\xff\xd8') or not frame.endswith(b'\xff\xd9'):
            raise AssertionError('第 %d 帧不是完整 JPEG: %r…' % (index, frame[:8]))
        if frame != frames_in[index]:
            raise AssertionError('第 %d 帧内容不符' % index)

    # (2) 逐字节喂（最恶劣的切分，模拟 TCP 分片）
    parser = MjpegParser()
    got = []
    for i in range(0, len(stream)):
        got.extend(parser.feed(stream[i:i + 1]))
    if len(got) != 3 or got[1] != frames_in[1]:
        raise AssertionError('逐字节喂入结果不对: %d 帧' % len(got))

    # (3) 半帧：只给 SOI 部分 -> 一帧都不许输出；补齐后才输出
    parser = MjpegParser()
    half = FAKE_JPEG[:8]
    if parser.feed(half) != []:
        raise AssertionError('不完整帧不应输出')
    if parser.incomplete != 1:
        raise AssertionError('应标记 incomplete=1')
    rest = parser.feed(FAKE_JPEG[8:])
    if len(rest) != 1 or rest[0] != FAKE_JPEG:
        raise AssertionError('补齐后应输出 1 帧完整 JPEG')

    # (4) 垃圾数据不会长成毒缓存的形态
    parser = MjpegParser(max_buffer=1024)
    parser.feed(b'\x00' * 64)
    parser.feed(b'\xff\xd8' + b'\x11' * 4096)
    if len(parser.buf) > 1024:
        raise AssertionError('缓冲上限失效, buf=%d' % len(parser.buf))
    return '一次性/逐字节/半帧/缓冲上限 4 种情形均正确'


def _test_serial_worker_loop():
    """
    串口收发线程逻辑：用一个假的 pyserial 模块喂数据，验证
      - 命令按入队顺序原样写出（'3' / '+'）
      - 回传被按 '\\n' 正确切行（数据被切成 4 字节碎片也能拼回来）
      - 收到 PRE-OK/MODE/TH/F 四类消息、退出时关端口
    不碰真串口硬件，所以本机也能跑。
    """
    import types

    worker = SerialWorker('COM_SELFTEST')

    class _Recorder(object):
        """替代 pyqtSignal，把 emit 的参数记下来（selftest 里不需要真信号）"""
        def __init__(self):
            self.items = []

        def emit(self, *args):
            self.items.append(args[0] if len(args) == 1 else args)

    class FakeSerial(object):
        last = None

        def __init__(self, *args, **kwargs):
            FakeSerial.last = self
            self.port = args[0] if args else ''
            self.written = []
            self.buf = bytearray(b'PRE-OK\r\nMODE=3\r\nTH=2040\r\nF=14\r\n')
            self.closed = False

        def write(self, payload):
            self.written.append(bytes(payload))
            return len(payload)

        def flush(self):
            pass

        def read(self, size):
            if self.buf:                    # 每次只给 4 字节：故意把行切开
                chunk = bytes(self.buf[:4])
                del self.buf[:4]
                return chunk
            worker._stop.set()              # 读空了 -> 相当于用户关窗，让线程自然退出
            return b''

        def close(self):
            self.closed = True

    fake_module = types.ModuleType('serial')
    fake_module.Serial = FakeSerial
    real_module = sys.modules.get('serial')
    lines, opened, failed = _Recorder(), _Recorder(), _Recorder()
    worker.lineReceived = lines
    worker.opened = opened
    worker.failed = failed
    sys.modules['serial'] = fake_module
    try:
        worker.send(mode_command(3))
        worker.send(thresh_command(1))
        worker.run()                        # 直接在测试线程里跑，行为确定、不涉并发
    finally:
        if real_module is not None:
            sys.modules['serial'] = real_module
        else:
            sys.modules.pop('serial', None)

    if failed.items:
        raise AssertionError('串口线程不应报错: %r' % (failed.items,))
    if lines.items != ['PRE-OK', 'MODE=3', 'TH=2040', 'F=14']:
        raise AssertionError('回传切行不对: %r' % (lines.items,))
    if not opened.items or opened.items[0][0] is not True:
        raise AssertionError('未上报"串口已打开": %r' % (opened.items,))
    fake = FakeSerial.last
    if fake is None or fake.written != [b'3', b'+']:
        raise AssertionError('写出的命令不对: %r'
                             % (None if fake is None else fake.written))
    if not fake.closed:
        raise AssertionError('线程退出时没有关闭串口')
    return '命令按序写出 + 4 字节碎片回传拼行 + 退出关端口，全部正确'


def _test_thumbnail_and_paths():
    """缩略图 160x90（不变形）与下载路径（建目录、不覆盖）"""
    import tempfile
    workdir = tempfile.mkdtemp(prefix='pcb_station_selftest_')
    try:
        target = local_path_for(workdir, 'det_142233.jpg')
        if target != os.path.join(os.path.abspath(workdir), 'det_142233.jpg'):
            raise AssertionError('下载路径构造错: %r' % target)
        if local_path_for(workdir, '../escape.jpg') != os.path.join(
                os.path.abspath(workdir), 'escape.jpg'):
            raise AssertionError('下载路径未做 basename 防护')
        # 第一次保存 -> 原名；第二次同名 -> 自动改名，绝不覆盖
        first = save_bytes(target, b'one')
        second = save_bytes(target, b'two')
        if os.path.basename(second) != 'det_142233_1.jpg':
            raise AssertionError('同名文件应改名为 _1, 实际 %r' % second)
        if open(first, 'rb').read() != b'one':
            raise AssertionError('改名避让时把原文件覆盖了')
        # 嵌套目录要能自动创建
        nested = save_bytes(os.path.join(workdir, 'a/b/c.jpg'), b'x')
        if not os.path.isfile(nested):
            raise AssertionError('多级目录未自动创建: %r' % nested)

        jpeg = _make_test_jpeg(320, 240)
        if jpeg is None:
            print('[SKIP] 未安装 pillow, 跳过缩略图缩放校验')
            return '下载路径/改名避让通过（缩略图项因缺 pillow 跳过）'
        png = make_thumbnail(jpeg)
        if png[:4] != b'\x89PNG':
            raise AssertionError('缩略图不是 PNG 数据')
        from PIL import Image as _Image
        image = _Image.open(io.BytesIO(png))
        if image.size != (THUMB_W, THUMB_H):
            raise AssertionError('缩略图尺寸应为 %dx%d, 实际 %r'
                                 % (THUMB_W, THUMB_H, image.size))
        # 4:3 的源图放进 16:9 的框：等比缩到 120x90，左右各 20 像素黑边，
        # 所以中心是图、左右边缘必须是黑的 —— 若被拉伸成 160x90，边缘就不会是黑
        center = image.getpixel((THUMB_W // 2, THUMB_H // 2))
        left = image.getpixel((1, THUMB_H // 2))
        right = image.getpixel((THUMB_W - 2, THUMB_H // 2))
        if left != (0, 0, 0) or right != (0, 0, 0):
            raise AssertionError('缩略图不是"等比缩放+黑边"，左右边缘应为黑: %r %r'
                                 % (left, right))
        if center == (0, 0, 0):
            raise AssertionError('缩略图中心不该是黑边: %r' % (center,))
        return '下载路径/改名避让/多级目录 + 160x90 等比缩略图（黑边填充）全部正确'
    finally:
        try:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass


def _test_http_roundtrip():
    """
    真起一个"假板卡"HTTP 服务（127.0.0.1 随机端口），用本程序的 urllib 代码
    走一遍 /api/status、/api/defects、/defects/<name>、/stream.mjpg、
    /api/events（含增量语义）、/api/snap。
    这样不用板卡也能验证 URL + 解析 + 流式读取整条链路。
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    sd_free = 30512078438
    save_dir = '/mnt/sdcard/defect'
    status_body = json.dumps({
        'ok': True, 'ratio': 0.856, 'kbps': 10.8, 'qsel': 2,
        'fps': 14.0, 'blocks': 1440, 'nbytes': 13200,
        'sd_free': sd_free, 'save_dir': save_dir,
    }).encode()
    defects_body = json.dumps({'files': [
        {'name': 'det_142233.jpg', 'size': len(FAKE_JPEG), 'mtime': 1712345678},
    ]}).encode()
    all_events = [
        {'seq': 1, 't': 1712345678.0, 'type': 'detect', 'file': 'det_142233.jpg',
         'size': len(FAKE_JPEG), 'cls': 'defect_missing', 'score': 0.91,
         'ratio': 0.856, 'fps': 14.0, 'mode': 3},
        {'seq': 2, 't': 1712345707.0, 'type': 'manual', 'file': 'manual_142307.jpg',
         'size': 86321, 'cls': None, 'score': None, 'ratio': None,
         'fps': 14.0, 'mode': None},
    ]
    snap_body = json.dumps({'ok': True, 'file': 'manual_142307.jpg',
                            'size': 86321, 'dir': save_dir}).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body, content_type, code=200):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path, _, query = self.path.partition('?')
            if path == '/api/status':
                self._send(status_body, 'application/json')
            elif path == '/api/defects':
                self._send(defects_body, 'application/json')
            elif path == '/api/events':
                # 增量语义：since >= 2 时没有新事件（与契约一致）
                since = 0
                for part in query.split('&'):
                    if part.startswith('since='):
                        try:
                            since = int(part[6:] or 0)
                        except ValueError:
                            since = 0
                fresh = [e for e in all_events if e['seq'] > since]
                body = json.dumps({'next': 2, 'events': fresh}).encode()
                self._send(body, 'application/json')
            elif path == '/api/snap':
                self._send(snap_body, 'application/json')
            elif path == '/defects/det_142233.jpg':
                self._send(FAKE_JPEG, 'image/jpeg')
            elif path == '/snap.jpg':
                self._send(FAKE_JPEG, 'image/jpeg')
            elif path == '/stream.mjpg':
                self.send_response(200)
                self.send_header('Content-Type',
                                 'multipart/x-mixed-replace; boundary=frame')
                self.end_headers()
                for _ in range(3):
                    self.wfile.write(_multipart_part(FAKE_JPEG))
                self.wfile.write(b'--' + BOUNDARY + b'--\r\n')
            else:
                self.send_response(404)
                self.end_headers()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.1})
    thread.daemon = True
    thread.start()
    base = build_base('127.0.0.1', port)
    try:
        status = parse_status_json(fetch_bytes(url_status(base), timeout=5.0))
        if status['ratio_pct'] != '85.6%' or status['kbps'] != 10.8:
            raise AssertionError('HTTP 取回的 status 解析不对: %r' % status)
        if status['sd_free'] != sd_free or status['save_dir'] != save_dir:
            raise AssertionError('HTTP 取回的 sd_free/save_dir 不对: %r' % (status,))
        items = parse_defects_json(fetch_bytes(url_defects(base), timeout=5.0))
        if len(items) != 1 or items[0]['name'] != 'det_142233.jpg':
            raise AssertionError('HTTP 取回的缺陷列表不对: %r' % items)
        raw = fetch_bytes(url_defect_file(base, items[0]['name']), timeout=5.0)
        if raw != FAKE_JPEG:
            raise AssertionError('下载的缺陷图字节不一致')
        if fetch_bytes(url_snapshot(base), timeout=5.0) != FAKE_JPEG:
            raise AssertionError('/snap.jpg 字节不一致')

        # /api/events 走真实 HTTP：首批 2 条 -> next=2；再拉 ?since=2 必须为空
        cursor = EventCursor()
        batch = parse_events_json(fetch_bytes(url_events(base, cursor.since()), timeout=5.0))
        new_events = cursor.ingest(batch)
        if len(new_events) != 2:
            raise AssertionError('首批事件应 2 条, 实际 %d' % len(new_events))
        if cursor.since() != 2:
            raise AssertionError('events next 游标应为 2, 实际 %r' % cursor.since())
        empty_batch = parse_events_json(
            fetch_bytes(url_events(base, cursor.since()), timeout=5.0))
        if cursor.ingest(empty_batch) != []:
            raise AssertionError('since=next 时不应再返回事件（增量语义错）')
        if format_event_line(new_events[0]).split(' [')[1].split(']')[0] != '检出':
            raise AssertionError('检出事件格式化错: %r' % format_event_line(new_events[0]))

        # /api/snap：截图成功返回文件名/大小/目录
        snap = parse_snap_json(fetch_bytes(url_snap(base), timeout=5.0))
        if not snap['ok'] or snap['file'] != 'manual_142307.jpg' or snap['size'] != 86321:
            raise AssertionError('HTTP 取回的 /api/snap 解析不对: %r' % (snap,))
        if snap['dir'] != save_dir:
            raise AssertionError('snap.dir 不对: %r' % (snap['dir'],))

        # 流式读取：小块 read 模拟真实 socket 分片
        parser = MjpegParser()
        frames = []
        response = urlopen(Request(url_stream(base), headers={'User-Agent': _USER_AGENT}),
                           timeout=5.0)
        try:
            while True:
                chunk = response.read(512)
                if not chunk:
                    break
                frames.extend(parser.feed(chunk))
        finally:
            response.close()
        if len(frames) != 3:
            raise AssertionError('MJPEG 流应收到 3 帧, 实际 %d' % len(frames))
        return ('假板卡(%s) 上 /api/status(含 sd_free/save_dir) · /api/defects · '
                '/defects/<name> · /snap.jpg · /stream.mjpg · '
                '/api/events(增量) · /api/snap 全部按契约走通' % base)
    finally:
        server.shutdown()
        try:
            server.server_close()
        except Exception:
            pass


def _test_events_json_and_cursor():
    """/api/events 解析 + 增量游标（去重 / 乱序 / seq 回退 / 空批）"""
    payload = json.dumps({'next': 3, 'events': [
        {'seq': 1, 't': 1712345678.0, 'type': 'detect', 'file': 'det_142233.jpg',
         'size': 88064, 'cls': 'defect_missing', 'score': 0.91, 'ratio': 0.856,
         'fps': 14.0, 'mode': 3},
        {'seq': 2, 't': 1712345707.0, 'type': 'manual', 'file': 'manual_142307.jpg',
         'size': 86321, 'cls': None, 'score': None, 'ratio': None,
         'fps': 14.0, 'mode': None},
        {'seq': 0, 'type': 'detect', 'file': 'ignored_no_seq.jpg'},   # seq 非法 -> 丢
        {'seq': 4, 'type': 'unknown', 'file': '../evil.jpg'},         # 越过 next 也要收
    ]}).encode()
    parsed = parse_events_json(payload)
    if parsed['next'] != 3:
        raise AssertionError('next 解析错: %r' % (parsed['next'],))
    if len(parsed['events']) != 3:
        raise AssertionError('应保留 3 条有效事件(丢掉 seq=0), 实际 %r' % (parsed['events'],))
    files = [e['file'] for e in parsed['events']]
    if files != ['det_142233.jpg', 'manual_142307.jpg', 'evil.jpg']:
        raise AssertionError('文件名/路径穿越处理不对: %r' % (files,))
    detect, manual, unknown = parsed['events']
    if detect['cls'] != 'defect_missing' or abs(detect['score'] - 0.91) > 1e-9:
        raise AssertionError('检出事件字段解析错: %r' % (detect,))
    if detect['mode'] != 3 or abs(detect['ratio'] - 0.856) > 1e-9:
        raise AssertionError('检出事件 mode/ratio 解析错: %r' % (detect,))

    # 日志行格式（需求给的样例：'14:22:33 [检出] defect_missing 0.91  → det_142233.jpg (86 KB)'）
    line_detect = format_event_line(detect)
    if not re.match(r'^\d\d:\d\d:\d\d \[检出\] defect_missing 0\.91  → det_142233\.jpg '
                    r'\(86\.0 KB\)$', line_detect):
        raise AssertionError('检出日志行格式不对: %r' % line_detect)
    line_manual = format_event_line(manual)
    if not re.match(r'^\d\d:\d\d:\d\d \[手动\] 截图 → manual_142307\.jpg \(84\.3 KB\)$',
                    line_manual):
        raise AssertionError('手动截图日志行格式不对: %r' % line_manual)
    if '[事件]' not in format_event_line(unknown):
        raise AssertionError('未知类型应有兜底格式: %r' % format_event_line(unknown))

    # 增量 + 去重：同一批吃两次，第二次必须为空
    cursor = EventCursor()
    first = cursor.ingest(parsed)
    if len(first) != 3 or [e['seq'] for e in first] != [1, 2, 4]:
        raise AssertionError('首批事件应全部交付且按 seq 升序: %r'
                             % [e['seq'] for e in first])
    if cursor.since() != 4:
        raise AssertionError('游标应推进到 max(next=3, 最高 seq=4), 实际 %r' % cursor.since())
    if cursor.ingest(parsed) != []:
        raise AssertionError('重复批次必须被去重')
    empty = parse_events_json(b'{"next":4,"events":[]}')
    if cursor.ingest(empty) != [] or cursor.since() != 4:
        raise AssertionError('空批次不应产生事件, next 也不该变: %r' % cursor.since())

    # 空批 + next 回退（板卡刚重启、还没有新事件）：游标必须跟回去，
    # 否则会一直用大 since 去问，永远收不到新事件
    cursor4 = EventCursor(since=9)
    if cursor4.ingest(parse_events_json(b'{"next":1,"events":[]}')) != []:
        raise AssertionError('空批不应产生事件')
    if cursor4.since() != 1 or cursor4.regressions != 1:
        raise AssertionError('空批 + next 回退时游标应回到 1: since=%d regressions=%d'
                             % (cursor4.since(), cursor4.regressions))

    # 批内乱序：seq 3 先给、seq 2 后给 -> 显示顺序必须是 2,3
    cursor2 = EventCursor(since=1)
    out_of_order = parse_events_json(json.dumps({'next': 3, 'events': [
        {'seq': 3, 't': 1712345700, 'type': 'detect', 'file': 'c.jpg'},
        {'seq': 2, 't': 1712345600, 'type': 'detect', 'file': 'b.jpg'},
    ]}))
    got = cursor2.ingest(out_of_order)
    if [e['seq'] for e in got] != [2, 3]:
        raise AssertionError('批内乱序未按 seq 升序显示: %r' % [e['seq'] for e in got])

    # seq 回退（板卡重启，计数器归 1）：必须照收，并记录一次回退
    regression = parse_events_json(json.dumps({'next': 1, 'events': [
        {'seq': 1, 't': 1712400000, 'type': 'manual', 'file': 'manual_restart.jpg',
         'size': 1024},
    ]}))
    got = cursor2.ingest(regression)
    if len(got) != 1 or got[0]['file'] != 'manual_restart.jpg':
        raise AssertionError('seq 回退后的事件被丢弃了: %r' % (got,))
    if cursor2.regressions != 1 or cursor2.since() != 1:
        raise AssertionError('seq 回退处理不对: regressions=%d since=%d'
                             % (cursor2.regressions, cursor2.since()))
    # 回退之后继续正常增量
    after = parse_events_json(json.dumps({'next': 2, 'events': [
        {'seq': 2, 't': 1712400001, 'type': 'detect', 'file': 'after_restart.jpg'},
    ]}))
    got = cursor2.ingest(after)
    if len(got) != 1 or cursor2.since() != 2:
        raise AssertionError('回退后的增量轮询没接上: %r / %r' % (got, cursor2.since()))

    # 缺 next 字段：用批内最高 seq 当游标
    no_next = parse_events_json(b'{"events":[{"seq":7,"type":"detect","file":"a.jpg"}]}')
    if no_next['next'] is not None:
        raise AssertionError('缺 next 时应为 None: %r' % (no_next['next'],))
    cursor3 = EventCursor(since=5)
    got = cursor3.ingest(no_next)
    if len(got) != 1 or cursor3.since() != 7:
        raise AssertionError('缺 next 时游标应取最高 seq: %r' % cursor3.since())

    # 坏 JSON / 结构不对 -> ValueError
    for bad in (b'', b'not json', b'[1,2,3]', b'{"events":"oops"}'):
        try:
            parse_events_json(bad)
        except ValueError:
            continue
        raise AssertionError('坏 events 响应 %r 应该抛 ValueError' % bad)
    return '增量游标/去重/批内乱序/seq 回退/缺 next/坏 JSON + 两种日志行格式全部正确'


def _test_snap_json():
    """/api/snap 响应解析（成功 / no frame / 缺 file / 路径穿越 / 坏 JSON）"""
    ok = parse_snap_json(b'{"ok":true,"file":"manual_142307.jpg","size":86321,'
                         b'"dir":"/mnt/sdcard/defect"}')
    if not ok['ok'] or ok['file'] != 'manual_142307.jpg':
        raise AssertionError('成功响应解析错: %r' % (ok,))
    if ok['size'] != 86321 or ok['dir'] != '/mnt/sdcard/defect':
        raise AssertionError('size/dir 解析错: %r' % (ok,))
    no_frame = parse_snap_json(b'{"ok":false,"error":"no frame"}')
    if no_frame['ok'] or no_frame['error'] != 'no frame':
        raise AssertionError('no frame 响应解析错: %r' % (no_frame,))
    if parse_snap_json(b'{"ok":false}')['error'] == '':
        raise AssertionError('ok=false 但没给原因时应有兜底文案')
    try:
        parse_snap_json(b'{"ok":true}')
    except ValueError:
        pass
    else:
        raise AssertionError('ok=true 却没有 file 应抛 ValueError')
    if parse_snap_json(b'{"ok":true,"file":"../x/manual.jpg"}')['file'] != 'manual.jpg':
        raise AssertionError('截图文件名未做 basename 防护')
    if parse_snap_json(b'{"ok":true,"file":"m.jpg","size":"86321"}')['size'] != 86321:
        raise AssertionError('字符串数字容错失效')
    for bad in (b'', b'<html>503 Service Unavailable</html>', b'[1]'):
        try:
            parse_snap_json(bad)
        except ValueError:
            continue
        raise AssertionError('坏 snap 响应 %r 应该抛 ValueError' % bad)
    return '成功/no frame/缺 file/路径穿越/坏 HTML 全部按预期处理'


def _test_human_and_sd_text():
    """sd_free 人类可读格式化 + /api/status 新增字段"""
    cases = [
        (0, '0 B'),
        (1023, '1023 B'),
        (1024, '1.0 KB'),
        (86321, '84.3 KB'),
        (1024 * 1024 * 5, '5.0 MB'),
        (1024 ** 3, '1.0 GB'),
        (30512078438, '28.4 GB'),
        (1024 ** 4, '1.00 TB'),
    ]
    for raw, expected in cases:
        got = human_size(raw)
        if got != expected:
            raise AssertionError('human_size(%r) = %r, 期望 %r' % (raw, got, expected))
    if human_size(None) != '--' or human_size(-1) != '--':
        raise AssertionError('无数据时 human_size 应给 --')
    if format_sd_text(30512078438, '/mnt/sdcard/defect') != \
            'SD 可用: 28.4 GB (/mnt/sdcard/defect)':
        raise AssertionError('SD 显示格式不对: %r'
                             % format_sd_text(30512078438, '/mnt/sdcard/defect'))
    if format_sd_text(None, '') != 'SD 可用: --':
        raise AssertionError('没有 sd_free 时应显示 --: %r' % format_sd_text(None, ''))
    status = parse_status_json(b'{"ok":true,"ratio":0.856,"kbps":10.8,"qsel":2,'
                               b'"fps":14.0,"blocks":1440,"nbytes":13200,'
                               b'"sd_free":30512078438,"save_dir":"/mnt/sdcard/defect"}')
    if status['sd_free'] != 30512078438 or status['save_dir'] != '/mnt/sdcard/defect':
        raise AssertionError('/api/status 新增字段没解析出来: %r' % (status,))
    if status['ratio_pct'] != '85.6%' or status['nbytes'] != 13200:
        raise AssertionError('原有状态字段被破坏: %r' % (status,))
    return '字节->B/KB/MB/GB/TB + SD 文案 + status 新增字段全部正确'


def _test_cover_status_parse():
    """/api/status 的 cover(本窗口采到整幅图多少面积) / b_per_blk(字节每块)：解析、夹紧、容错"""
    # 板卡 5d7337a 起 cover 的分母是**一帧满块数 32400**：1440 块 -> 1440/32400 = 0.044444
    full = parse_status_json(
        b'{"ok":true,"ratio":0.856,"blocks":1440,"nbytes":13200,'
        b'"cover":0.044444,"b_per_blk":9.17}')
    if abs(full['cover'] - 0.044444) > 1e-9 or abs(full['b_per_blk'] - 9.17) > 1e-9:
        raise AssertionError('cover/b_per_blk 没解析出来: %r' % (full,))
    if full['ratio_pct'] != '85.6%' or full['nbytes'] != 13200:
        raise AssertionError('原有状态字段被破坏: %r' % (full,))
    # 板卡口径：cover = blocks / 32400(一帧满块数)，b_per_blk = nbytes/blocks
    scaled = parse_status_json(b'{"blocks":1440,"nbytes":13200,"cover":0.044444,'
                               b'"b_per_blk":9.166666666666666}')
    if abs(scaled['cover'] - 1440 / float(COVER_FULL_FRAME_BLOCKS)) > 1e-6:
        raise AssertionError('cover 不等于 blocks/32400: %r' % (scaled['cover'],))
    if abs(scaled['b_per_blk'] - 13200 / 1440.0) > 1e-6:
        raise AssertionError('b_per_blk 不等于 nbytes/blocks: %r' % (scaled['b_per_blk'],))
    # 字符串数字容错（板卡脚本用 json.dumps 前可能先算成字符串）
    texty = parse_status_json(b'{"cover":"0.1","b_per_blk":"7.76"}')
    if abs(texty['cover'] - 0.1) > 1e-9 or abs(texty['b_per_blk'] - 7.76) > 1e-9:
        raise AssertionError('cover/b_per_blk 字符串数字容错失效: %r' % (texty,))
    # 越界只夹紧不抛异常（cover 是估算值，不能因为 1.000001 就崩界面）
    if parse_status_json(b'{"cover":1.5}')['cover'] != 1.0:
        raise AssertionError('cover>1 应夹到 1.0')
    if parse_status_json(b'{"cover":-0.2}')['cover'] != 0.0:
        raise AssertionError('cover<0 应夹到 0.0')
    # blocks=0（这一窗口没编码）-> 板卡给 b_per_blk=null
    zero = parse_status_json(b'{"blocks":0,"nbytes":0,"cover":0.0,"b_per_blk":null}')
    if zero['b_per_blk'] is not None or zero['cover'] != 0.0:
        raise AssertionError('blocks=0 时应 b_per_blk=null: %r' % (zero,))
    # 负数"字节/块"无意义 -> None（界面上不显示 '-3.0 B'）
    if parse_status_json(b'{"b_per_blk":-3}')['b_per_blk'] is not None:
        raise AssertionError('负数 b_per_blk 应给 None')
    return ('cover/b_per_blk 解析 + 口径(blocks/32400, nbytes/blocks) + '
            '字符串容错 + 0~1 夹紧 + blocks=0(null) 全部正确')


def _test_cover_full_frame_blocks():
    """
    t8 口径：cover 的分母是**一帧满块数 240x135 = 32400**（不是 4 帧预算 129600）。

    用假板卡数据把三种真实读数钉住，并确认它们落到界面的哪一档：
        · p19(条带轮转后) 9960/32400 ≈ 0.3074 -> 30.7%，在 25% 阈值之上 -> 不报警；
        · 假板卡 1440/32400 = 0.044444 -> 4.4%，低于阈值 -> 报警（抽样明显偏少）；
        · 旧位流(p18) 12960/32400 = 0.40 -> 40%，是**重复编码虚高**，不报警也不是异常；
        · 若还用旧分母 129600，p19 会被读成 9960/129600 = 7.7%（误导成"比修前更差"）。
    """
    if COVER_FULL_FRAME_BLOCKS != 32400 or COVER_FULL_FRAME_BLOCKS != 240 * 135:
        raise AssertionError('cover 分母常量应为 32400 (240x135): %r'
                             % (COVER_FULL_FRAME_BLOCKS,))
    fake = 1440 / float(COVER_FULL_FRAME_BLOCKS)
    if abs(fake - 0.044444) > 1e-6:
        raise AssertionError('假板卡 cover 应为 1440/32400=0.044444, 实际 %r' % (fake,))
    line_fake, low_fake = format_cover_line({'cover': fake, 'b_per_blk': 13200 / 1440.0})
    if not low_fake or not line_fake.startswith('覆盖率: 4.4% (本窗口抽样, 明显偏少)'):
        raise AssertionError('假板卡(4.4%%)状态行不对: %r low=%r' % (line_fake, low_fake))
    # p19 预期：9960/32400 = 30.74% -> 30.7%，阈值之上不报警
    p19 = 9960 / float(COVER_FULL_FRAME_BLOCKS)
    if abs(p19 - 0.307) > 1e-3:
        raise AssertionError('p19 预期 cover 应为 9960/32400≈0.307: %r' % (p19,))
    line_p19, low_p19 = format_cover_line({'cover': p19, 'b_per_blk': 7.76})
    if low_p19 or line_p19 != '覆盖率: 30.7% (本窗口抽样) ｜ 字节/块: 7.8 B':
        raise AssertionError('p19(30.7%%)状态行不对: %r low=%r' % (line_p19, low_p19))
    # 旧位流重复编码 -> 0.40，是虚高：不报警，也不能显示成"抽样偏少"
    legacy = 12960 / float(COVER_FULL_FRAME_BLOCKS)
    line_legacy, low_legacy = format_cover_line({'cover': legacy, 'b_per_blk': 13.5})
    if abs(legacy - 0.40) > 1e-9 or low_legacy or line_legacy != \
            '覆盖率: 40% (本窗口抽样) ｜ 字节/块: 13.5 B':
        raise AssertionError('旧位流(40%%, 重复编码虚高)不该报警: %r low=%r'
                             % (line_legacy, low_legacy))
    # 反证：同一个 9960 块若除以旧分母 129600 只有 7.7% —— 旧分母就是把 p19 误读成"更差"
    wrong = 9960 / 129600.0
    if wrong >= COVER_LOW_THRESHOLD:
        raise AssertionError('旧分母本应把 p19 误读成低于阈值(7.7%%): %r' % (wrong,))
    return ('分母=32400(240x135) + 假板卡 4.4% 报警 + p19 30.7% 不报警 + '
            '旧位流 40% 虚高不报警 + 旧分母 7.7% 的反证 全部正确')


def _test_cover_line_format():
    """覆盖率状态行三种取值：正常(≥25%) / 抽样明显偏少(<25%) / 缺失(--) —— t6 口径"""
    # 正常：t2 条带轮转后单窗口抽样约 31%，必须说清"本窗口抽样"，且不报警
    normal = parse_status_json(b'{"cover":0.31,"b_per_blk":7.76}')
    line, low = format_cover_line(normal)
    want_normal = '覆盖率: 31% (本窗口抽样) ｜ 字节/块: 7.8 B'
    if line != want_normal:
        raise AssertionError('正常覆盖率状态行不对:\n  实际 %r\n  期望 %r' % (line, want_normal))
    if low:
        raise AssertionError('31% 不该标橙（阈值是 25%，修后常态 31%）')
    # 抽样明显偏少（修前一个窗口只编同一批条带 ≈10%）：橙色 + "跨帧约 13 帧覆盖整幅"
    line_low, low_flag = format_cover_line(
        parse_status_json(b'{"cover":0.1,"b_per_blk":7.76}'))
    want_low = ('覆盖率: 10% (本窗口抽样, 明显偏少) ｜ 字节/块: 7.8 B'
                '  (跨帧约 13 帧覆盖整幅)')
    if line_low != want_low:
        raise AssertionError('低抽样状态行不对:\n  实际 %r\n  期望 %r' % (line_low, want_low))
    if not low_flag:
        raise AssertionError('10% 应标成抽样明显偏少（橙色提醒）')
    if '压缩率不代表整幅图' in line_low:
        raise AssertionError('t6 已删掉"压缩率不代表整幅图"的错误暗示: %r' % (line_low,))
    # 缺失（旧板卡）：显示 -- + 说明，且不能误报成"抽样偏少"
    line_old, low_old = format_cover_line(
        parse_status_json(b'{"blocks":1440,"nbytes":13200}'))
    want_old = '覆盖率: -- (旧版板卡无此字段) ｜ 字节/块: --'
    if line_old != want_old:
        raise AssertionError('缺字段时应显示 --: %r' % (line_old,))
    if low_old:
        raise AssertionError('cover 缺失时不该标橙（不知道就不能断言抽样偏少）')
    if format_cover_line(None) != (want_old, False):
        raise AssertionError('format_cover_line(None) 不应崩: %r'
                             % (format_cover_line(None),))
    # cover 有值但 b_per_blk=null（blocks=0）-> 只缺"字节/块"
    half = format_cover_line({'cover': 0.0, 'b_per_blk': None})[0]
    if half != ('覆盖率: 0% (本窗口抽样, 明显偏少) ｜ 字节/块: --'
                '  (跨帧约 13 帧覆盖整幅)'):
        raise AssertionError('b_per_blk=null 的状态行不对: %r' % (half,))
    # 整数百分比不留 .0（31% 比 31.0% 好读），非整数才留一位小数
    if _cover_pct_text(31.0) != '31%' or _cover_pct_text(24.9) != '24.9%':
        raise AssertionError('百分比格式不对: %r / %r'
                             % (_cover_pct_text(31.0), _cover_pct_text(24.9)))
    # 显示值与判据同源：24.96% 取整成 25.0% 后不能再标"明显偏少"（否则自相矛盾）
    line_2496, low_2496 = format_cover_line({'cover': 0.2496, 'b_per_blk': 7.8})
    if low_2496 or '25%' not in line_2496 or '明显偏少' in line_2496:
        raise AssertionError('24.96%% 取整后应显示 25%% 且不报警: %r low=%r'
                             % (line_2496, low_2496))
    return ('正常(31%)/偏少(10%,带 13 帧说明)/缺失(--)/字节块=null + 整数百分比格式 '
            '全部正确')


def _test_cover_sampling_semantics():
    """
    t6 的核心口径：cover 是**单窗口抽样率**，不是"整幅图覆盖率"。

    所以：① 阈值常量必须是 0.25；② 边界 24.9% 报警、25.1% 不报警；
          ③ 文案里永远不出现"压缩率不代表整幅图"这种误导；④ 31%(修后常态) 不报警。
    """
    if COVER_LOW_THRESHOLD != 0.25:
        raise AssertionError('COVER_LOW_THRESHOLD 应改为 0.25（单窗口抽样判据）: %r'
                             % (COVER_LOW_THRESHOLD,))
    # 阈值两侧：24.9% -> 报警；25.1% -> 不报警
    line_249, low_249 = format_cover_line({'cover': 0.249, 'b_per_blk': 7.8})
    line_251, low_251 = format_cover_line({'cover': 0.251, 'b_per_blk': 7.8})
    if not low_249 or low_251:
        raise AssertionError('25%% 边界判反了: 24.9%%->low=%r, 25.1%%->low=%r'
                             % (low_249, low_251))
    if '24.9%' not in line_249 or '明显偏少' not in line_249:
        raise AssertionError('24.9%% 的状态行不对: %r' % (line_249,))
    if '25.1%' not in line_251 or '明显偏少' in line_251 or '13 帧' in line_251:
        raise AssertionError('25.1%% 的状态行不对: %r' % (line_251,))
    # 正常与偏少两种文案都要说明"本窗口抽样"这个口径，且都不能出现旧的误导说法
    for cover in (0.31, 0.1, 0.92):
        text = format_cover_line({'cover': cover, 'b_per_blk': 7.8})[0]
        if '本窗口抽样' not in text:
            raise AssertionError('cover=%r 的文案没写清"本窗口抽样": %r' % (cover, text))
        if '压缩率不代表整幅图' in text or '只统计已编码条带' in text:
            raise AssertionError('cover=%r 的文案仍带旧口径: %r' % (cover, text))
    # 12/13 帧满覆盖的说法只在"抽样偏少"时才出现（那时才需要解释"跨帧是够的"）
    if '13 帧' in format_cover_line({'cover': 0.31, 'b_per_blk': 7.8})[0]:
        raise AssertionError('31% 时不该再挂"跨帧约 13 帧"说明（不是告警态）')
    if '13 帧' not in format_cover_line({'cover': 0.1, 'b_per_blk': 7.8})[0]:
        raise AssertionError('10% 时必须说明"跨帧约 13 帧覆盖整幅"')
    return ('阈值 0.25 + 24.9%/25.1% 边界 + 文案恒说"本窗口抽样"、'
            '无"压缩率不代表整幅图"字样 —— 口径已改成单窗口抽样率')


def _test_cover_old_board_tolerance():
    """旧板卡（13 字段，没有 cover/b_per_blk）必须照常工作：不抛异常、显示 --"""
    legacy = (b'{"ok":true,"ratio":0.856,"kbps":10.8,"qsel":2,"fps":14.0,'
              b'"blocks":1440,"nbytes":13200,"sd_free":30512078438,'
              b'"save_dir":"/mnt/sdcard/defect","auto":0,"target_kbps":20,'
              b'"autosave":1,"detect":1}')
    status = parse_status_json(legacy)
    if status['cover'] is not None or status['b_per_blk'] is not None:
        raise AssertionError('旧板卡缺字段时 cover/b_per_blk 应为 None: %r' % (status,))
    if status['ratio_pct'] != '85.6%' or status['sd_free'] != 30512078438:
        raise AssertionError('旧板卡其它字段被破坏: %r' % (status,))
    line, low = format_cover_line(status)
    want_old = '覆盖率: -- (旧版板卡无此字段) ｜ 字节/块: --'
    if line != want_old or low:
        raise AssertionError('旧板卡状态行应显示 -- 且不告警: %r low=%r' % (line, low))
    # 过渡期：只给其中一个字段也要能显示另一半
    only_cover = format_cover_line(parse_status_json(b'{"cover":0.6}'))[0]
    if only_cover != '覆盖率: 60% (本窗口抽样) ｜ 字节/块: --':
        raise AssertionError('只有 cover 时的状态行不对: %r' % (only_cover,))
    only_bpb = format_cover_line(parse_status_json(b'{"b_per_blk":9.17}'))[0]
    if only_bpb != '覆盖率: -- (旧版板卡无此字段) ｜ 字节/块: 9.2 B':
        raise AssertionError('只有 b_per_blk 时的状态行不对: %r' % (only_bpb,))
    return '旧板卡(无 cover/b_per_blk) 不报错且显示 -- + 只给一个字段也照常显示'


def _test_quality_commands():
    """压缩质量串口命令 Q/A/B + 连发次数规划（FPGA 是循环切档，不能假设一发到位）"""
    if quality_command() != b'Q':
        raise AssertionError('手动档位命令应为 b\'Q\', 实际 %r' % (quality_command(),))
    if auto_command() != b'A':
        raise AssertionError('自适应命令应为 b\'A\', 实际 %r' % (auto_command(),))
    if bitrate_command() != b'B':
        raise AssertionError('目标码率命令应为 b\'B\', 实际 %r' % (bitrate_command(),))
    # 不能和已有命令撞车：算法模式 '0'~'4'、阈值 '+'/'-'、码流 'J'
    reserved = set(bytearray(b'01234+-J'))
    for payload in (quality_command(), auto_command(), bitrate_command()):
        if payload[0] in reserved:
            raise AssertionError('压缩质量命令 %r 和已有串口命令撞车了' % (payload,))

    cases = [
        (2, 2, 0),      # 已在目标档：一发都不用
        (0, 2, 2),      # Q30 -> Q75：按两下
        (3, 3, 0),
        (3, 0, 1),      # Q90 -> Q30：循环一下
        (0, 3, 3),
        (1, 1, 0),
        (2, 0, 2),
        ('2', '0', 2),  # 字符串也认
    ]
    for current, target, expected in cases:
        got = plan_quality_presses(current, target)
        if got != expected:
            raise AssertionError('plan_quality_presses(%r, %r) = %r, 期望 %r'
                                 % (current, target, got, expected))
    if plan_quality_presses(None, 2) is not None:
        raise AssertionError('当前档位未知时应返回 None（先发 1 发探路）')
    if plan_quality_presses('xx', 2) is not None:
        raise AssertionError('当前档位认不出时也应返回 None')
    for bad in (4, -1, None, 'q'):
        try:
            plan_quality_presses(2, bad)
        except SerialCommandError:
            continue
        raise AssertionError('目标档位 %r 越界应抛 SerialCommandError' % (bad,))

    presets = [
        (280, 320), (320, 360), (360, 400), (400, 440), (440, 280),
        (None, 280), (30, 280),      # 不在预设表里 -> 从第一档开始
    ]
    for current, expected in presets:
        got = next_bitrate_preset(current)
        if got != expected:
            raise AssertionError('next_bitrate_preset(%r) = %r, 期望 %r'
                                 % (current, got, expected))
    if list(BITRATE_PRESETS_KBPS) != [280, 320, 360, 400, 440]:
        raise AssertionError('目标码率预设应为 280/320/360/400/440 KB/s（每档 40）: %r'
                             % (BITRATE_PRESETS_KBPS,))
    return 'Q/A/B 三个命令字符 + 8 组连发规划 + 循环预设表(40KB/s 单位)全部正确（且不与 0~4/+-/J 撞车）'


def _test_quality_status_and_highlight():
    """压缩质量回读：qsel/auto/target_kbps 容错、状态行、qsel→按钮高亮映射"""
    full = parse_status_json(
        b'{"ok":true,"ratio":0.856,"qsel":2,"auto":0,"target_kbps":20,'
        b'"kbps":18.6,"sd_free":30512078438,"save_dir":"/mnt/sdcard/defect"}')
    if full['auto'] != 0 or full['target_kbps'] != 20 or full['qsel'] != 2:
        raise AssertionError('新增字段解析错: auto=%r target=%r qsel=%r'
                             % (full['auto'], full['target_kbps'], full['qsel']))
    want = '质量 Q75 (手动)  目标 20 KB/s  实际 18.6 KB/s'
    got = format_quality_line(full)
    if got != want:
        raise AssertionError('质量状态行不对:\n  实际 %r\n  期望 %r' % (got, want))
    # 原有字段不能被破坏
    if full['ratio_pct'] != '85.6%' or full['sd_free'] != 30512078438:
        raise AssertionError('原有 status 字段被破坏: %r' % (full,))

    auto_line = format_quality_line({'qsel': 3, 'auto': 1, 'target_kbps': 40,
                                     'kbps': 39.95})
    if auto_line != '质量 Q90 (自适应)  目标 40 KB/s  实际 40.0 KB/s':
        raise AssertionError('自适应状态行不对: %r' % auto_line)

    # 旧版板卡脚本：没有 auto / target_kbps -> 显示 --，绝不报错
    old = parse_status_json(b'{"ok":true,"qsel":2,"kbps":18.6}')
    if old['auto'] is not None or old['target_kbps'] is not None:
        raise AssertionError('缺字段时应为 None: %r' % (old,))
    old_line = format_quality_line(old)
    if old_line != '质量 Q75 (--)  目标 --  实际 18.6 KB/s':
        raise AssertionError('旧板卡容错行不对: %r' % old_line)
    # 旧位流水印 target5 恒 0 -> target_kbps=0 也要显示 --
    if '目标 --' not in format_quality_line({'qsel': 1, 'auto': 0, 'target_kbps': 0,
                                             'kbps': 5.0}):
        raise AssertionError('target_kbps=0 应显示 --')
    # 连 qsel 都没有（更旧的板卡）
    none_line = format_quality_line({'kbps': 12.3})
    if none_line != '质量 -- (--)  目标 --  实际 12.3 KB/s':
        raise AssertionError('全缺字段时状态行不对: %r' % none_line)
    if format_quality_line(None) != '质量 -- (--)  目标 --  实际 --':
        raise AssertionError('format_quality_line(None) 不应崩: %r'
                             % format_quality_line(None))

    # auto 的写法容错：bool / 字符串 / 中文
    for raw, expected in ((True, 1), (False, 0), ('1', 1), ('0', 0), ('auto', 1),
                          ('manual', 0), ('自适应', 1), ('手动', 0), ('xx', None),
                          (2, 1)):
        got = _as_bool01(raw)
        if got != expected:
            raise AssertionError('_as_bool01(%r) = %r, 期望 %r' % (raw, got, expected))
    if parse_status_json(b'{"auto":true,"target_kbps":"20"}')['auto'] != 1:
        raise AssertionError('auto=true 应解析成 1')
    if parse_status_json(b'{"target_kbps":"20"}')['target_kbps'] != 20:
        raise AssertionError('target_kbps 字符串数字应容错')

    # qsel -> 档位按钮高亮映射（界面高亮就靠这个函数）
    for qsel, name in enumerate(QSEL_NAMES):
        if quality_checked_index(qsel) != qsel:
            raise AssertionError('qsel=%d 的高亮下标错了: %r'
                                 % (qsel, quality_checked_index(qsel)))
        if qsel_name(qsel) != name:
            raise AssertionError('qsel=%d 的名称应为 %s, 实际 %r'
                                 % (qsel, name, qsel_name(qsel)))
    if QSEL_NAMES != ('Q30', 'Q50', 'Q75', 'Q90'):
        raise AssertionError('档位名称应为 Q30/Q50/Q75/Q90: %r' % (QSEL_NAMES,))
    for bad in (None, 4, 9, -1, 'x'):
        if quality_checked_index(bad) is not None:
            raise AssertionError('qsel=%r 应不高亮任何档位' % (bad,))
        if qsel_name(bad) != '--':
            raise AssertionError('qsel=%r 的名称应为 --' % (bad,))
    return ('状态行 4 种情形 + auto/target 缺字段容错 + bool/字符串容错 + '
            'qsel→高亮映射(0~3/越界) 全部正确')


def _test_config_url_and_parse():
    """板卡功能开关：/api/config 的 URL 构造 + 响应解析（含 HTTP 400 错误体）"""
    base = 'http://192.168.10.50:8000'
    cases = [
        ((None, None), base + '/api/config'),
        ((0, None), base + '/api/config?autosave=0'),
        ((None, 1), base + '/api/config?detect=1'),
        ((1, 0), base + '/api/config?autosave=1&detect=0'),
        (('1', '0'), base + '/api/config?autosave=1&detect=0'),   # 字符串也认
    ]
    for (autosave, detect), expected in cases:
        got = url_config(base, autosave=autosave, detect=detect)
        if got != expected:
            raise AssertionError('url_config(%r, %r) = %r, 期望 %r'
                                 % (autosave, detect, got, expected))
    for bad in (2, -1, 'x'):
        try:
            url_config(base, autosave=bad)
        except SerialCommandError:
            continue
        raise AssertionError('autosave=%r 非法值应抛 SerialCommandError' % (bad,))

    good = parse_config_json(b'{"ok":true,"autosave":0,"detect":1,'
                             b'"changed":["autosave"]}')
    if not good['ok'] or good['autosave'] != 0 or good['detect'] != 1:
        raise AssertionError('config 成功响应解析错: %r' % (good,))
    if good['changed'] != ['autosave']:
        raise AssertionError('changed 解析错: %r' % (good['changed'],))
    if good['error'] != '' or good['warning'] != '':
        raise AssertionError('成功时不应有 error/warning: %r' % (good,))

    # HTTP 400 非法值：**必须能解析出 error**，不能当网络错误吞掉
    bad400 = parse_config_json(b'{"ok":false,"error":"bad autosave=xx"}', http_status=400)
    if bad400['ok'] or bad400['error'] != 'bad autosave=xx' or bad400['http_status'] != 400:
        raise AssertionError('400 错误体解析错: %r' % (bad400,))
    # 只给状态码不给 error：兜底成 'HTTP 500'
    only_code = parse_config_json(b'{"ok":true}', http_status=500)
    if only_code['ok'] or 'HTTP 500' not in only_code['error']:
        raise AssertionError('HTTP 5xx 兜底文案不对: %r' % (only_code,))

    warn = parse_config_json(b'{"ok":true,"autosave":1,"detect":0,"changed":[],'
                             b'"warning":"no model loaded"}')
    if not warn['ok'] or warn['detect'] != 0 or 'no model' not in warn['warning']:
        raise AssertionError('warning 解析错: %r' % (warn,))

    # 容错：changed 给字符串、缺字段、缺 changed
    if parse_config_json(b'{"ok":true,"changed":"detect"}')['changed'] != ['detect']:
        raise AssertionError('changed 为字符串时应包成列表')
    if parse_config_json(b'{"ok":true}')['changed'] != []:
        raise AssertionError('缺 changed 时应为 []')
    missing = parse_config_json(b'{"ok":true}')
    if missing['autosave'] is not None or missing['detect'] is not None:
        raise AssertionError('缺字段时应为 None: %r' % (missing,))
    for bad in (b'', b'not json', b'[1,2]'):
        try:
            parse_config_json(bad)
        except ValueError:
            continue
        raise AssertionError('坏 config 响应 %r 应该抛 ValueError' % bad)
    return ('URL 5 种写法 + 非法值拒绝 + 成功/400/warning/缺字段/坏 JSON 解析全部正确')


def _test_switch_state_and_tolerance():
    """开关取反、按钮文字、status 缺 autosave/detect 的容错、置灰原因"""
    toggles = [(1, 0), (0, 1), (True, 0), ('1', 0), ('0', 1), (None, None), ('x', None)]
    for current, expected in toggles:
        got = next_switch_value(current)
        if got != expected:
            raise AssertionError('next_switch_value(%r) = %r, 期望 %r'
                                 % (current, got, expected))
    texts = [
        (('autosave', 1), '自动截图: 开'),
        (('autosave', 0), '自动截图: 关'),
        (('autosave', None), '自动截图: --'),
        (('detect', 1), '目标检测: 开'),
        (('detect', 0), '目标检测: 关'),
        (('detect', '0'), '目标检测: 关'),
    ]
    for (name, value), expected in texts:
        got = format_switch_text(name, value)
        if got != expected:
            raise AssertionError('format_switch_text(%r, %r) = %r, 期望 %r'
                                 % (name, value, got, expected))

    # status 带新字段
    full = parse_status_json(b'{"ok":true,"ratio":0.856,"kbps":10.8,"qsel":2,"fps":14.0,'
                             b'"blocks":1440,"nbytes":13200,"sd_free":1,"save_dir":"/x",'
                             b'"auto":0,"target_kbps":20,"autosave":1,"detect":0}')
    if (full['autosave'], full['detect']) != (1, 0):
        raise AssertionError('status 新字段解析错: %r %r'
                             % (full['autosave'], full['detect']))
    # 旧板卡（11 字段，没有 autosave/detect）：不报错，给 None
    old = parse_status_json(b'{"ok":true,"ratio":0.856,"kbps":10.8,"qsel":2,"fps":14.0,'
                            b'"blocks":1440,"nbytes":13200,"sd_free":1,"save_dir":"/x",'
                            b'"auto":0,"target_kbps":20}')
    if old['autosave'] is not None or old['detect'] is not None:
        raise AssertionError('旧板卡缺字段时应为 None: %r %r'
                             % (old['autosave'], old['detect']))
    if old['ratio_pct'] != '85.6%' or old['target_kbps'] != 20 or old['qsel'] != 2:
        raise AssertionError('原有 status 字段被破坏: %r' % (old,))

    # 置灰原因：旧板卡 -> 版本过旧；有 warning -> 没加载模型；正常 detect=0 -> 可点（能再打开）
    if '版本过旧' not in switch_blocked_reason(old, SWITCH_DETECT):
        raise AssertionError('旧板卡应提示"板卡版本过旧"')
    if '版本过旧' not in switch_blocked_reason(old, SWITCH_AUTOSAVE):
        raise AssertionError('旧板卡应提示"板卡版本过旧"（autosave）')
    if '模型' not in switch_blocked_reason({'detect': 0, 'warning': 'no model loaded'},
                                           SWITCH_DETECT):
        raise AssertionError('有 warning 时应提示"板卡没加载模型"')
    if switch_blocked_reason({'detect': 0}, SWITCH_DETECT) != '':
        raise AssertionError('detect=0 但无 warning（用户自己关的）必须仍可点，否则开不回来')
    if switch_blocked_reason({'autosave': 1}, SWITCH_AUTOSAVE) != '':
        raise AssertionError('autosave 有值时不该被置灰')
    return ('取反 7 组 + 按钮文字 6 组 + status 新字段/旧板卡容错 + 3 种置灰原因 全部正确')


def _test_dim_config_and_exclusive():
    """尺寸测量开关：/api/config 的 dim 回读 + 与 detect 互斥的参数（t19）"""
    base = 'http://192.168.10.50:8000'
    # ① url_config 支持 dim，参数顺序固定 autosave → detect → dim
    cases = [
        ((None, None, 1), base + '/api/config?dim=1'),
        ((None, 0, 1), base + '/api/config?detect=0&dim=1'),
        ((None, 1, 0), base + '/api/config?detect=1&dim=0'),
        ((1, None, None), base + '/api/config?autosave=1'),
        (('1', '0', '1'), base + '/api/config?autosave=1&detect=0&dim=1'),
    ]
    for (auto, det, dim), expected in cases:
        got = url_config(base, autosave=auto, detect=det, dim=dim)
        if got != expected:
            raise AssertionError('url_config(%r, %r, %r) = %r, 期望 %r'
                                 % (auto, det, dim, got, expected))
    for bad in (2, -1, 'x'):
        try:
            url_config(base, dim=bad)
        except SerialCommandError:
            continue
        raise AssertionError('dim=%r 非法值应抛 SerialCommandError' % (bad,))
    # ② 互斥参数：一次请求把两个参数带齐（尺寸测量/目标检测各自正反四种）
    pairs = [
        ((SWITCH_DIM, 1), {'detect': 0, 'dim': 1}),
        ((SWITCH_DIM, 0), {'dim': 0}),
        ((SWITCH_DETECT, 1), {'dim': 0, 'detect': 1}),
        ((SWITCH_DETECT, 0), {'detect': 0}),
        ((SWITCH_AUTOSAVE, 1), {'autosave': 1}),
    ]
    for (name, target), expected in pairs:
        got = exclusive_params(name, target)
        if got != expected:
            raise AssertionError('exclusive_params(%r, %r) = %r, 期望 %r'
                                 % (name, target, got, expected))
    # 互斥请求实际发出去的 URL：点"尺寸测量开"必须同时关检测
    if url_config(base, **exclusive_params(SWITCH_DIM, 1)) != \
            base + '/api/config?detect=0&dim=1':
        raise AssertionError('尺寸测量开没把 detect=0 一起发出去')
    if url_config(base, **exclusive_params(SWITCH_DETECT, 1)) != \
            base + '/api/config?detect=1&dim=0':
        raise AssertionError('检测开没把 dim=0 一起发出去')
    # ③ /api/config 解析：dim 与 dim_auto_sec（旧板卡没有这两个字段 -> None）
    cfg = parse_config_json(b'{"ok":true,"autosave":1,"detect":0,"dim":1,'
                            b'"dim_auto_sec":5,"changed":["dim","detect"]}')
    if cfg['dim'] != 1 or cfg['dim_auto_sec'] != 5 or cfg['detect'] != 0:
        raise AssertionError('dim/dim_auto_sec 解析错: %r' % (cfg,))
    if cfg['changed'] != ['dim', 'detect']:
        raise AssertionError('互斥时 changed 应含被自动关掉的那项: %r' % (cfg['changed'],))
    old = parse_config_json(b'{"ok":true,"autosave":1,"detect":1,"changed":[]}')
    if old['dim'] is not None or old['dim_auto_sec'] is not None:
        raise AssertionError('旧板卡 dim 字段应为 None: %r' % (old,))
    if old['autosave'] != 1 or old['detect'] != 1:
        raise AssertionError('旧板卡既有字段被破坏: %r' % (old,))
    # ④ 置灰原因：dim 缺失 -> 版本过旧；dim 模块没加载 -> 没模块
    if '过旧' not in dim_blocked_reason(old):
        raise AssertionError('旧板卡应提示版本过旧: %r' % (dim_blocked_reason(old),))
    if '模块' not in dim_blocked_reason({'dim': 1, 'warning': 'dim module not loaded'}):
        raise AssertionError('没 dim 模块时应提示"没有尺寸测量模块"')
    if dim_blocked_reason({'dim': 0, 'warning': ''}) != '':
        raise AssertionError('dim 有值且无 warning 时应可点')
    return ('url_config 带 dim(5 组) + 非法值拒绝 + 互斥参数(4 组) + 互斥 URL + '
            'dim/dim_auto_sec 解析 + 旧板卡 None + 2 种置灰原因 全部正确')


def _test_dim_json_parse():
    """`GET /api/dim` 解析：正常 / calib_ok=false / 404 降级 / 坏 JSON"""
    body = (u'{"ok":true,"schema":"dim-v1","calib_ok":1,"calib_note":"可信",'
            u'"state":"ok","sigma_mm":1.26,"accuracy_mm":1.26,"tol_kind":"judge_line",'
            u'"scale_mm_per_px":0.026,"dim_available":1,"dim_module_error":"",'
            u'"busy":0,"age_s":1.2,"recalib_queued":0,"perf_ms":{"measure":96.0},'
            u'"targets":[{"name":"board_outline","value_mm":89.24,"nominal_mm":89.0,'
            u'"err_mm":0.24,"tol_mm":4.53,"ok":1},'
            u'{"name":"hole_pitch","value_mm":35.0,"nominal_mm":35.1,"err_mm":-0.1,'
            u'"tol_mm":1.0,"ok":0,"why":""}]}').encode('utf-8')
    dim = parse_dim_json(body)
    if not dim['ok'] or dim['supported'] is not True or dim['schema'] != 'dim-v1':
        raise AssertionError('/api/dim 正常响应解析错: %r' % (dim,))
    if dim['calib_ok'] != 1 or dim['state'] != 'ok' or dim['sigma_mm'] != 1.26:
        raise AssertionError('calib_ok/state/sigma 解析错: %r' % (dim,))
    if dim['tol_kind'] != 'judge_line':
        raise AssertionError('tol_kind 应原样透传(判定线不是精度): %r' % (dim['tol_kind'],))
    if len(dim['targets']) != 2:
        raise AssertionError('应解析出 2 个目标（S1：只剩板框/孔距）, 实际 %r'
                             % (len(dim['targets']),))
    first = dim['targets'][0]
    if (first['name'], first['value_mm'], first['nominal_mm'], first['err_mm'],
            first['ok'], first['label']) != ('board_outline', 89.24, 89.0, 0.24, 1, '板框'):
        raise AssertionError('第一个目标解析错（含中文列名）: %r' % (first,))
    if abs(first['err_pct'] - 0.24 / 89.0 * 100.0) > 1e-9:
        raise AssertionError('err_pct 计算错: %r' % (first['err_pct'],))
    if dim['targets'][1]['ok'] != 0:
        raise AssertionError('hole_pitch 应为 NG(ok=0)')
    # calib_ok=false：板卡一个 value_mm 都不给（null），界面也只照抄
    uncal = parse_dim_json(
        (u'{"ok":true,"schema":"dim-v1","calib_ok":0,'
         u'"calib_note":"标定不可信: 角点不足","state":"bad",'
         u'"sigma_mm":null,"targets":[{"name":"board_outline",'
         u'"value_mm":null,"nominal_mm":null,"err_mm":null,'
         u'"tol_mm":1.0,"ok":0}]}').encode('utf-8'))
    if uncal['calib_ok'] != 0 or uncal['targets'][0]['value_mm'] is not None:
        raise AssertionError('calib_ok=false 时值必须为 None: %r' % (uncal['targets'][0],))
    if uncal['calib_note'] != '标定不可信: 角点不足':
        raise AssertionError('calib_note 没解析出来: %r' % (uncal['calib_note'],))
    # 旧板卡（没有这个路由）-> 404 纯文本：supported=False，不抛异常
    old = parse_dim_json(b'404 not found', http_status=404)
    if old['supported'] is not False or old['ok'] is not False or old['targets'] != []:
        raise AssertionError('旧板卡 404 应降级: %r' % (old,))
    # 缺字段/空 targets 也要能解析
    empty = parse_dim_json(b'{"ok":true}')
    if empty['targets'] != [] or empty['calib_ok'] is not None or empty['state'] != '':
        raise AssertionError('缺字段时应给空表/None: %r' % (empty,))
    for bad in (b'not json', b'[1,2]', b''):
        try:
            parse_dim_json(bad)
        except ValueError:
            continue
        raise AssertionError('坏 /api/dim 响应 %r 应该抛 ValueError' % (bad,))
    return ('2 目标 10 字段 + err_pct + calib_ok=false 全 null + 404 降级 + '
            '缺字段/坏 JSON 容错 全部正确')


def _test_dim_table_render():
    """/api/dim -> 结果表行与摘要（测量值/真值/误差 mm+% / OK-NG；不可信时整表空）"""
    dim = parse_dim_json(
        b'{"ok":true,"schema":"dim-v1","calib_ok":1,"state":"ok","sigma_mm":1.26,'
        b'"targets":[{"name":"board_outline","value_mm":89.24,"nominal_mm":89.0,'
        b'"err_mm":0.24,"tol_mm":4.53,"ok":1},'
        b'{"name":"hole_pitch","value_mm":null,"nominal_mm":null,"err_mm":null,'
        b'"tol_mm":1.0,"ok":0}]}')
    rows = format_dim_rows(dim)
    if len(rows) != 2:
        raise AssertionError('应渲染 2 行（S1：只剩板框/孔距）, 实际 %r' % (len(rows),))
    if rows[0] != ('板框', '89.24 mm', '89.00 mm', '+0.24 mm (0.3%)', 'OK'):
        raise AssertionError('第一行文案不对: %r' % (rows[0],))
    if rows[1] != ('孔距', '--', '--', '--', 'NG'):
        raise AssertionError('没量到的目标应显示 --（判定仍按板卡 ok）: %r' % (rows[1],))
    summary = format_dim_summary(dim)
    if '已标定' not in summary or '精度 ±1.26 mm' not in summary \
            or '2 个目标: 1 OK 1 NG' not in summary:
        raise AssertionError('摘要行不对: %r' % (summary,))
    # calib_ok=false -> 表里一个值都没有 + 红字原因 + 摘要写"不出测量值"
    uncal = parse_dim_json(
        (u'{"ok":true,"calib_ok":0,"calib_note":"标定不可信",'
         u'"state":"bad","targets":[{"name":"board_outline",'
         u'"value_mm":null,"nominal_mm":null,"err_mm":null,'
         u'"tol_mm":1.0,"ok":0}]}').encode('utf-8'))
    rows_uncal = format_dim_rows(uncal)
    if any('mm' in cell for row in rows_uncal for cell in row):
        raise AssertionError('calib_ok=false 时表里不该出现任何 mm: %r' % (rows_uncal,))
    if '不出测量值' not in format_dim_summary(uncal):
        raise AssertionError('calib_ok=false 的摘要不对: %r' % (format_dim_summary(uncal),))
    if dim_table_note(uncal) != '标定不可信':
        raise AssertionError('红字原因应为 calib_note: %r' % (dim_table_note(uncal),))
    # 旧板卡 -> 空表 + 摘要/红字写"版本过旧"
    old = parse_dim_json(b'', http_status=404)
    if format_dim_rows(old) != []:
        raise AssertionError('旧板卡应给空表')
    if '过旧' not in format_dim_summary(old) or '过旧' not in dim_table_note(old):
        raise AssertionError('旧板卡摘要/红字应提示版本过旧: %r / %r'
                             % (format_dim_summary(old), dim_table_note(old)))
    if dim_run_blocked_reason(old) != DIM_OLD_BOARD_HINT:
        raise AssertionError('旧板卡不该让点「测量一次」')
    if dim_run_blocked_reason({'supported': True, 'dim_available': 0,
                               'dim_module_error': 'import dim_measure failed'}) == '':
        raise AssertionError('没测量模块时「测量一次」应被拦')
    if dim_run_blocked_reason(dim) != '':
        raise AssertionError('正常状态「测量一次」应可点')
    # 精度与判定线分开：摘要用 sigma，不把 tol 当精度
    tol_only = parse_dim_json(b'{"ok":true,"calib_ok":1,"state":"ok","sigma_mm":null,'
                              b'"targets":[{"name":"board_outline","value_mm":89.0,'
                              b'"nominal_mm":89.0,"err_mm":0.0,"tol_mm":4.53,"ok":1}]}')
    if '精度 --' not in format_dim_summary(tol_only):
        raise AssertionError('sigma=null 时精度应显示 --（不能用 tol_mm 冒充）: %r'
                             % (format_dim_summary(tol_only),))
    return ('3 行文案(mm/真值/误差 mm+%/OK-NG) + 摘要(状态/精度/OK-NG 计数) + '
            'calib_ok=false 空表+红字 + 旧板卡降级 + 精度≠判定线 全部正确')


def _test_dim_url_build():
    """`/api/dim` URL 构造与"测量一次"的阻塞条件"""
    base = 'http://192.168.10.50:8000'
    cases = [((None, None), base + '/api/dim'),
             ((1, None), base + '/api/dim?run=1'),
             ((None, 1), base + '/api/dim?calib=1'),
             ((1, 1), base + '/api/dim?run=1&calib=1')]
    for (run, calib), expected in cases:
        got = url_dim(base, run=run, calib=calib)
        if got != expected:
            raise AssertionError('url_dim(%r, %r) = %r, 期望 %r'
                                 % (run, calib, got, expected))
    for bad in (2, -1, 'x'):
        try:
            url_dim(base, run=bad)
        except SerialCommandError:
            continue
        raise AssertionError('run=%r 非法值应抛 SerialCommandError' % (bad,))
    # 与 /api/status 的 15 字段没关系：/api/dim 是独立端点（这里只验 URL 不串到别的路径）
    if '/api/status' in url_dim(base) or url_dim(base).endswith('/api/status'):
        raise AssertionError('尺寸测量不该走 /api/status')
    return 'url_dim 4 组(只读/run/calib/两者) + 非法值拒绝 + 独立端点 全部正确'


def _test_dim_switch_text():
    """尺寸测量开关文案（只认回读）与取反语义"""
    if format_switch_text(SWITCH_DIM, 1) != '尺寸测量: 开':
        raise AssertionError('开启态文案不对: %r' % (format_switch_text(SWITCH_DIM, 1),))
    if format_switch_text(SWITCH_DIM, 0) != '尺寸测量: 关':
        raise AssertionError('关闭态文案不对: %r' % (format_switch_text(SWITCH_DIM, 0),))
    if format_switch_text(SWITCH_DIM, None) != '尺寸测量: --':
        raise AssertionError('未知态文案不对: %r'
                             % (format_switch_text(SWITCH_DIM, None),))
    if SWITCH_NAMES.get(SWITCH_DIM) != '尺寸测量':
        raise AssertionError('dim 的中文名应为"尺寸测量": %r' % (SWITCH_NAMES.get(SWITCH_DIM),))
    if next_switch_value(1) != 0 or next_switch_value(0) != 1 \
            or next_switch_value(None) is not None:
        raise AssertionError('取反语义不对（尺寸测量同样适用）')
    # detect 与 dim 的文案互不影响（各自认各自回的字段）
    if format_switch_text(SWITCH_DETECT, 0) != '目标检测: 关':
        raise AssertionError('detect 文案被污染: %r'
                             % (format_switch_text(SWITCH_DETECT, 0),))
    return '尺寸测量 开/关/-- 三态 + 与目标检测各认各的回读 全部正确'


def _test_dim_calib_line():
    """「重新标定」结果行（t26）：OK / 失败(红) / 标定中 / 缺字段 '--' 四态"""
    ok = {'supported': True, 'calib_ok': 1, 'sigma_mm': 0.42, 'state': 'ok',
          'calib_note': '孔位与已知布局一致'}
    text, level = format_dim_calib_line(ok)
    if text != '标定: OK  σ=0.42mm' or level != 'ok':
        raise AssertionError('标定成功的结果行不对: %r/%r' % (text, level))
    if dim_calib_color(level) != DIM_CALIB_OK_COLOR:
        raise AssertionError('成功应是绿字: %r' % (dim_calib_color(level),))
    # sigma 拿不到就 σ=--（不编数）
    text_ns, _lvl = format_dim_calib_line({'supported': True, 'calib_ok': 1,
                                          'sigma_mm': None, 'state': 'ok'})
    if text_ns != '标定: OK  σ=--':
        raise AssertionError('sigma 缺失时应显示 σ=--: %r' % (text_ns,))
    # 失败：把 calib_note 塞进括号，并且是红字
    bad = {'supported': True, 'calib_ok': 0, 'sigma_mm': None, 'state': 'bad',
           'calib_note': '孔位与已知布局不符'}
    text_bad, level_bad = format_dim_calib_line(bad)
    if text_bad != '标定: 失败(孔位与已知布局不符)' or level_bad != 'fail':
        raise AssertionError('标定失败的结果行不对: %r/%r' % (text_bad, level_bad))
    if dim_calib_color(level_bad) != DIM_CALIB_FAIL_COLOR:
        raise AssertionError('失败必须是红字: %r' % (dim_calib_color(level_bad),))
    if '失败' not in text_bad:
        raise AssertionError('失败文案必须含"失败": %r' % (text_bad,))
    # 标定中
    text_busy, level_busy = format_dim_calib_line({'supported': True, 'calib_ok': 0,
                                                   'state': 'calib'})
    if '标定中' not in text_busy or dim_calib_color(level_busy) != '':
        raise AssertionError('标定中不该染色: %r/%r' % (text_busy, dim_calib_color(level_busy)))
    # 旧固件/旧板卡没有 calib_ok 字段 -> '标定: --'，不报错、不染色
    for legacy in ({'supported': True}, {'supported': True, 'calib_ok': None,
                                         'state': 'idle'},
                   {}, None, {'supported': False}):
        text_old, level_old = format_dim_calib_line(legacy)
        if text_old != '标定: --' or level_old != 'none' or dim_calib_color(level_old) != '':
            raise AssertionError('%r 应显示 "标定: --"（不报错）: %r/%r'
                                 % (legacy, text_old, level_old))
    # 失败原因太长要截断（一行短文本）
    long_bad = format_dim_calib_line({'supported': True, 'calib_ok': 0,
                                      'calib_note': '孔' * 80})[0]
    if len(long_bad) > 60:
        raise AssertionError('失败原因太长没截断: %r' % (long_bad,))
    return ('OK(σ=0.42mm/σ=--)/失败(含原因+红字)/标定中/缺字段--/空桩 五种文案 + 颜色 全部正确')


def _test_dim_calib_blocked():
    """「重新标定」的可用性（t26）：dim=关/未知 -> 置灰；dim=开 -> 可点；旧板卡 -> 置灰"""
    on = {'supported': True, 'calib_ok': None, 'dim_available': 1}
    if dim_calib_blocked_reason(on, 1) != '':
        raise AssertionError('dim=开时「重新标定」应可点: %r'
                             % (dim_calib_blocked_reason(on, 1),))
    for off in (0, None, 'x'):
        reason = dim_calib_blocked_reason(on, off)
        if '尺寸测量没开' not in reason:
            raise AssertionError('dim=%r 时应提示"尺寸测量没开": %r' % (off, reason))
    if '没开' not in dim_calib_blocked_reason({}, None):
        raise AssertionError('dim 未知时也该提示"尺寸测量没开"')
    # 旧板卡（没有 /api/dim）-> 置灰并提示版本过旧
    if dim_calib_blocked_reason({'supported': False}, 1) != DIM_OLD_BOARD_HINT:
        raise AssertionError('旧板卡应提示版本过旧: %r'
                             % (dim_calib_blocked_reason({'supported': False}, 1),))
    # 只缺 calib_ok 字段（旧固件）**不算**点不了（需求 3）
    if dim_calib_blocked_reason({'supported': True}, 1) != '':
        raise AssertionError('只缺 calib_ok 时按钮应照样可点')
    return 'dim=开可点 / dim=关或未知->没开提示 / 旧板卡->版本过旧 / 缺 calib_ok 仍可点 全部正确'


def _test_dim_calib_url():
    """「重新标定」打的端点（t26）：必须走 /api/dim?calib=1，且不带 run（不阻塞测量）"""
    base = 'http://192.168.10.50:8000'
    url = url_dim(base, calib=1)
    if url != base + '/api/dim?calib=1':
        raise AssertionError('重新标定的 URL 不对: %r' % (url,))
    if 'run=' in url:
        raise AssertionError('重新标定不该带 run=1（那会同步测一次，阻塞 ~0.3s）: %r' % (url,))
    if '/api/config' in url:
        # 板卡 _config() 只认 autosave/detect/dim/dim_auto*，calib 会被忽略 -> 不能走这条
        raise AssertionError('重新标定不能走 /api/config（calib 参数会被板卡忽略）: %r' % (url,))
    # 既有语义不动：run / calib / 两者 都还能拼
    if url_dim(base) != base + '/api/dim':
        raise AssertionError('url_dim() 既有语义被改了')
    if url_dim(base, run=1) != base + '/api/dim?run=1':
        raise AssertionError('url_dim(run=1) 既有语义被改了')
    if url_dim(base, run=1, calib=1) != base + '/api/dim?run=1&calib=1':
        raise AssertionError('url_dim(run, calib) 组合被改了')
    return 'calib=1 走 /api/dim（不带 run、不走 /api/config）+ url_dim 既有语义不变 全部正确'


def _test_dim_calib_parse():
    """标定结果解析 + 降级：calib_ok/note/sigma 从 /api/dim 回读里来（t26）"""
    good = parse_dim_json(
        (u'{"ok":true,"schema":"dim-v1","calib_ok":1,"calib_note":"孔位一致",'
         u'"state":"ok","sigma_mm":0.42,"recalib_queued":1,"targets":[]}').encode('utf-8'))
    if good['calib_ok'] != 1 or good['sigma_mm'] != 0.42 or good['recalib_queued'] != 1:
        raise AssertionError('calib 回读字段解析错: %r' % (good,))
    if format_dim_calib_line(good)[0] != '标定: OK  σ=0.42mm':
        raise AssertionError('解析后的结果行不对: %r' % (format_dim_calib_line(good),))
    # 旧固件：整包没有 calib_ok -> None（界面显示 '--'，按钮照样可点，不抛异常）
    legacy = parse_dim_json(b'{"ok":true,"schema":"dim-v1","state":"ok",'
                            b'"targets":[{"name":"board_outline","value_mm":89.2,'
                            b'"nominal_mm":89.0,"err_mm":0.2,"tol_mm":4.5,"ok":1}]}')
    if legacy['calib_ok'] is not None:
        raise AssertionError('缺 calib_ok 时应为 None: %r' % (legacy['calib_ok'],))
    if format_dim_calib_line(legacy)[0] != '标定: --':
        raise AssertionError('缺 calib_ok 时应显示 标定: --')
    if dim_calib_blocked_reason(legacy, 1) != '':
        raise AssertionError('缺 calib_ok 时按钮应照样可点')
    # 404（真·旧板卡）：不抛异常、结果行 '--'
    old = parse_dim_json(b'404 not found', http_status=404)
    if format_dim_calib_line(old)[0] != '标定: --':
        raise AssertionError('旧板卡 404 时结果行应为 标定: --')
    return ('calib_ok/note/sigma/recalib_queued 解析 + 缺 calib_ok 降级为 -- + '
            '404 不抛异常 全部正确')


def _test_push_url_build():
    """远程推送 URL 构造：`/api/push?enabled&url&test`（不传参 = 只查询）"""
    base = 'http://192.168.10.50:8000'
    cases = [
        ((None, None, None), base + '/api/push'),
        ((1, None, None), base + '/api/push?enabled=1'),
        ((0, None, None), base + '/api/push?enabled=0'),
        ((None, 'http://10.0.0.2:9000/upload', None),
         base + '/api/push?url=http%3A%2F%2F10.0.0.2%3A9000%2Fupload'),
        ((None, None, 1), base + '/api/push?test=1'),
        # 参数顺序固定 enabled -> url -> test，方便日志/自检比对
        ((1, 'http://10.0.0.2:9000/upload', 1),
         base + '/api/push?enabled=1&url=http%3A%2F%2F10.0.0.2%3A9000%2Fupload&test=1'),
        (('1', 'https://pc:9000/upload', '1'),
         base + '/api/push?enabled=1&url=https%3A%2F%2Fpc%3A9000%2Fupload&test=1'),
    ]
    for (enabled, url, test), expected in cases:
        got = url_push(base, enabled=enabled, url=url, test=test)
        if got != expected:
            raise AssertionError('url_push(%r, %r, %r) = %r, 期望 %r'
                                 % (enabled, url, test, got, expected))
    for bad in (2, -1, 'x'):
        try:
            url_push(base, enabled=bad)
        except SerialCommandError:
            continue
        raise AssertionError('enabled=%r 非法值应抛 SerialCommandError' % (bad,))
    for bad in (2, -1, 'x'):
        try:
            url_push(base, test=bad)
        except SerialCommandError:
            continue
        raise AssertionError('test=%r 非法值应抛 SerialCommandError' % (bad,))
    # 手机监看地址 & 是不是合法 http(s) 地址
    if url_mobile(base) != base + '/m':
        raise AssertionError('手机页地址应为 <base>/m: %r' % url_mobile(base))
    if format_mobile_line(base) != '手机看: http://192.168.10.50:8000/m':
        raise AssertionError('手机监看行不对: %r' % format_mobile_line(base))
    for good in ('http://10.0.0.2:9000/upload', 'https://pc.example:9000/u'):
        if not is_http_url(good):
            raise AssertionError('%r 应被认成合法 http(s) 地址' % (good,))
    for bad in ('', '   ', 'ftp://10.0.0.2/u', '10.0.0.2:9000/upload', 'http://'):
        if is_http_url(bad):
            raise AssertionError('%r 不该被认成合法 http(s) 地址' % (bad,))
    # 默认地址：给 IP 时用它，拿不到 IP 时用占位
    if default_push_url('192.168.10.100') != 'http://192.168.10.100:9000/upload':
        raise AssertionError('默认推送地址不对: %r' % default_push_url('192.168.10.100'))
    if default_push_url('') != PUSH_URL_PLACEHOLDER:
        raise AssertionError('拿不到本机 IP 时应回退占位地址: %r'
                             % default_push_url(''))
    return ('url_push 7 组拼参(含 url 编码/参数顺序) + 非法值拒绝 + 手机页地址 + '
            'http(s) 校验 + 默认地址回退 全部正确')


def _test_push_parse():
    """`/api/push` 解析：GET 6 字段 / POST 响应 / ok 键两义 / 旧板卡 404 降级"""
    # ① GET 快照：**ok 是"成功推送次数"（整数）**，不是布尔
    state = parse_push_json(b'{"enabled":1,"url":"http://10.0.0.2:9000/upload",'
                           b'"queued":0,"ok":12,"fail":1,"last_error":""}')
    if state['enabled'] != 1 or state['queued'] != 0:
        raise AssertionError('GET 快照解析错: %r' % (state,))
    if state['count_ok'] != 12 or state['count_fail'] != 1:
        raise AssertionError('ok/fail 计数解析错（ok 整数=计数）: %r' % (state,))
    if state['ok'] is not True or state['supported'] is not True:
        raise AssertionError('GET 快照应视为请求成功+支持: ok=%r supported=%r'
                             % (state['ok'], state['supported']))
    if state['url'] != 'http://10.0.0.2:9000/upload' or state['last_error'] != '':
        raise AssertionError('url/last_error 解析错: %r' % (state,))
    # ② POST 响应：ok 是布尔，另外带 enabled/url/changed
    setres = parse_push_json(b'{"ok":true,"enabled":0,"url":"http://x/u",'
                             b'"changed":["enabled"]}')
    if setres['ok'] is not True or setres['enabled'] != 0:
        raise AssertionError('POST 响应解析错: %r' % (setres,))
    if setres['changed'] != ['enabled'] or setres['count_ok'] is not None:
        raise AssertionError('changed/计数解析错: %r' % (setres,))
    # ③ 缺字段（很旧的脚本只回 ok）：不报错，全部 None
    sparse = parse_push_json(b'{"ok":true}')
    if (sparse['enabled'], sparse['queued'], sparse['count_fail'],
            sparse['url'], sparse['last_error']) != (None, None, None, '', ''):
        raise AssertionError('缺字段时应全 None/空串: %r' % (sparse,))
    # ④ 旧板卡（没有这个路由）-> HTTP 404：不抛异常，supported=False
    old = parse_push_json(b'{"ok":false,"error":"not found"}', http_status=404)
    if old['supported'] is not False or old['ok'] is not False:
        raise AssertionError('404 应降级成 supported=False: %r' % (old,))
    if push_blocked_reason(old) != PUSH_OLD_BOARD_HINT:
        raise AssertionError('404 的置灰原因不对: %r' % push_blocked_reason(old))
    # 纯 404 空体（板卡直接回 HTML）也要降级，不能当 JSON 错误
    empty404 = parse_push_json(b'', http_status=404)
    if empty404['supported'] is not False or 'HTTP 404' not in empty404['error']:
        raise AssertionError('空 404 响应也应降级: %r' % (empty404,))
    # ⑤ 非法 url -> HTTP 400 bad url：error 要能拿到（界面提示行就靠它）
    bad = parse_push_json(b'{"ok":false,"error":"bad url"}', http_status=400)
    if bad['ok'] is not False or bad['error'] != 'bad url' or bad['http_status'] != 400:
        raise AssertionError('400 错误体解析错: %r' % (bad,))
    if bad['supported'] is not True:
        raise AssertionError('400 不是版本问题，不该把整组判成不支持: %r' % (bad,))
    # ⑥ test=1 的响应：test_queued=0 + warning
    tq = parse_push_json(b'{"ok":true,"enabled":1,"url":"http://x/u","test_queued":0,'
                         b'"warning":"not enabled or no frame"}')
    if tq['test_queued'] != 0 or 'no frame' not in tq['warning']:
        raise AssertionError('test=1 响应解析错: %r' % (tq,))
    # ⑦ 坏 JSON / 不是对象 -> ValueError（调用方按网络错误处理）
    for payload in (b'not json', b'[1,2]', b''):
        try:
            parse_push_json(payload)
        except ValueError:
            continue
        raise AssertionError('坏 /api/push 响应 %r 应该抛 ValueError' % (payload,))
    return ('GET 6 字段(ok=计数) + POST 响应 + ok 键两义 + 缺字段容错 + '
            '404 降级(旧板卡) + 400 bad url + test_queued + 坏 JSON 全部正确')


def _test_push_texts():
    """推送状态行 / 开关按钮文字 / last_error 行 / 置灰原因 的文案"""
    on = {'enabled': 1, 'queued': 0, 'count_ok': 12, 'count_fail': 0,
          'supported': True, 'ok': True}
    want_on = '推送: 开 ｜ 队列 0 ｜ 成功 12 ｜ 失败 0'
    if format_push_line(on) != want_on:
        raise AssertionError('推送开启状态行不对: %r' % (format_push_line(on),))
    if format_push_switch_text(on) != '推送: 开':
        raise AssertionError('开启按钮文字不对: %r' % (format_push_switch_text(on),))
    off = {'enabled': 0, 'queued': 3, 'count_ok': 0, 'count_fail': 2,
           'supported': True, 'ok': True}
    if format_push_line(off) != '推送: 关 ｜ 队列 3 ｜ 成功 0 ｜ 失败 2':
        raise AssertionError('推送关闭状态行不对: %r' % (format_push_line(off),))
    if format_push_switch_text(off) != '推送: 关':
        raise AssertionError('关闭按钮文字不对: %r' % (format_push_switch_text(off),))
    # 旧板卡（404 降级）：整组显示 --，且不标"开/关"
    old = {'supported': False, 'ok': False, 'error': 'HTTP 404'}
    if format_push_line(old) != '推送: -- ｜ 队列 -- ｜ 成功 -- ｜ 失败 --':
        raise AssertionError('旧板卡状态行应为 --: %r' % (format_push_line(old),))
    if format_push_switch_text(old) != '推送: --':
        raise AssertionError('旧板卡按钮文字应为 --: %r' % (format_push_switch_text(old),))
    if format_push_line(None) != '推送: -- ｜ 队列 -- ｜ 成功 -- ｜ 失败 --':
        raise AssertionError('format_push_line(None) 不应崩: %r' % (format_push_line(None),))
    # last_error：非空才给红字行
    err = dict(on, last_error='connection refused')
    if format_push_error_line(err) != '最近错误: connection refused':
        raise AssertionError('last_error 行不对: %r' % (format_push_error_line(err),))
    if format_push_error_line(on) != '':
        raise AssertionError('没有 last_error 时不该有小字: %r'
                             % (format_push_error_line(on),))
    if format_push_error_line(None) != '':
        raise AssertionError('format_push_error_line(None) 应为空串')
    # 置灰原因
    if push_blocked_reason(on) != '':
        raise AssertionError('正常状态下不该置灰')
    if '过旧' not in push_blocked_reason(old):
        raise AssertionError('旧板卡应提示版本过旧: %r' % (push_blocked_reason(old),))
    if '出错' not in push_blocked_reason({'ok': False, 'error': 'boom'}):
        raise AssertionError('板卡接口报错应给"出错"原因')
    if PUSH_OLD_BOARD_HINT != '板卡版本过旧（没有 /api/push）':
        raise AssertionError('旧板卡提示文案变了: %r' % (PUSH_OLD_BOARD_HINT,))
    return ('状态行 开/关/-- 三种 + 按钮文字 + last_error 红字 + 3 种置灰原因 全部正确')


def _test_push_toggle_state():
    """远程推送改成「开 / 关」两个按钮：点亮态只跟 /api/push 回读（t15）"""
    on = {'enabled': 1, 'supported': True, 'ok': True}
    off = {'enabled': 0, 'supported': True, 'ok': True}
    view_on = push_toggle_state(on)
    view_off = push_toggle_state(off)
    if not (view_on['on_checked'] and not view_on['off_checked']
            and view_on['usable'] and view_on['enabled'] == 1):
        raise AssertionError('enabled=1 时应该只点亮「开」且可点: %r' % (view_on,))
    if not (view_off['off_checked'] and not view_off['on_checked']
            and view_off['usable'] and view_off['enabled'] == 0):
        raise AssertionError('enabled=0 时应该只点亮「关」且可点: %r' % (view_off,))
    # 还不知道 / 旧板卡 / 回读失败 -> 两个都不点亮、都不让点
    for unknown in ({}, None, {'enabled': None, 'supported': True},
                    {'supported': False, 'ok': False}):
        view = push_toggle_state(unknown)
        if view['on_checked'] or view['off_checked'] or view['usable']:
            raise AssertionError('%r 时不该点亮/可点: %r' % (unknown, view))
    # 状态行/文字仍然只表达"板卡现在是什么样"
    if format_push_switch_text(on) != '推送: 开':
        raise AssertionError('开启态文字不对: %r' % (format_push_switch_text(on),))
    if format_push_switch_text({'supported': False}) != '推送: --':
        raise AssertionError('旧板卡文字应为 --: %r'
                             % (format_push_switch_text({'supported': False}),))
    # next_switch_value 仍按取反算（点「关」= 发 enabled=0）
    if next_switch_value(1) != 0 or next_switch_value(0) != 1 \
            or next_switch_value(None) is not None:
        raise AssertionError('next_switch_value 取反不对: %r/%r/%r'
                             % (next_switch_value(1), next_switch_value(0),
                                next_switch_value(None)))
    return '开/关两态点亮 + 未知/旧板卡都不点亮不可点 + 取反语义 全部正确'


def _test_push_url_issue():
    """[D29] 「目标 URL」形态检查：拦住"把手机页 /m 当推送目标"这类真机错误"""
    ok = ['http://192.168.10.100:9000/upload',
          'https://example.com/upload',
          'http://192.168.1.106:9000/upload/']
    for u in ok:
        if push_url_issue(u) != '':
            raise AssertionError('合法地址被拦: %r -> %r' % (u, push_url_issue(u)))
    bad = {
        '': '不能为空',
        '   ': '不能为空',
        '192.168.10.100:9000/upload': 'http://',
        'ftp://1.2.3.4/upload': 'http://',
        'http://192.168.43.158:8000/m': '/m',
        'http://192.168.43.158:8000/m/': '/m',
    }
    for u, need in bad.items():
        msg = push_url_issue(u)
        if not msg:
            raise AssertionError('该拦的地址没拦: %r' % (u,))
        if need not in msg:
            raise AssertionError('提示里应提到 %r，实际 %r' % (need, msg))
    return '空/无 scheme/手机页 /m 被拦；/upload 与前缀变体放行'


def _test_push_pending_expired():
    """[D29] 点开/关后等回读的「等待」必须有超时，否则板卡不确认时按钮永久置灰"""
    if push_pending_expired(0.0, 100.0) or push_pending_expired(None, 100.0):
        raise AssertionError('没在等待时不该超时')
    if push_pending_expired(100.0, 101.0):
        raise AssertionError('刚点下去 1s 不该超时（默认阈值 %ss）'
                             % (PUSH_PENDING_TIMEOUT,))
    if not push_pending_expired(100.0, 100.0 + PUSH_PENDING_TIMEOUT + 0.1):
        raise AssertionError('超过 %ss 必须超时，否则按钮永久置灰'
                             % (PUSH_PENDING_TIMEOUT,))
    if push_pending_expired('bad', 200.0) or push_pending_expired(100.0, 'bad'):
        raise AssertionError('脏数据不该抛异常，也不该判成超时')
    return '未等待/刚点下不超时；超过 %ss 超时；脏数据不崩' % (PUSH_PENDING_TIMEOUT,)


def _test_net_json_parse():
    """`GET /api/net` 解析：正常 / 空 / 缺字段 / 旧板卡 404 降级"""
    body = (b'{"ok":true,"ifs":[{"name":"wlx8c7ab3f81b37","addr":"192.168.10.50",'
            b'"up":1,"carrier":1,"wireless":1},'
            b'{"name":"end0","addr":"10.0.0.2","up":0,"carrier":0,"wireless":0}],'
            b'"urls":{"mobile":["http://192.168.10.50:8000/m","http://10.0.0.2:8000/m"]}}')
    net = parse_net_json(body)
    if not net['ok'] or len(net['ifs']) != 2 or net['supported'] is not True:
        raise AssertionError('/api/net 正常响应解析错: %r' % (net,))
    first = net['ifs'][0]
    if (first['name'] != 'wlx8c7ab3f81b37' or first['addr'] != '192.168.10.50'
            or first['wireless'] != 1 or first['up'] != 1 or first['carrier'] != 1):
        raise AssertionError('/api/net 第一块网卡字段解析错: %r' % (first,))
    if net['mobile'] != ['http://192.168.10.50:8000/m', 'http://10.0.0.2:8000/m']:
        raise AssertionError('/api/net mobile 列表解析错: %r' % (net['mobile'],))
    # 空 ifs / 缺字段 / 条目不是 dict / 没有 urls：都要能解析，不抛异常
    empty = parse_net_json(b'{"ok":true,"ifs":[],"urls":{"mobile":[]}}')
    if empty['ifs'] != [] or empty['mobile'] != [] or empty['ok'] is not True:
        raise AssertionError('空 ifs 解析错: %r' % (empty,))
    sparse = parse_net_json(b'{"ifs":[{"addr":"10.0.0.2"},"oops",{}]}')
    if len(sparse['ifs']) != 1 or sparse['ifs'][0]['name'] != '' \
            or sparse['ifs'][0]['wireless'] != 0 or sparse['ifs'][0]['up'] is not None:
        raise AssertionError('缺字段时默认值不对: %r' % (sparse['ifs'],))
    if sparse['mobile'] != []:
        raise AssertionError('没有 urls 时应给空列表: %r' % (sparse['mobile'],))
    # 旧板卡：没有 /api/net，404 回的是纯文本 -> supported=False，不抛异常
    old = parse_net_json(b'404 not found', http_status=404)
    if old['supported'] is not False or old['ok'] is not False or old['ifs'] != []:
        raise AssertionError('旧板卡 404 应降级: %r' % (old,))
    # 坏 JSON（200 却不是 JSON）-> ValueError（调用方按网络错误处理）
    for bad in (b'not json', b'[1,2]', b''):
        try:
            parse_net_json(bad)
        except ValueError:
            continue
        raise AssertionError('坏 /api/net 响应 %r 应该抛 ValueError' % (bad,))
    return ('正常两网卡 + 空 ifs + 缺字段默认值 + 旧板卡 404 降级 + 坏 JSON 全部正确')


def _test_net_choose_mobile_addr():
    """二维码地址挑选：无线优先；没有无线 -> 第一条有线 + 橙色警告；都没有 -> 空"""
    with_wifi = {'ifs': [{'addr': '10.0.0.2', 'wireless': 0},
                         {'addr': '192.168.10.50', 'wireless': 1}]}
    addr, wireless, warn = choose_mobile_addr(with_wifi)
    if addr != '192.168.10.50' or not wireless or warn:
        raise AssertionError('有无线时应挑无线且不警告: %r/%r/%r'
                             % (addr, wireless, warn))
    wired = {'ifs': [{'addr': '192.168.10.50', 'wireless': 0},
                     {'addr': '10.0.0.2', 'wireless': 0}]}
    addr, wireless, warn = choose_mobile_addr(wired)
    if addr != '192.168.10.50' or wireless or warn != QR_NO_WIRELESS_WARN:
        raise AssertionError('没有无线时应退第一条有线并警告: %r/%r/%r'
                             % (addr, wireless, warn))
    if QR_NO_WIRELESS_WARN != '板卡未连 WiFi：手机扫码可能打不开':
        raise AssertionError('警告文案变了: %r' % (QR_NO_WIRELESS_WARN,))
    for empty in ({}, None, {'ifs': []}, {'ifs': [{'addr': ''}]}):
        if choose_mobile_addr(empty) != ('', False, ''):
            raise AssertionError('%r 时应返回空地址不警告: %r'
                                 % (empty, choose_mobile_addr(empty)))
    return ('有无线优先 + 无无线橙色警告 + 空/缺字段不报警 全部正确')


def _test_push_url_for_board():
    """推送目标 URL 默认值：路由探测"板卡能到达的本机 IP"，拿不到才回退占位"""
    # 非法/空板卡地址 -> 探测失败 -> 占位地址（旧的 192.168.10.100 那台 PC）
    for bad in ('', None, 'not-an-ip', '256.256.256.256'):
        got = push_url_for_board(bad)
        if got != PUSH_URL_PLACEHOLDER:
            raise AssertionError('板卡地址 %r 时应用占位地址, 实际 %r'
                                 % (bad, got))
        if local_ip_for_board(bad) != '':
            raise AssertionError('板卡地址 %r 时不该探测出本机 IP' % (bad,))
    if PUSH_URL_PLACEHOLDER != 'http://192.168.10.100:9000/upload':
        raise AssertionError('占位地址变了: %r' % (PUSH_URL_PLACEHOLDER,))
    # 回环地址不算"板卡能到达的本机 IP"（路由探测会拿到 127.x, 直接判空）
    if local_ip_for_board('127.0.0.1') != '':
        raise AssertionError('回环地址不该当成本机对外 IP')
    # 探测得到 IP 时：URL 形如 http://<ip>:9000/upload（本机有默认路由才会命中）
    url = push_url_for_board('8.8.8.8')
    if not url.startswith('http://') or not url.endswith(':9000/upload'):
        raise AssertionError('探测成功时 URL 形状不对: %r' % (url,))
    if '://8.8.8.8' in url:
        raise AssertionError('URL 里不该出现板卡自己的地址: %r' % (url,))
    print('[selftest] 目标 URL 建议值: 板卡 8.8.8.8 -> %s（非法地址 -> %s）'
          % (url, PUSH_URL_PLACEHOLDER))
    return ('路由探测: 非法/空/回环都回退占位 + 正常时是"板卡能到达的本机 IP" 全部正确')


def _test_push_qr_optional():
    """二维码可选依赖：segno 优先、qrcode 退路、两个都没有 -> None（三种都不崩）"""
    text = 'http://192.168.10.50:8000/m'

    def _have(name):
        try:
            __import__(name)
            return True
        except Exception:
            return False

    have_segno, have_qrcode = _have('segno'), _have('qrcode')
    good = qr_png_bytes(text)
    if have_segno or have_qrcode:
        if not good or not good.startswith(b'\x89PNG'):
            raise AssertionError('装了 %s 时应画出 PNG 二维码, 实际 %r'
                                 % ('segno' if have_segno else 'qrcode',
                                    None if good is None else good[:8]))
        detail = ('有 %s -> 已画出 PNG 二维码 (%d 字节, 魔数 \\x89PNG)'
                  % ('segno' if have_segno else 'qrcode', len(good)))
    else:
        if good is not None:
            raise AssertionError('两个库都没有时应返回 None, 实际 %d 字节' % (len(good),))
        detail = 'segno/qrcode 都没装 -> 返回 None（界面显示占位文字+明文地址，不崩）'
    # 强制模拟"两个库都不可用"：sys.modules[name]=None 会让 import 直接 ImportError
    saved = {name: sys.modules.get(name) for name in ('segno', 'qrcode')}
    sys.modules['segno'] = None
    sys.modules['qrcode'] = None
    try:
        if qr_png_bytes(text) is not None:
            raise AssertionError('两个库都不可用时 qr_png_bytes 必须返回 None')
        print('[selftest] 二维码降级模拟: 两个库都不可用 -> 返回 None（只显示文字）')
    finally:
        for name, module in saved.items():
            if module is not None:
                sys.modules[name] = module
            else:
                sys.modules.pop(name, None)
    # 缺 segno 但有 qrcode（老环境）也要能画：单独恢复 qrcode 再试
    if have_qrcode:
        sys.modules['segno'] = None
        try:
            if not qr_png_bytes(text):
                raise AssertionError('只装了 qrcode 时应走退路画出来')
            print('[selftest] 二维码退路: 没有 segno 但装了 qrcode -> 仍能画出 PNG')
        finally:
            if saved['segno'] is not None:
                sys.modules['segno'] = saved['segno']
            else:
                sys.modules.pop('segno', None)
    if qr_png_bytes('') is not None or qr_png_bytes(None) is not None:
        raise AssertionError('空文本不该画二维码')
    print('[selftest] 二维码: %s' % detail)
    return detail


def _test_bitrate_unit_40():
    """目标码率单位 5→40 KB/s：Qt 只显示回读的 target_kbps，绝不自己乘单位"""
    if TARGET_STEP_KBPS != 40:
        raise AssertionError('TARGET_STEP_KBPS 应为 40（p20+ 每单位 40 KB/s）: %r'
                             % (TARGET_STEP_KBPS,))
    if list(BITRATE_PRESETS_KBPS) != [280, 320, 360, 400, 440]:
        raise AssertionError('B 键循环应为 280/320/360/400/440: %r'
                             % (BITRATE_PRESETS_KBPS,))
    if next_bitrate_preset(280) != 320 or next_bitrate_preset(440) != 280:
        raise AssertionError('next_bitrate_preset 循环不对: %r / %r'
                             % (next_bitrate_preset(280), next_bitrate_preset(440)))
    # 状态行直接照抄接口值：360 就显示 360，不做任何换算
    line = format_quality_line({'qsel': 2, 'auto': 1, 'target_kbps': 360, 'kbps': 311.2})
    want = '质量 Q75 (自适应)  目标 360 KB/s  实际 311.2 KB/s'
    if line != want:
        raise AssertionError('目标码率显示不对:\n  实际 %r\n  期望 %r' % (line, want))
    # 拿新脚本读旧位流会看到 5 KB/s/单位的小值（如 45）——界面**原样显示**，不乘 8
    legacy = format_quality_line({'qsel': 2, 'auto': 0, 'target_kbps': 45, 'kbps': 40.0})
    if '目标 45 KB/s' not in legacy:
        raise AssertionError('旧位流的小值也必须原样显示: %r' % (legacy,))
    if parse_status_json(b'{"target_kbps":440}')['target_kbps'] != 440:
        raise AssertionError('target_kbps=440 不应被界面改写')
    return ('单位 40 KB/s + 循环 280/320/360/400/440 + 状态行原样显示回读值'
            '(含旧位流小值 45) 全部正确')


def _test_slot_arg_trimming():
    """
    回归测试：Qt 的 clicked/triggered 会给槽函数多传一个 checked(False)。
    修复前会报 "takes 1 positional argument but 2 were given"（真机上踩过），
    这里不依赖 PyQt5，直接测 _slot 装饰器的参数裁剪。
    """
    calls = []

    class Dummy(object):
        @_slot
        def slot_noargs(self):
            calls.append('noargs')
            return 1

        @_slot
        def slot_onearg(self, value):
            calls.append(('onearg', value))
            return 2

        @_slot
        def slot_raises(self):
            raise ValueError('故意抛的异常要被 _slot 吞掉并打印')

    d = Dummy()
    # Qt clicked 的真实调用形态：多给一个 False
    assert d.slot_noargs(False) == 1, 'slot_noargs 应能接受 Qt 多给的 checked 参数'
    assert calls == ['noargs'], 'slot_noargs 调用次数不对'
    # 需要 1 个参数的槽：checked 要原样传进去
    assert d.slot_onearg(True) == 2
    assert calls[-1] == ('onearg', True), '带参槽应收到 Qt 传来的 checked'
    # 异常必须被 _slot 吃掉，返回 None，不向上抛
    assert d.slot_raises(False) is None, '_slot 应吞掉异常并返回 None'
    assert d.slot_raises() is None, '无参调用也应被保护'
    return 'clicked 多给的 checked 被正确裁剪；带参槽/异常保护都正常'


def run_selftest():
    """--selftest 入口：打印每一项 PASS/FAIL 并返回退出码（0=全过）"""
    print('=' * 78)
    print('pcb_station.py 自检 (不需要 PyQt5 / 板卡 / 串口硬件)')
    print('Python: %s' % sys.version.split()[0])
    try:
        import PIL
        print('pillow: %s' % getattr(PIL, '__version__', '?'))
    except Exception:
        print('pillow: 未安装（缩略图项会跳过；PC 上请 pip install pillow）')
    try:
        import serial
        print('pyserial: %s' % getattr(serial, '__version__', '?'))
    except Exception:
        print('pyserial: 未安装（GUI 串口功能需要 pip install pyserial）')
    print('PyQt5: %s' % ('已安装' if QT_OK else '未安装（本机正常现象，GUI 需要它）'))
    print('=' * 78)

    checks = [
        ('串口命令映射 (5 算法 + 阈值 ±)', _test_serial_commands),
        ('串口回传解析 (PRE-OK/MODE/TH/F)', _test_serial_reply_parse),
        ('串口收发线程 (假 pyserial, 不碰硬件)', _test_serial_worker_loop),
        ('URL 构造 (status/snap.jpg/stream/defects/events/api-snap)', _test_url_build),
        ('/api/status JSON 解析', _test_status_json),
        ('/api/defects JSON 解析', _test_defects_json),
        ('MJPEG 多段流解析', _test_mjpeg_parser),
        ('缩略图与下载路径', _test_thumbnail_and_paths),
        ('事件日志增量游标与格式 (/api/events)', _test_events_json_and_cursor),
        ('截图响应解析 (/api/snap)', _test_snap_json),
        ('SD 余量与字节格式化 (sd_free)', _test_human_and_sd_text),
        ('覆盖率/字节每块解析 (cover/b_per_blk)', _test_cover_status_parse),
        ('cover 分母=一帧满块数 32400 (t8 口径)', _test_cover_full_frame_blocks),
        ('覆盖率状态行文案与低抽样提醒 (≥25%/25%/缺失)', _test_cover_line_format),
        ('cover 是单窗口抽样率 (阈值 0.25 + 24.9/25.1 边界)', _test_cover_sampling_semantics),
        ('旧板卡缺 cover/b_per_blk 时降级', _test_cover_old_board_tolerance),
        ('压缩质量命令与连发规划 (Q/A/B)', _test_quality_commands),
        ('压缩质量回读与高亮映射 (qsel/auto/target_kbps)', _test_quality_status_and_highlight),
        ('功能开关 URL 与 /api/config 解析', _test_config_url_and_parse),
        ('功能开关取反/回读容错/置灰原因', _test_switch_state_and_tolerance),
        ('尺寸测量开关 dim 回读与互斥参数 (t19)', _test_dim_config_and_exclusive),
        ('尺寸测量开关文案与取反 (只认回读)', _test_dim_switch_text),
        ('尺寸测量「重新标定」结果行 (OK/失败/--/标定中)', _test_dim_calib_line),
        ('尺寸测量「重新标定」可用性 (dim=关/旧板卡/缺字段)', _test_dim_calib_blocked),
        ('尺寸测量「重新标定」端点 (/api/dim?calib=1)', _test_dim_calib_url),
        ('尺寸测量「重新标定」回读解析与降级 (t26)', _test_dim_calib_parse),
        ('尺寸测量结果解析 /api/dim (+旧板卡 404 降级)', _test_dim_json_parse),
        ('尺寸测量结果表渲染 (mm/真值/误差/OK-NG)', _test_dim_table_render),
        ('尺寸测量 URL 构造 (/api/dim?run|calib)', _test_dim_url_build),
        ('远程推送 URL 构造与手机监看地址 (/api/push)', _test_push_url_build),
        ('远程推送解析与旧板卡 404 降级 (parse_push_json)', _test_push_parse),
        ('推送状态行/按钮文字/last_error/置灰原因', _test_push_texts),
        ('推送开/关两按钮的点亮态 (只认回读)', _test_push_toggle_state),
        ('推送目标 URL 形态检查 (拦手机页 /m 与无 scheme)', _test_push_url_issue),
        ('推送等待回读的超时 (板卡不确认不会永久置灰)', _test_push_pending_expired),
        ('网卡列表解析 /api/net (+旧板卡 404 降级)', _test_net_json_parse),
        ('二维码地址挑选 (无线优先/无 WiFi 橙色警告)', _test_net_choose_mobile_addr),
        ('推送目标 URL 默认值 (路由探测/占位回退)', _test_push_url_for_board),
        ('二维码可选依赖 (segno 优先/qrcode 退路/都缺降级)', _test_push_qr_optional),
        ('目标码率单位 40 KB/s 与 B 键循环 (显示只认回读)', _test_bitrate_unit_40),
        ('接口回调参数裁剪 (Qt clicked 多传 checked)', _test_slot_arg_trimming),
        ('HTTP 端到端 (假板卡 127.0.0.1)', _test_http_roundtrip),
    ]
    passed = 0
    failed = []
    for name, func in checks:
        try:
            detail = func()
        except Exception as exc:
            failed.append(name)
            print('[FAIL] %s' % name)
            print('       原因: %s: %s' % (type(exc).__name__, exc))
            traceback.print_exc()
        else:
            passed += 1
            print('[PASS] %s — %s' % (name, detail or 'ok'))
    print('-' * 78)
    if failed:
        print('[FAIL] 自检未通过 %d/%d 项: %s' %
              (len(failed), len(checks), '; '.join(failed)))
        return 1
    print('[PASS] 全部 %d 项自检通过 (SELFTEST PASS)' % passed)
    print('提示: 本自检只覆盖协议/解析/网络等纯逻辑（不需要 PyQt5）。')
    print('      要验证界面渲染与交互，请跑: python tools/qt_headless_smoketest.py')
    print('      （需要 PyQt5；离屏运行，不弹窗口）')
    return 0


# ============================================================================
# 11. main
# ============================================================================

def parse_args(argv):
    parser = argparse.ArgumentParser(
        description='PCB 缺陷检测上位机（串口切 FPGA 算法 + MJPEG 预览 + 缺陷图墙）')
    parser.add_argument('--selftest', action='store_true',
                        help='只跑自检（不需要 PyQt5/板卡/串口），打印 PASS/FAIL 并返回退出码')
    parser.add_argument('--ip', default=DEFAULT_BOARD_IP,
                        help='板卡 IP（默认 %s）' % DEFAULT_BOARD_IP)
    parser.add_argument('--port', default=str(DEFAULT_BOARD_PORT),
                        help='板卡 HTTP 端口（默认 %d）' % DEFAULT_BOARD_PORT)
    parser.add_argument('--serial', default=None,
                        help='启动后自动连接的串口, 如 COM5')
    parser.add_argument('--downloads', default=DEFAULT_DOWNLOAD_DIR,
                        help='缺陷图保存目录（默认 ./downloads）')
    parser.add_argument('--no-net', action='store_true',
                        help='启动时不自动连板卡（只看界面/只用串口）')
    return parser.parse_args(argv)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)
    if args.selftest:
        return run_selftest()
    if not QT_OK:
        print('[ERR] 没有可用的 PyQt5，界面无法启动。')
        print('[ERR] 请在 PC 上执行: pip install PyQt5 pillow pyserial')
        print('[ERR] 具体导入错误: %s' % QT_ERROR)
        print('[提示] 只想验证逻辑可以跑: python pc_qt/pcb_station.py --selftest')
        return 2
    try:
        app = QApplication([sys.argv[0]])
        window = MainWindow(ip=args.ip, port=args.port,
                            download_dir=args.downloads,
                            serial_port=args.serial,
                            auto_net=not args.no_net)
        if args.serial:
            window._on_serial_button()
        window.show()
        return app.exec_()
    except Exception:
        print('[FATAL] 界面启动失败，请把下面的报错完整贴回给开发者：')
        traceback.print_exc()
        return 3


if __name__ == '__main__':
    sys.exit(main())
