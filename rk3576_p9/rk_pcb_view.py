#!/usr/bin/env python3
"""
rk3576_yolov5s_relu.py
YOLOv5s-Relu 实时推理 — 适配 RK3576 + MIPI 输入(/dev/video22)
模型输出: 3 个原始特征图 (1,255,80/40/20,80/40/20)，需按 anchors 解码
OpenCV 支持 GUI 则弹居中窗口，否则定时存图兜底
"""

try:
    import cv2
except ImportError:          # --selftest / --http-selftest 在本机跑时没有 cv2
    cv2 = None
import base64
import contextlib
import io
import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import numpy as np
from collections import deque
from urllib.parse import unquote
import socket
import urllib.error
import urllib.request
try:
    from rknnlite.api import RKNNLite
except ImportError:
    RKNNLite = None

DEVICE       = '/dev/video22'
WIDTH        = 1920
HEIGHT       = 1080
FPS          = 30
MODEL_PATH   = 'yolov5s_relu.rknn'
IMG_SIZE     = 640
SAVE_EVERY   = 30
SAVE_PATH    = '/home/rock/yolo/det.jpg'

CLASS_NAMES = [
    'person', 'bicycle', 'car', 'motorcycle', 'airplane', 'bus', 'train', 'truck',
    'boat', 'traffic light', 'fire hydrant', 'stop sign', 'parking meter', 'bench',
    'bird', 'cat', 'dog', 'horse', 'sheep', 'cow', 'elephant', 'bear', 'zebra',
    'giraffe', 'backpack', 'umbrella', 'handbag', 'tie', 'suitcase', 'frisbee',
    'skis', 'snowboard', 'sports ball', 'kite', 'baseball bat', 'baseball glove',
    'skateboard', 'surfboard', 'tennis racket', 'bottle', 'wine glass', 'cup',
    'fork', 'knife', 'spoon', 'bowl', 'banana', 'apple', 'sandwich', 'orange',
    'broccoli', 'carrot', 'hot dog', 'pizza', 'donut', 'cake', 'chair', 'couch',
    'potted plant', 'bed', 'dining table', 'toilet', 'tv', 'laptop', 'mouse',
    'remote', 'keyboard', 'cell phone', 'microwave', 'oven', 'toaster', 'sink',
    'refrigerator', 'book', 'clock', 'vase', 'scissors', 'teddy bear',
    'hair drier', 'toothbrush'
]

OBJ_THRESH  = 0.35
NMS_THRESH  = 0.45

MASKS   = [[0, 1, 2], [3, 4, 5], [6, 7, 8]]
ANCHORS = [[10, 13], [16, 30], [33, 23], [30, 61], [62, 45],
           [59, 119], [116, 90], [156, 198], [373, 326]]


# ============================================================================
# [我们加的] 视频水印解码 —— FPGA 把 {自适应, 目标码率, 质量档, 已编码块数,
# 压缩字节数, 帧计数} 编成 64bit 画在每帧左上角 512x8 像素
# (每 bit 占 8x8, 白=1 黑=0, 左=MSB)。
# 原因: FPGA 与 RK 之间只有单向 MIPI, 没有别的数据回传通道。
# 布局(bit): [63]auto [62:58]target5 [57:56]qsel [55:40]块数 [39:16]字节数
#            [15:8]帧计数 [7:0]0xA5
#   auto      1=自适应码率, 0=手动
#   target5   目标码率 = target5 x TARGET_STEP_KBPS(见下); 0 表示未设置/旧位流(p19 及以前)
#   qsel      当前档位 0/1/2/3 -> Q30/Q50/Q75/Q90
#   blocks    当前采集窗口已编码块数(窗口回绕时清零)
#   nbytes    当前采集窗口已压缩字节数(**窗口累计值, 不是每帧增量**! 回绕时清零,
#             所以码率要用「相邻样本差 + 回绕检测」算, 别把累计值直接相加)
# 压缩率 = 1 - 压缩字节数/(块数x64)
# 兼容: 旧 FPGA 位流把 [63:58] 当保留位恒 0 -> auto=0(手动)、target_kbps=0
#       (上位机显示 "--"), 不会解错也不会报错。
# ============================================================================
ROI_W, ROI_H, CELL = 512, 8, 8
WM_MAGIC = 0xA5
QNAME = {0: "Q30", 1: "Q50", 2: "Q75", 3: "Q90"}
# target5 每个 LSB 代表多少 KB/s —— **单位跟位流版本绑定**:
#   p20 起(含 p20): 1 个 LSB = 40 KB/s, 即 target5 = 7/8/9/10/11 -> 280/320/360/400/440 KB/s,
#                   位流复位默认 target5=9 -> **360 KB/s**(HUD/上位机应显示 360)
#   p19 及以前:      1 个 LSB = 5 KB/s (例如 target5=4 表示 20 KB/s)
# ⚠ 用本脚本去读 **p19 及以前** 的位流, target_kbps 会**放大 8 倍**(40/5), 现场看到
#   "目标 160 KB/s 而实际只有 20 KB/s" 就是这个原因; 判断办法见启动日志的 [TGT] 行。
TARGET_STEP_KBPS = 40             # target5 每 1 个 LSB = 40 KB/s (p20+ 位流)
TGT_RESET_5 = 9                   # p20+ 位流复位默认 target5
TGT_RESET_KBPS = TGT_RESET_5 * TARGET_STEP_KBPS   # = 360 KB/s (复位默认显示值)
# 一帧满块数 = 240x135 个 8x8 亮度块 = 32400(FPGA 每条带 240 块 x 135 条带)。
# cover 的分母必须是「**一帧**」而不是「一个采集窗口」: 水印里的 blocks 是**编码工作量**
# (窗口内累计编了多少块), 拿 4 帧窗口的块数(129600)当分母, 会把 p19 轮转后的
# 9960 块显示成 7.7%, 而真实单窗口抽样率是 9960/32400 ≈ 30.7%。见 frame_cover。
FULL_FRAME_BLOCKS = 240 * 135


def decode_wm(frame):
    if frame is None or frame.shape[0] < ROI_H or frame.shape[1] < ROI_W:
        return None
    roi = frame[0:ROI_H, 0:ROI_W]
    if roi.ndim == 3:
        g = (0.114 * roi[:, :, 0].astype(np.float32)
             + 0.587 * roi[:, :, 1].astype(np.float32)
             + 0.299 * roi[:, :, 2].astype(np.float32))
    else:
        g = roi.astype(np.float32)
    means = np.array([g[:, i * CELL:(i + 1) * CELL].mean() for i in range(64)],
                     dtype=np.float32)
    lo, hi = float(means.min()), float(means.max())
    if hi - lo < 20.0:
        return None
    thr = (lo + hi) / 2.0
    val = 0
    for i in range(64):
        val = (val << 1) | (1 if means[i] > thr else 0)
    return val


def parse_wm(val):
    """64bit 水印 -> dict。qsel/blocks/nbytes/fcnt 位置与旧位流完全一致,
    只是把原保留位 [63:58] 用起来做 auto/target5"""
    if val is None or (val & 0xFF) != WM_MAGIC:
        return None
    blocks = (val >> 40) & 0xFFFF
    nbytes = (val >> 16) & 0xFFFFFF
    target5 = (val >> 58) & 0x1F
    return dict(qsel=(val >> 56) & 0x03, blocks=blocks, nbytes=nbytes,
                fcnt=(val >> 8) & 0xFF,
                ratio=(1.0 - nbytes / (blocks * 64.0)) if blocks else None,
                auto=(val >> 63) & 0x01,
                target5=target5,
                target_kbps=target5 * TARGET_STEP_KBPS)


def frame_cover(blocks):
    """**单窗口抽样率**: 本窗口编码块数 / **一帧**满块数(240*135 = 32400), 0~1。

    含义: 「这一个采集窗口采到了整幅图多大面积」。p19(条带轮转 ROT_STEP=13)后
    一个 4 帧窗口约 41.5 条带 ≈ 9960 块 -> 9960/32400 ≈ 0.307(30.7%),
    与 RTL 仿真「4 帧走 41.5/135 条带」一致。

    ⚠ 前提: **条带不重复**。p19 轮转后成立; 旧位流(p18)每帧都编同一批 13.5 条带,
    4 帧窗口累计 12960 块 -> 12960/32400 = 0.40, 这是**重复编码 4 次**造成的虚高,
    不是公式错 —— 换回旧位流看到 40% 别当成"覆盖变好了"。

    口径边界: 这是**单窗口**值, **不是**「整幅图已覆盖率」—— 跨帧看约 12 帧覆盖 92%、
    约 13 帧走满 100%, 那是另一个口径(按帧累计), 不要混着报。

    仍是**估算**: FPGA 块计数是 8x8 亮度块的口径(240 块/条带), 采到的也未必是
    满 1080p, 只用来判断量级与趋势, 不要当精确数值报数。
    """
    try:
        n = int(blocks or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0.0
    return min(1.0, n / float(FULL_FRAME_BLOCKS))


def bytes_per_block(nbytes, blocks):
    """字节/块 = nbytes/blocks(越小压缩越狠); blocks=0(这一窗口没编码)时 None"""
    try:
        n = int(blocks or 0)
        b = int(nbytes or 0)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return b / float(n)


class WmSmoother(object):
    """编码器比视频慢, 很多帧 blocks=0; 保持上次有效值并估计码率

    【码率口径修正(t1)】水印里的 nbytes 是「**当前采集窗口累计**字节数」, 不是
    每帧增量: 一个窗口内它从 0 一路涨到窗口总字节, 然后回绕归零重新累计。
    老写法把窗口内所有样本的 nbytes 直接相加 => 大 3~4 倍
    (实测板子上显示 906 KB/s, 按 ratio 反算真值约 311 KB/s)。
    现在改成「相邻样本差 + 回绕检测」:
        n1 >= n0 : 同一个窗口   -> 增量 = n1 - n0
        n1 <  n0 : 窗口回绕了   -> 增量 = n1 (新窗口已经累计的字节)
    每个增量带时间戳进滑动账本, 只保留最近 win 秒(now - t > win 才丢),
    窗口累计 = 账本里的增量之和, 码率 = 窗口累计 / 实际时间跨度 / 1024。
    blocks=0 的帧不采样(编码器比视频慢, 那些帧没有新窗口)。
    """

    def __init__(self, win=3.0):
        self.last = None
        self.samples = deque()      # [(t, nbytes)] 采样点(只留 win 秒, 排障用)
        self.deltas = deque()       # [(t, 真实增量字节)] 滑动窗口账本
        self.win = float(win)
        self.strip_cnt = 0
        self.prev = None            # 上一个有效样本 (t, nbytes), 算增量用
        self.bytes = 0.0            # 最近一次入账/裁剪后的窗口累计字节

    def _trim(self, now):
        while self.samples and now - self.samples[0][0] > self.win:
            self.samples.popleft()
        while self.deltas and now - self.deltas[0][0] > self.win:
            self.deltas.popleft()

    def update(self, info, now):
        if info and info.get("ratio") is not None:
            try:
                n = int(info.get("nbytes", 0) or 0)
            except (TypeError, ValueError):
                n = 0
            prev = self.prev
            self.prev = (now, n)
            if prev is not None:
                pn = int(prev[1] or 0)
                # n < pn => FPGA 开了新窗口(从 0 重新累计), 不能算成负增量
                d = (n - pn) if n >= pn else n
                if d > 0:
                    self.deltas.append((now, float(d)))
            self.last = info
            self.samples.append((now, n))
            self.strip_cnt += 1
            self._trim(now)
            self.bytes = float(sum(d for _, d in self.deltas))
        return self.last

    def window_bytes(self):
        """最近 win 秒内的**真实字节增量**累计(离线自检/排障用, 不依赖 sleep)"""
        return float(sum(d for _, d in self.deltas))

    def kbps(self):
        """码率 KB/s = win 秒内真实字节增量 / 实际时间跨度 / 1024"""
        if not self.deltas:
            return 0.0
        span = self.samples[-1][0] - self.deltas[0][0]
        if span < 1e-3:
            return 0.0
        return sum(d for _, d in self.deltas) / span / 1024.0


def draw_wm_hud(frame, info, fps, kbps, show=True):
    """盖掉噪点条纹 + 左上角画压缩率面板"""
    if not show:
        return frame
    if frame.shape[0] >= 2 * ROI_H:
        frame[0:ROI_H, 0:ROI_W] = frame[ROI_H:2 * ROI_H, 0:ROI_W].copy()
    pw, ph = 620, 170
    panel = frame[0:ph, 0:pw].copy()
    cv2.rectangle(panel, (0, 0), (pw, ph), (0, 0, 0), -1)
    frame[0:ph, 0:pw] = cv2.addWeighted(panel, 0.55, frame[0:ph, 0:pw], 0.45, 0)

    ok = info is not None and info.get("ratio") is not None
    pct = (info["ratio"] * 100.0) if ok else 0.0
    qn = QNAME.get(info["qsel"], "?") if info else "?"
    col = (0, 255, 0) if ok else (0, 0, 255)
    cv2.putText(frame, "FPGA HARDWARE JPEG", (16, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (200, 200, 200), 2)
    cv2.putText(frame, "COMPRESSION %5.1f%%" % pct, (16, 80),
                cv2.FONT_HERSHEY_SIMPLEX, 1.15, col, 3)
    bx, by, bw, bh = 20, 100, 540, 18
    cv2.rectangle(frame, (bx, by), (bx + bw, by + bh), (70, 70, 70), -1)
    fill = int(bw * min(max(pct / 100.0, 0.0), 1.0))
    if fill:
        cv2.rectangle(frame, (bx, by), (bx + fill, by + bh), col, -1)
    tick = bx + int(bw * 0.30)
    cv2.line(frame, (tick, by - 4), (tick, by + bh + 4), (200, 200, 0), 2)
    # 30% 是赛题(CAT-3 简单级)压缩率的及格线, 标清楚免得当成"我们的目标"
    cv2.putText(frame, "CAT-3 30%", (tick - 14, by + bh + 22),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 0), 1)
    # 第三行: 自动/手动 + 当前档位 + (只有 auto 才有意义)目标码率
    #         + 字节/块 + 覆盖率估算 + 码率 + 帧率
    # ⚠ cv2 的 Hershey 字体不支持中文, 只能用 ASCII
    _auto = bool(info.get("auto")) if info else False
    _tgt = info.get("target_kbps") if info else None
    _blocks = int(info.get("blocks") or 0) if info else 0
    _nbytes = int(info.get("nbytes") or 0) if info else 0
    _tag = ("AUTO " if _auto else "MAN  ") + qn
    _bp = bytes_per_block(_nbytes, _blocks)
    _bp_s = ("%5.1fB/blk" % _bp) if _bp is not None else "  -- B/blk"
    # MAN 模式下目标码率根本不起作用 -> 不显示 TGT
    # (免得出现"目标 80 KB/s 但手动模式毫无作用"的困惑)
    # auto=1 但 target5=0(p20 位流不会出现, 只有异常/半通才可能) -> 显示复位默认 360KB/s,
    # 这就是"复位默认显示 360"的落点; /api/status 的 target_kbps 仍然老老实实报 0(不动契约)
    _tgt_show = _tgt if _tgt else TGT_RESET_KBPS
    _tgt_s = ("  " + ("TGT%3dKB/s" % _tgt_show)) if _auto else ""
    cv2.putText(frame, "%s%s  %s  COV%5.1f%%  %5.1fKB/s  %4.1f fps"
                % (_tag, _tgt_s, _bp_s, frame_cover(_blocks) * 100.0, kbps, fps),
                (16, 158), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (220, 220, 220), 2)
    return frame


def _wm_selftest():
    val = ((2 & 3) << 56) | ((240 & 0xFFFF) << 40) | \
          ((2216 & 0xFFFFFF) << 16) | ((7 & 0xFF) << 8) | WM_MAGIC
    img = np.full((1080, 1920, 3), 60, dtype=np.uint8)
    for i in range(64):
        bit = (val >> (63 - i)) & 1
        img[0:ROI_H, i * CELL:(i + 1) * CELL] = 235 if bit else 16
    got = decode_wm(img)
    info = parse_wm(got)
    exp = (1 - 2216 / (240 * 64.0)) * 100
    cov = frame_cover(info["blocks"]) if info else 0.0
    bpb = bytes_per_block(info["nbytes"], info["blocks"]) if info else None
    okk = (got == val) and info is not None and abs(info["ratio"] * 100 - exp) < 0.05
    print("[selftest] 0x%016X -> %s" % (val, info))
    print("[selftest] 压缩率应为 %.1f%%, 解得 %.1f%%" %
          (exp, info["ratio"] * 100 if info else -1))
    print("[selftest] 单窗口抽样估算 %.4f (块数/%d, 一帧满块数), 字节/块 %s"
          % (cov, FULL_FRAME_BLOCKS,
             ('%.2f' % bpb) if bpb is not None else 'None'))
    print("[selftest] %s" % ("PASS" if okk else "FAIL"))
    return 0 if okk else 1


# ============================================================================
# [我们加的] 板卡 HTTP 服务 —— 让 PC 上位机通过网线取实时画面 / 压缩率 / 缺陷图
#
#   · /dev/video22 只能被打开一次 => 服务和主循环**同一个进程**,
#     主循环帧末 publish 最新帧, HTTP 线程只读（不再开 VideoCapture）
#   · JPEG 编码全部在 HTTP 线程里做, 且限频 HTTP_JPEG_MAX_FPS(10) 张/秒
#     => 不拖慢推理主循环
#   · 契约（PC 端 pcb_station.py 按此实现, 字段名不要改）:
#       GET /                状态页 HTML(压缩率/帧率/码率, 内嵌 /stream.mjpg)
#       GET /api/status      15 个字段: {"ok":true,"ratio":0.856,"kbps":311.2,"qsel":2,
#                             "fps":14.0,"blocks":9960,"nbytes":77688,
#                             "cover":0.3074,"b_per_blk":7.8,
#                             "sd_free":123456789,"save_dir":"/mnt/sdcard/defect",
#                             "auto":1,"target_kbps":20,
#                             "autosave":1,"detect":1}
#                            ratio 为 0~1 小数(=HUD 上的百分数/100), 无数据时 null
#                            kbps: 真实码率 KB/s —— 3 秒滑窗里累计「nbytes 的相邻样本差」
#                                  (nbytes 回绕变小即视为新窗口), 不再把窗口累计值相加
#                            cover: 本窗口编码块数 / **一帧**满块数(240*135 = 32400),
#                                  0~1 = "本窗口采到整幅图多大面积"(**单窗口**抽样率,
#                                  不是整幅图覆盖率: 跨帧约 13 帧才走满);
#                                  前提是条带不重复 —— p19 轮转后 9960 块 = 30.7%,
#                                  旧位流重复编同一批条带会虚高到 0.40; **估算**值
#                            b_per_blk: 字节/块 = nbytes/blocks; blocks=0 时 null
#                            auto: 1=自适应 0=手动; target_kbps: 目标码率(0=未设置/旧位流)
#                            autosave: 1=检出就自动存图 0=不自动存(手动截图仍可用)
#                            detect  : 1=跑 YOLO 推理  0=跳过推理(无框、帧率上升)
#       GET /api/config?autosave=0|1&detect=0|1&dim=0|1   运行时开关(t11/t18, 改完立刻生效):
#                            {"ok":true,"autosave":0,"detect":1,"dim":0,"changed":["detect"]}
#                            不传参数=只查询; 只改传了的那个; 非法值 400
#                            {"ok":false,"error":"bad autosave=xx"}
#                            detect=1 但没模型 -> ok:true + "warning":"no model loaded"
#                            (detect 保持 0)
#                            **detect 与 dim 互斥(t18)**: dim=1 自动 detect=0,
#                            detect=1 自动 dim=0; 被自动关掉的那项也出现在 changed 里,
#                            所以从回读的 changed + dim/detect 就能看出互斥结果
#                            dim=1 但板上没 dim_measure.py(或缺 scipy) ->
#                            ok:true + "warning":"dim module not loaded"(dim 保持 1)
#       GET /api/dim[?calib=1]  尺寸测量结果(t18), schema 由 tools/dim_measure.py 冻结:
#                            {"ok":true,"schema":"dim-v1","calib_ok":true,
#                             "calib_note":"可信：...","sigma_mm":1.2,
#                             "accuracy_mm":1.2,"tol_kind":"judge_line",
#                             "state":"ok","scale_mm_per_px":0.026,
#                             "dim_available":true,"dim_module_error":"",
#                             "calib_ts":1792...,"recalib_queued":0,
#                             "perf_ms":{"warp":3.8,"targets":90.0,"measure":96.0,
#                                        "loop_total":97.0,"fps":14.0},
#                             "targets":[{name,value_mm,nominal_mm,err_mm,tol_mm,
#                                         ok,corners,sides,diag,area_mm2,
#                                         (kind,short_mm,why)}...]}
#                            · 顺序固定 board_outline, hole_pitch —— **只有这两项**
#                              (2026-10-26 S1: 蓝牙模块尺寸停测, 电阻/电容更早已停测);
#                              value_mm/err_mm/corners/sides/diag/area_mm2 都**允许 null**
#                            · **calib_ok=false 时所有 value_mm 必须 null**, 只给 calib_note
#                            · **tol_mm 是判定线(±0.3 mm 或 5%), 不是精度**; 精度看
#                              sigma_mm/accuracy_mm(当前 1.0~1.3 mm 量级, 拿不到就是 null)
#                            · state: idle(还没标定)/calib(标定中)/ok(已标定)/bad(不可信)
#                              —— HUD 用这三态显示
#                            · ?calib=1 用最新帧后台重标一次(不阻塞主循环/HTTP)
#                            · /api/status 的 15 个字段**不含**任何测量信息(契约不动)
#       GET /api/events?since=N  {"next":M,"events":[{seq,t,type,file,size,cls,
#                            score,ratio,fps,mode}]}  (t4)
#       GET /api/snap        {"ok":true,"file":"manual_HHMMSS.jpg","size":..,"dir":..} (t4)
#       GET /snap.jpg        当前帧 JPEG (image/jpeg)
#       GET /stream.mjpg     multipart/x-mixed-replace, boundary 固定为 "frame"
#       GET /api/defects     {"files":[{"name":"det_142233.jpg","size":..,"mtime":..}]}
#       GET /defects/<name>  下载单张缺陷图(只允许 save-dir 内的 basename)
#       GET /api/net         本机网卡 + 手机页地址(t14, 给 Qt 的二维码取地址用):
#                            {"ok":true,
#                             "ifs":[{"name":"end0","addr":"192.168.10.50",
#                                     "up":1,"carrier":1,"wireless":0},
#                                    {"name":"wlx8c7ab3f81b37","addr":"192.168.43.158",
#                                     "up":1,"carrier":1,"wireless":1}],
#                             "urls":{"mobile":["http://192.168.43.158:8000/m",
#                                               "http://192.168.10.50:8000/m"]}}
#                            · 只列 IPv4, 排除 lo / 0.0.0.0 / 127.*
#                            · wireless: 网卡名 wl*(含 USB WiFi 的 wlx<mac>) 或 wlan*
#                            · up/carrier: 读 /sys/class/net/<if>/{operstate,carrier};
#                              读不到就是 null(不是 0)
#                            · urls.mobile **无线优先**, 端口 = 服务实际监听端口, 不带尾斜杠
#                            · 没有网卡时 ifs=[] 且 ok:true(绝不报错)
#       GET /m               手机低带宽页(t22): 大字号(本机各网卡 IP/压缩率/fps/档位/cover)
#                            + <img src="/stream.mjpg?q=40&w=640&maxfps=5">
#                            + 缺陷缩略图墙(/api/defects 前 6 张) + 最近 10 条事件
#                            + 命中新 detect 时顶部红条 3 秒 + WebAudio 一声 beep
#                            纯内联 HTML/JS, 不依赖外网 CDN
#       GET/POST /api/push   板卡**主动推**缺陷图/事件到远端 HTTP 接收端(t22):
#                             GET           -> {"enabled":0|1,"url":"...","queued":N,
#                                               "ok":N,"fail":N,"last_error":"..."}
#                             ?enabled=0|1  开关(只关发送, 线程不销毁)
#                             ?url=<http(s)://...>  远端地址(非 http/https -> 400 bad url)
#                             ?test=1       立刻推一张测试图(进同一个队列)
#                             POST 参数可放 query 也可放 form body; 不传参=只查询
#                             返回 {"ok":true,"enabled":..,"url":..,"changed":[...]}
#                             推送内容: POST body=JPEG 原始字节, 头 X-Event=<一行 JSON>
#                                       (seq,type,cls,score,ratio,fps,cover,box),
#                                       Content-Type: image/jpeg, 超时 3s, 失败重试 1 次
#                             · 有界队列 8: 满了**丢最旧**并 fail+1, 绝不阻塞主循环
#                             · 触发: 自动存缺陷图(autosave=1)后 type=detect,
#                                     /api/snap 成功后 type=manual
#       GET /snap.jpg、/stream.mjpg 的**低带宽参数**(两个端点都认, 非法值一律**钳位**不报错):
#                             ?q=10..95     JPEG 质量(q=999 -> 95, q=1 -> 10; 默认 75)
#                             ?w=160..4096 目标宽度, 等比缩放(默认 = --http-maxw/不缩放;
#                                          w=1 -> 160)
#                             ?maxfps=1..30 MJPEG 限帧(默认 HTTP_JPEG_MAX_FPS=10; 只对
#                                          /stream.mjpg 有意义)
#                             ?diff=0..64   帧间去重阈值: 与"上次真正发出去的帧"的
#                                          64x36 灰度平均差 < diff 就不发(默认 0=关闭;
#                                          只对 /stream.mjpg 有意义)
#                             实际生效的参数会回在响应头 X-JPEG-Q / X-JPEG-W /
#                             X-JPEG-FPS / X-JPEG-DIFF 里, 方便现场核对
#     类别表(t21): 命令行 --classes <path>；不给就用 <模型同目录>/classes.txt；
#                  都没有就回退内置 COCO 80 类并打 [WARN]。启动打印
#                  [CLASS] nc=3 names=['scratch','missing','stain']  来源=...
#                  一行一个类名，空行与 # 注释行跳过，行号=类别 id（带 BOM 也能读；
#                  nc 会跟着变，detect=0 的空输出通道数按 3*(5+nc) 自适应）
#     所有响应带 Access-Control-Allow-Origin: *（方便以后网页调试）
# ============================================================================
HTTP_DEFAULT_PORT = 8000
HTTP_JPEG_MAX_FPS = 10.0          # 编码限频: 最多 10 张/秒(不拖慢主循环)
HTTP_STREAM_MAXW = 960            # 推流宽度上限(降 CPU; 0=不缩放)
DEFECT_DIR = '/home/rock/yolo/detect'   # 缺陷图目录, 可用 --save-dir 改
DEFECT_MIN_INTERVAL = 2.0         # 缺陷存图最小间隔(秒), 防止刷爆磁盘
EVENT_MAX_MEM = 500               # 事件日志: 内存环形队列长度(最近 500 条)
EVENT_MAX_RETURN = 200            # /api/events 单次最多返回条数
EVENT_FILE = 'events.jsonl'       # 事件全量落盘文件(在 save-dir 下, 每行一条 JSON)
SAVE_DIR_FALLBACK = 'snap'        # save-dir 不可写时的回落目录(相对 CWD)
DETECT_NULL_CH = 255              # 内置 COCO 80 类时 detect=0 的全 0 输出通道数 3*(5+80);
                                  # 自定义类别时按 null_channels() 动态算(别直接用这个常量)
CLASSES_FILE = 'classes.txt'      # 类别表默认文件名(与 .rknn 同目录); 一行一个类名, 行号=类别 id
SD_PATH_HINTS = ('/media/', '/run/media/', '/mnt/')   # 可移动盘常见挂载前缀
SYS_BLOCK = '/sys/block'          # sysfs 块设备根(自检里会临时指向假目录, 便于注入测试)
SYS_NET = '/sys/class/net'        # sysfs 网卡根(读 operstate/carrier; 自检可指向假目录)
SD_EXCLUDE_FSTYPES = frozenset((
    'rootfs', 'tmpfs', 'devtmpfs', 'ramfs', 'proc', 'sysfs', 'cgroup',
    'cgroup2', 'devpts', 'securityfs', 'pstore', 'bpf', 'tracefs', 'debugfs',
    'configfs', 'fusectl', 'mqueue', 'hugetlbfs', 'autofs', 'binfmt_misc',
    'overlay', 'squashfs', 'nsfs', 'rpc_pipefs', 'selinuxfs', 'efivarfs',
    'fuse.gvfsd-fuse', 'fuse.portal'))   # 这些都不是"真 SD 卡"
# ---- 低带宽 JPEG 参数的钳位范围(t22): 非法值一律**钳位**，绝不 400 ----
JPEG_Q_MIN, JPEG_Q_MAX, JPEG_Q_DEFAULT = 10, 95, 75
JPEG_W_MIN, JPEG_W_MAX = 160, 4096
STREAM_MAXFPS_MIN, STREAM_MAXFPS_MAX = 1.0, 30.0
STREAM_DIFF_MAX = 64.0            # 64x36 灰度平均差的上限(0=关闭去重)
# ---- 主动推送(t22): 板卡 -> 远端 HTTP POST ----
PUSH_QUEUE_MAX = 8                # 有界队列: 满了丢最旧并 fail+1（绝不阻塞主循环）
PUSH_TIMEOUT = 3.0                # 单次 POST 超时(秒)
PUSH_RETRY = 1                    # 失败重试次数(总共最多试 2 次)
MOBILE_PAGE_DEFECTS = 6           # /m 缺陷缩略图墙张数
MOBILE_PAGE_EVENTS = 10           # /m 最近事件条数

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:               # 很老的 Python: 自己拼一个多线程服务器
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

# 没有 cv2 / PIL 时的兜底: 一张 16x16 的**合法**最小 JPEG(灰度, 285 字节)
_MIN_JPEG = base64.b64decode(
    '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikx'
    'MC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01'
    'T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAAR'
    'CAAQABADASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAT/xAAUEAEAAAAAAAAAAAAA'
    'AAAAAAAA/8QAFAEBAAAAAAAAAAAAAAAAAAAAA//EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAM'
    'AwEAAhEDEQA/AIQDgf/Z')


def encode_jpeg(frame, max_w=0, quality=75):
    """numpy 帧 -> JPEG bytes。优先 cv2, 其次 PIL, 都没有就返回内置最小 JPEG"""
    if frame is None:
        return None
    img = frame
    if max_w and img.shape[1] > max_w:
        r = float(max_w) / float(img.shape[1])
        if cv2 is not None:
            img = cv2.resize(img, (int(img.shape[1] * r), int(img.shape[0] * r)),
                             interpolation=cv2.INTER_AREA)
        else:
            step = max(1, int(round(1.0 / r)))
            img = img[::step, ::step]
    if cv2 is not None:
        okj, buf = cv2.imencode('.jpg', img,
                                [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if okj:
            return buf.tobytes()
    try:
        from PIL import Image
        pil = Image.fromarray(
            np.ascontiguousarray(img[:, :, ::-1]) if img.ndim == 3 else img)
        b = io.BytesIO()
        pil.save(b, format='JPEG', quality=int(quality))
        return b.getvalue()
    except Exception:
        return _MIN_JPEG


# --------------------------------------------------------------------------
# 低带宽 JPEG 参数(t22): /stream.mjpg、/snap.jpg 的 ?q= ?w= ?maxfps= ?diff=
# --------------------------------------------------------------------------
def _to_int(val, default):
    """宽松取整: 空/None/非数字都回 default(不抛异常)"""
    if val is None:
        return default
    s = str(val).strip()
    if not s:
        return default
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _to_float(val, default):
    if val is None:
        return default
    s = str(val).strip()
    if not s:
        return default
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def parse_jpeg_opts(query, default_w=0, default_fps=None,
                    default_q=JPEG_Q_DEFAULT):
    """解析 /stream.mjpg、/snap.jpg 的**低带宽参数**——非法值一律**钳位**，绝不报错。

        q       JPEG 质量，钳到 [10, 95]（默认 75；q=999 -> 95，q=1 -> 10，q=abc -> 75）
        w       目标宽度，钳到 [160, 4096]，0/缺省 = 不缩放（沿用 --http-maxw）；
                只缩不放（比原图宽就按原图），等比缩放（w=1 -> 160）
        maxfps  MJPEG 限帧，钳到 [1, 30]（默认 HTTP_JPEG_MAX_FPS=10；只对推流有意义）
        diff    帧间去重阈值，钳到 [0, 64]（默认 0=关闭；与"上次真正发出去的帧"的
                64x36 灰度平均差 < diff 就**不发**，手机上静止画面几乎不耗流量）
    返回 dict(q=, w=, maxfps=, diff=) —— 都是钳位后的**实际生效值**。"""
    q = _to_int(_param(query, 'q'), default_q)
    if q <= 0:
        q = default_q
    q = max(JPEG_Q_MIN, min(JPEG_Q_MAX, q))
    w = _to_int(_param(query, 'w'), int(default_w or 0))
    if w < 0:
        w = 0
    elif w == 0:
        w = int(default_w or 0)          # 显式 w=0 也想不缩放
    elif w < JPEG_W_MIN:
        w = JPEG_W_MIN                   # w=1 -> 160
    elif w > JPEG_W_MAX:
        w = JPEG_W_MAX
    fps = _to_float(_param(query, 'maxfps'),
                    HTTP_JPEG_MAX_FPS if default_fps is None else default_fps)
    if fps != fps or fps <= 0:            # NaN / 负数 -> 默认
        fps = HTTP_JPEG_MAX_FPS
    fps = max(STREAM_MAXFPS_MIN, min(STREAM_MAXFPS_MAX, fps))
    dif = _to_float(_param(query, 'diff'), 0.0)
    if dif != dif or dif < 0:             # NaN / 负数 -> 关闭
        dif = 0.0
    dif = min(STREAM_DIFF_MAX, dif)
    return dict(q=q, w=w, maxfps=fps, diff=dif)


def _param(query, key):
    """从 'a=1&b=2' 里取参数值(URL 解码)；没有返回 None"""
    for part in (query or '').split('&'):
        if not part:
            continue
        name, _sep, val = part.partition('=')
        if unquote(name).strip() == key:
            return unquote(val)
    return None


def _gray_sig(frame, cols=64, rows=36):
    """帧的 64x36 灰度签名(帧间去重用)。没有 cv2 也能用: numpy 等距采样 + BT.601 灰度"""
    if frame is None:
        return None
    try:
        h, w = int(frame.shape[0]), int(frame.shape[1])
        if h < 2 or w < 2:
            return None
        ys = np.linspace(0, h - 1, min(rows, h)).astype(np.int32)
        xs = np.linspace(0, w - 1, min(cols, w)).astype(np.int32)
        g = frame[np.ix_(ys, xs)]
        if g.ndim == 3:
            g = (0.114 * g[:, :, 0].astype(np.float32)
                 + 0.587 * g[:, :, 1].astype(np.float32)
                 + 0.299 * g[:, :, 2].astype(np.float32))
        return g.astype(np.float32)
    except Exception:
        return None


def gray_diff(a, b):
    """两个签名的平均绝对差(0~255); 任一为空返回 None(视为"得发")"""
    if a is None or b is None:
        return None
    try:
        if a.shape != b.shape:
            return None
        return float(np.mean(np.abs(a - b)))
    except Exception:
        return None


def is_wireless_iface(name):
    """无线网卡判定(**只看名字**, 板子上很准): 以 wl 开头或 wlan 开头。

    Linux 上 USB WiFi 会叫 `wlx<mac>`(本项目实测 `wlx8c7ab3f81b37`), 内置的是
    `wlan0`, 两者都要算无线 —— 手机要连的通常就是它, 所以 /api/net 里无线排前面。"""
    n = str(name or '').strip().lower()
    return n.startswith('wl') or n.startswith('wlan')


def _read_sys_flag(path):
    """读 /sys/class/net/<if>/{operstate,carrier} 这类小文件 -> 1 / 0 / None。

    operstate 内容是 up/down/unknown..., carrier 是 0/1。**读不到一律 None**(不抛异常),
    所以在 Windows/没有 sysfs 的机器上 up/carrier 就是 JSON 的 null, 不会被误当成 down。"""
    try:
        f = open(path, 'r')
        try:
            txt = f.read(64).strip().lower()
        finally:
            f.close()
    except Exception:
        return None
    if txt in ('up', '1'):
        return 1
    if txt in ('down', '0'):
        return 0
    if txt in ('', 'unknown', 'dormant', 'lowerlayerdown', 'notpresent',
               'dormant\n'):
        return None
    try:
        return int(txt)
    except Exception:
        return None


def _iface_flags(name, sys_net=None):
    """(up, carrier): 读 /sys/class/net/<if>/operstate 与 /carrier, 读不到给 None"""
    base = os.path.join(sys_net or SYS_NET, str(name or ''))
    return (_read_sys_flag(os.path.join(base, 'operstate')),
            _read_sys_flag(os.path.join(base, 'carrier')))


def _enum_ifaces_linux():
    """Linux: socket.if_nameindex() 列全部网卡 + ioctl(SIOCGIFADDR) 取 IPv4。

    没有 IPv4 的网卡(纯 IPv6/未配置)自动跳过; 没有 fcntl(Windows) 直接返回 []。"""
    out = []
    try:
        import fcntl                       # 只有 Linux/POSIX 有
        import struct
        import socket as _socket
    except ImportError:
        return out
    try:
        pairs = _socket.if_nameindex()
    except Exception:
        return out
    sk = None
    try:
        sk = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        for _idx, name in pairs:
            name = str(name)
            if name == 'lo':
                continue
            try:
                req = struct.pack('256s', name[:15].encode('utf-8'))
                res = fcntl.ioctl(sk.fileno(), 0x8915, req)   # SIOCGIFADDR
                addr = _socket.inet_ntoa(res[20:24])
            except Exception:
                continue                   # 这块网卡没有 IPv4
            if not addr or addr == '0.0.0.0' or addr.startswith('127.'):
                continue
            out.append(dict(name=name, addr=addr))
    finally:
        if sk is not None:
            try:
                sk.close()
            except Exception:
                pass
    return out


def _enum_ifaces_fallback():
    """Windows/其它: 标准库没有"网卡名 -> IP"的映射, 用
    gethostbyname_ex(gethostname()) + UDP 出口技巧凑出 IP 列表。

    接口名只能给占位(`default` = 默认出口那块, 其它 `addr<N>`), 因为 Python 标准库
    拿不到 Windows 适配器的 IPv4; 手机上只关心地址, 名字仅供排查。"""
    import socket as _socket
    ips = []
    try:
        _host, _aliases, addr_list = _socket.gethostbyname_ex(_socket.gethostname())
        for ip in addr_list:
            if ip and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    egress = None
    try:
        sk = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            sk.connect(('8.8.8.8', 53))    # 不会真发包, 只让内核选出口 IP
            egress = sk.getsockname()[0]
        finally:
            sk.close()
    except Exception:
        egress = None
    if egress and egress not in ips:
        ips.insert(0, egress)
    out = []
    for i, ip in enumerate(ips):
        if not ip or ip == '0.0.0.0' or ip.startswith('127.'):
            continue
        out.append(dict(name=('default' if ip == egress else 'addr%d' % i),
                        addr=ip))
    return out


def net_ifaces(enum=None, sys_net=None):
    """本机各网卡的 IPv4 -> [{name, addr, up, carrier, wireless}], **无线优先**排序。

    · 只列 IPv4(AF_INET); 排除 lo、0.0.0.0、127.*
    · wireless: 名字 wl*/wlan*(见 is_wireless_iface)
    · up/carrier: 读 /sys/class/net/<if>/{operstate,carrier}; 读不到 = None
    · enum: **可注入的"网卡枚举"函数**(自检用假数据), 返回 [{'name':..,'addr':..}]
    · 任何失败都返回 [] —— 绝不抛异常(Windows 上没网卡就是空列表, /api/net 仍 ok:true)
    """
    raw = []
    if enum is not None:
        try:
            raw = list(enum() or [])
        except Exception:
            raw = []
    else:
        try:
            raw = _enum_ifaces_linux()
        except Exception:
            raw = []
        if not raw:                        # Windows/无 fcntl: 走 gethostbyname_ex 兜底
            try:
                raw = _enum_ifaces_fallback()
            except Exception:
                raw = []
    items = []
    seen = set()
    for it in raw:
        try:
            name = str(it.get('name') or '')
            addr = str(it.get('addr') or '')
        except Exception:
            continue
        if not addr or addr == '0.0.0.0' or addr.startswith('127.'):
            continue
        if name == 'lo':
            continue
        key = (name, addr)
        if key in seen:
            continue
        seen.add(key)
        up, carrier = _iface_flags(name, sys_net)
        items.append(dict(name=name, addr=addr, up=up, carrier=carrier,
                          wireless=1 if is_wireless_iface(name) else 0))
    # 无线优先(手机要连的就是无线); Windows 兜底时把主出口 'default' 排在其后,
    # 再按名字/地址稳定排序 —— 手机上第一条地址通常就是能连上的那条
    items.sort(key=lambda d: (0 if d['wireless'] else 1,
                              0 if d['name'] == 'default' else 1,
                              d['name'], d['addr']))
    return items


def net_urls(port, ifs=None, scheme='http', path='/m'):
    """由网卡列表拼出手机可用的 URL: {'mobile': ['http://<ip>:<port>/m', ...]}

    **无线优先**(沿用 net_ifaces 的顺序), 不带尾部斜杠; port 用服务真正监听的端口
    (--http 传进来的那个, 不是写死 8000)。"""
    if ifs is None:
        ifs = net_ifaces()
    urls = []
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = HTTP_DEFAULT_PORT
    for it in (ifs or []):
        addr = str((it or {}).get('addr') or '')
        if not addr:
            continue
        u = '%s://%s:%d%s' % (scheme, addr, port, path)
        if u not in urls:
            urls.append(u)
    return dict(mobile=urls)


def local_ips(ifaces=('end0', 'end1', 'wlan0')):
    """列出本机所有非环回 IPv4(手机照着输)。手机页 /m 顶部用。

    地址来源是 net_ifaces(): Linux 走 if_nameindex + ioctl(SIOCGIFADDR)
    ——**USB WiFi 叫 `wlx<mac>`, 不在固定名单里, 所以这里枚举全部网卡**;
    拿不到再退回 gethostbyname_ex + UDP 出口技巧。任何失败都返回 [], 绝不抛异常。
    `ifaces` 参数只为兼容老调用保留(曾经只查 end0/end1/wlan0 三块)。"""
    del ifaces                              # 现在不再按固定名单查
    return [it['addr'] for it in net_ifaces()]


def is_http_url(url):
    """推送目标只认 http:// 与 https://; 空串表示"没设置"(不算非法)"""
    s = str(url or '').strip().lower()
    return s.startswith('http://') or s.startswith('https://')


class FramePusher(object):
    """板卡**主动推送**(t22): 缺陷图/手动截图 -> 远端 HTTP 接收端(tools/recv_upload.py)

    · 主循环与 HTTP 线程只调 enqueue()（纯内存入队 + 丢掉最旧），**网络 IO 全在后台线程**
    · 有界队列 PUSH_QUEUE_MAX(8): 满了**丢最旧**并把 fail +1 —— 绝不阻塞主循环
    · POST body = JPEG 原始字节, 头 X-Event = 一行 JSON(seq,type,cls,score,ratio,fps,
      cover,box) + Content-Type: image/jpeg; 超时 3 s; 失败**重试 1 次**;
      仍失败 fail +1 并记 last_error
    · enabled=0 只是**关掉发送**(线程与队列都还在), 恢复后继续发; 没设置 url 时同样不发
    · 头里的 JSON 用 ensure_ascii=True: HTTP 头只能是 ASCII, 类别名可能是中文
    """

    def __init__(self, url='', enabled=False, queue_max=PUSH_QUEUE_MAX,
                 start=True):
        self._lock = threading.Lock()
        self._url = str(url or '')
        self._enabled = bool(enabled) and bool(self._url)
        self.queue_max = int(queue_max or PUSH_QUEUE_MAX)
        self.q = queue.Queue(maxsize=self.queue_max)
        self.ok = 0
        self.fail = 0
        self.dropped = 0                  # 丢最旧的次数(内部分析用, GET 里并进 fail)
        self.last_error = ''
        self.last_ok_t = 0.0
        self._stop = False
        self.th = None
        if start:
            self.th = threading.Thread(target=self._worker, name='pcb-push',
                                       daemon=True)
            self.th.start()

    # ---------------- 配置 / 状态 ----------------
    def get(self):
        """GET /api/push 的只读快照(契约字段: enabled/url/queued/ok/fail/last_error)"""
        with self._lock:
            return dict(enabled=1 if self._enabled else 0, url=self._url,
                        queued=int(self.q.qsize()), ok=int(self.ok),
                        fail=int(self.fail), last_error=self.last_error)

    def enabled(self):
        with self._lock:
            return bool(self._enabled)

    def url(self):
        with self._lock:
            return self._url

    def apply(self, args):
        """改开关/地址(只改 args 里出现的项); 返回 (state, changed_list)"""
        changed = []
        with self._lock:
            if 'url' in args:
                new_url = str(args.get('url') or '').strip()
                if new_url != self._url:
                    self._url = new_url
                    changed.append('url')
                if not self._url:         # 清空地址 => 自然发不出去
                    self._enabled = False
            if 'enabled' in args:
                want = bool(int(args.get('enabled') or 0)) and bool(self._url)
                if want != self._enabled:
                    self._enabled = want
                    changed.append('enabled')
            state = dict(enabled=1 if self._enabled else 0, url=self._url,
                         queued=int(self.q.qsize()), ok=int(self.ok),
                         fail=int(self.fail), last_error=self.last_error,
                         changed=changed)
        return state

    # ---------------- 入队 ----------------
    def enqueue(self, jpeg, event=None):
        """主循环/HTTP 线程只调这个: 入队成功返回 True。
        队列满时**丢最旧**并 fail+1(不阻塞、不抛异常); 没开/没 url 直接返回 False。"""
        if not jpeg:
            return False
        with self._lock:
            if not self._enabled or not self._url:
                return False
        item = (bytes(jpeg), dict(event or {}))
        try:
            self.q.put_nowait(item)
            return True
        except queue.Full:
            pass
        try:                              # 丢最旧, 再塞新的
            self.q.get_nowait()
            with self._lock:
                self.fail += 1
                self.dropped += 1
                self.last_error = 'queue full (dropped oldest)'
        except queue.Empty:
            pass
        try:
            self.q.put_nowait(item)
            return True
        except queue.Full:
            with self._lock:
                self.fail += 1
                self.last_error = 'queue full'
            return False

    # ---------------- 后台线程 ----------------
    def stop(self):
        self._stop = True

    def _worker(self):
        while not self._stop:
            try:
                jpeg, ev = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            with self._lock:
                url = self._url
                on = self._enabled
            if not on or not url:
                continue                  # 关掉了就当丢掉, 不计失败
            okk, err = self._post(url, jpeg, ev)
            with self._lock:
                if okk:
                    self.ok += 1
                    self.last_ok_t = time.time()
                else:
                    self.fail += 1
                    self.last_error = err

    def _post(self, url, jpeg, ev):
        """POST 一次(失败按 PUSH_RETRY 重试)。返回 (ok, err_text)"""
        try:
            payload = json.dumps(ev or {}, ensure_ascii=True,
                                 separators=(',', ':'))
        except Exception:
            payload = '{}'
        if len(payload) > 3000:           # HTTP 头别太长, 超了就不带头(接收端自己兜底)
            payload = '{}'
        hdr = {'Content-Type': 'image/jpeg',
               'X-Event': payload,
               'Content-Length': str(len(jpeg)),
               'User-Agent': 'rk-pcb-view-push/1.0'}
        last = ''
        for _try in range(PUSH_RETRY + 1):
            try:
                req = urllib.request.Request(url, data=jpeg, headers=hdr,
                                             method='POST')
                resp = urllib.request.urlopen(req, timeout=PUSH_TIMEOUT)
                try:
                    resp.read()
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass
                return True, ''
            except Exception as exc:
                last = '%s: %s' % (type(exc).__name__, exc)
                if _try < PUSH_RETRY:
                    time.sleep(0.2)       # 后台线程里短等再试, 不影响主循环
        return False, last


# ==========================================================================
# 尺寸测量模式 (t18) —— **全仓库只有这一节**碰 tools/dim_measure.py
#
# 为什么单独抽一层: vision 那边(t22)还在改内部算法(R3/C4/蓝牙的 ROI 模型)。
# 板卡端只认它**冻结的两个入口**, 将来换实现只改这一节, 主循环/HTTP 不用动:
#     calib = mod.calibrate(frame, cfg)          # 一次; 返回 Calib dict(CALIB_KEYS)
#     res   = mod.measure(frame, calib, cfg)     # 逐帧; 返回 Result dict(RESULT_KEYS)
# 精度现状(**必须照实说**): 板级 ±1 mm 量级的粗测, C4/R3/蓝牙还没达标(test/d35)。
# 所以每个目标都带 tol_mm、sigma 拿不到就是 null, calib 不可信时一个值都不给。
# ==========================================================================
DIM_MODULE_ENV = 'PCB_DIM_MODULE'   # 环境变量也能指定 dim_measure.py 位置
DIM_SCHEMA_FALLBACK = 'dim-v1'      # 模块不可用时对外报的 schema 版本(与 t22 冻结值一致)
DIM_SIGMA_FALLBACK_MM = 1.0         # 只在 calib 没给 sigma_mm 时兜底(板级粗测量级, 见 d35)
# 测量目标(**2026-10-26 用户决策 · S1**: 只测 板外框 / 定位孔距, 蓝牙模块尺寸不再测量)。
# 以 tools/dim_measure.py 里的 TARGET_ORDER 为准(它是冻结 schema 的一方), 这里只是兜底。
DIM_TARGET_ORDER = ('board_outline', 'hole_pitch')
# 自动测量间隔(秒): measure() 在 PC 上 0.32 s/帧, 板上 CPU 更慢 -> 默认 5 s, 不许低于 2 s
# ⚠ 0 = **关闭自动测量**(合法值, 不参与钳位; t24 修的正是"0 被 max(2,…) 吃掉"这个 bug)
DIM_AUTO_SEC_DEFAULT = 5
DIM_AUTO_SEC_MIN = 2
DIM_AUTO_SEC_MAX = 600
# /api/dim 顶层**恰好**这 17 个键(与 doc/10 §14.3 的契约逐字对齐, t24):
#   引擎内部时间戳(如 measured_t)**不许漏到响应里**, 时间信息只用契约内的 age_s。
DIM_API_KEYS = ('ok', 'schema', 'state', 'calib_ok', 'calib_note', 'sigma_mm',
                'accuracy_mm', 'tol_kind', 'scale_mm_per_px', 'calib_ts',
                'dim_available', 'dim_module_error', 'recalib_queued', 'busy',
                'age_s', 'perf_ms', 'targets')


def dim_module_candidates(extra=None):
    """dim_measure.py 的查找顺序: --dim-module > $PCB_DIM_MODULE > 脚本目录/tools/... > cwd"""
    cands = []
    for p in (extra, os.environ.get(DIM_MODULE_ENV)):
        if p:
            cands.append(str(p))
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:                      # 理论上不会发生(模块级 __file__ 一定在)
        here = os.getcwd()
    cands += [os.path.join(here, 'dim_measure.py'),
              os.path.join(here, 'tools', 'dim_measure.py'),
              os.path.join(here, '..', 'tools', 'dim_measure.py'),
              os.path.join('tools', 'dim_measure.py'),
              os.path.join('..', 'tools', 'dim_measure.py'),
              'dim_measure.py']
    out = []
    for p in cands:
        try:
            ap = os.path.abspath(p)
        except Exception:
            continue
        if ap not in out:
            out.append(ap)
    return out


def load_dim_module(extra=None):
    """按路径加载 tools/dim_measure.py(只用标准库 importlib)。返回 (module, 错误文本)。

    失败不抛异常(板子上可能根本没拷这个文件, 也可能缺 scipy): 返回 (None, 原因),
    这样 /api/dim 仍然有合法结构(calib_ok=false + note), HUD 显示 UNTRUSTED。"""
    import importlib.util
    for path in dim_module_candidates(extra):
        if not os.path.isfile(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location('pcb_dim_measure', path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as exc:
            return None, '%s 导入失败: %s: %s' % (os.path.basename(path),
                                                 type(exc).__name__, exc)
        for need in ('calibrate', 'measure'):
            if not hasattr(mod, need):
                return None, '%s 缺 %s()(schema 版本不匹配?)' % (
                    os.path.basename(path), need)
        return mod, ''
    return None, '没找到 dim_measure.py(试过 %d 个路径, 可用 --dim-module 指定)' % len(
        dim_module_candidates(extra))


class DimEngine(object):
    """尺寸测量适配层(t18): 标定一次 + 逐帧测量 + 三态状态机。

    状态机(给 HUD / /api/dim 用):
        'idle'  还没标定   'calib' 标定中(后台线程)   'ok' 已标定可信   'bad' 不可信/不可用
    · 标定只做一次: dim 第一帧自动触发, 或 /api/dim?calib=1 人工重标(后台线程, 不卡主循环)
    · 逐帧只调 mod.measure(): **2026-10-26 S1 起它不再读帧**(板外框/定位孔距在标定时
      就算完) → 板上开销 ≈ 0, 不含 NPU
    · **calib_ok=false 时一个 value_mm 都不给**(模块本来如此, 这里再兜一层, 防将来回归)
    · sigma 拿不到就是 null, 绝不编; tol_mm 永远给(mod 冻结接口保证)
    """

    def __init__(self, module_path=None, cfg=None, module=None, log=None):
        self._lock = threading.Lock()
        self.log = log or (lambda m: None)
        self.cfg = dict(cfg or {})
        t_imp = time.time()
        if module is not None:                 # 自检注入用(假模块)
            self.mod, self.err = module, ''
        else:
            self.mod, self.err = load_dim_module(module_path)
        import_ms = round((time.time() - t_imp) * 1000, 1)
        self.calib = None
        self.result = None
        self.perf = {'import': import_ms}
        self.t_calib = 0.0
        self.last_meas_t = 0.0
        self._busy = False                     # 标定中
        self._measuring = False                # 正在量一帧(触发式测量)
        self.auto_sec = 0.0                    # 自动测量间隔(秒); 0=关
        if self.mod is None:
            self.state = 'bad'
            self.note = 'dim_measure.py 不可用: ' + self.err
            self.result = self._blank_result(None)
        else:
            self.state = 'idle'
            self.note = '还没标定(进入测量模式第一帧会在后台标定一次)'
            # 预热: 模块在**启动时 import 一次**(scipy/PIL 的初始化都在里面), 绝不在主循环里现 import
            self.log('[DIM] 模块已加载: %s (import %.0f ms, schema=%s)'
                     % (getattr(self.mod, '__file__', '?'), import_ms, self.schema()))

    # ---------------- 小工具 ----------------
    def available(self):
        return self.mod is not None

    def schema(self):
        return str(getattr(self.mod, 'SCHEMA_VERSION', None) or DIM_SCHEMA_FALLBACK)

    def _names(self):
        ns = getattr(self.mod, 'TARGET_ORDER', None)
        return tuple(ns) if ns else DIM_TARGET_ORDER

    def _blank_targets(self, why=''):
        """calib 不可信/模块不可用时的目标列表: **没有 value_mm**, 但字段齐、tol_mm 有值"""
        out = []
        for n in self._names():
            out.append(dict(name=str(n), value_mm=None, nominal_mm=None,
                            err_mm=None, tol_mm=DIM_SIGMA_FALLBACK_MM, ok=False,
                            corners=None, sides=None, diag=None, area_mm2=None,
                            why=why))
        return out

    def _blank_result(self, calib, why=''):
        note = str((calib or {}).get('calib_note') or self.note or '')
        if not note:
            note = '标定不可信, 不出测量值'
        return dict(schema=self.schema(),
                    calib_ok=bool((calib or {}).get('calib_ok')),
                    calib_note=note,
                    sigma_mm=(calib or {}).get('sigma_mm'),
                    perf_ms=dict(self.perf or {}),
                    targets=self._blank_targets(why or note),
                    state=str(self.state))

    @staticmethod
    def _strip_values(res):
        """兜底: calib_ok=false 时把任何测量值都抹成 null(只留 tol/name/ok/why)"""
        out = dict(res or {})
        if out.get('calib_ok'):
            return out
        tgs = []
        for t in list(out.get('targets') or []):
            d = dict(t or {})
            for k in ('value_mm', 'err_mm', 'corners', 'sides', 'diag',
                      'area_mm2', 'short_mm', 'width_mm'):
                if k in d:
                    d[k] = None
            d['ok'] = False
            d.setdefault('why', 'calibration rejected')
            if d.get('tol_mm') is None:
                d['tol_mm'] = DIM_SIGMA_FALLBACK_MM
            tgs.append(d)
        out['targets'] = tgs
        return out

    # ---------------- 标定 ----------------
    def request_calibrate(self, frame):
        """后台线程标定(不卡主循环)。已在标定中/模块不可用 -> 返回 False"""
        if self.mod is None or frame is None:
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self.state = 'calib'
            self.note = '标定中...'
        img = np.array(frame, copy=True)       # 主循环的 frame 缓冲会被下一帧覆盖 -> 必须拷
        th = threading.Thread(target=self._calibrate_worker, args=(img,),
                              name='pcb-dim-calib', daemon=True)
        th.start()
        return True

    def _calibrate_worker(self, frame):
        try:
            self.calibrate_now(frame)
        except Exception as exc:               # 线程里绝不能把异常抛出去
            with self._lock:
                self.state = 'bad'
                self.note = '标定异常: %s: %s' % (type(exc).__name__, exc)
        finally:
            with self._lock:
                self._busy = False

    def calibrate_now(self, frame):
        """同步标定(后台线程/自检/人工触发都走它)。返回 calib dict 或 None"""
        if self.mod is None or frame is None:
            with self._lock:
                self.state = 'bad'
                self.note = ('dim_measure.py 不可用: ' + self.err) if self.mod is None \
                    else '没有可用帧, 无法标定'
            return None
        t0 = time.time()
        try:
            calib = self.mod.calibrate(frame, self.cfg) or {}
        except Exception as exc:
            with self._lock:
                self.state = 'bad'
                self.note = '标定异常: %s: %s' % (type(exc).__name__, exc)
                self.t_calib = time.time()
            return None
        okk = bool(calib.get('calib_ok'))
        with self._lock:
            self.calib = dict(calib)
            self.state = 'ok' if okk else 'bad'
            self.note = str(calib.get('calib_note') or ('可信' if okk else '不可信'))
            self.t_calib = time.time()
            self.perf = {'calibrate': round((time.time() - t0) * 1000, 1)}
            if not okk:
                self.result = self._blank_result(self.calib)
        if okk:
            # 【2026-10-26 S1】测量已是**标定驱动**: 标定一成功就立刻出结果,
            #   不必等第一次 run=1 或第一次自动测量(否则 /api/dim 会有一段时间是空的)。
            self.measure_now(fps=None, sync=True)
        if not okk:
            self.log('[DIM] 标定不可信: %s' % self.note)
        else:
            self.log('[DIM] 标定完成(%s): %s' % (
                ('%.1f ms' % ((time.time() - t0) * 1000)), self.note))
        return dict(calib)

    # ---------------- 触发式测量 ----------------
    def _calib_ready(self):
        with self._lock:
            return dict(self.calib) if self.calib else None

    def calibrated(self):
        c = self._calib_ready()
        return bool(c and c.get('calib_ok'))

    def busy(self):
        with self._lock:
            return bool(self._busy or self._measuring)

    def age(self):
        """距上次测量过了多少秒(没量过 -> None)。HUD 用它显示『已测 0.3s 前』"""
        with self._lock:
            t = self.last_meas_t
        return (time.time() - t) if t else None

    def measure_now(self, frame=None, fps=None, sync=True):
        """算一次测量结果。sync=False 丢后台线程, 自检/自动模式用。

        ⚠【2026-10-26 S1】**frame 已不参与计算**(保留形参兼容旧调用)。
          尺寸测量只测 *板外框 / 定位孔距*, 两项都在 calibrate() 阶段算完 →
          这里只是把 calib 重新组装成 dim-v1 结构: **不 warp、不读帧、不拷贝帧**,
          开销 ≈ 0(见 perf_ms)。所以 dim=1 对帧率的影响现在只剩 HUD 画字。

        原先为什么要"触发式"而不是"逐帧": measure() 当时要 warp + 量模块, PC 上
        0.32 s/帧。**那条约束随模块停测一起消失了** —— 保留触发式只是为了不动契约。
        正在算的时候再触发 -> 直接回上一次结果(带 busy=1), 不排队、不阻塞。
        """
        if self.mod is None:
            with self._lock:
                self.state = 'bad'
                self.result = self._blank_result(None)
                return dict(self.result)
        with self._lock:
            if self._measuring:
                res = dict(self.result or self._blank_result(None))
                res['busy'] = 1
                return res
            self._measuring = True
        if sync:
            return self._measure_worker(fps)
        th = threading.Thread(target=self._measure_worker, args=(fps,),
                              name='pcb-dim-measure', daemon=True)
        th.start()
        with self._lock:
            res = dict(self.result or self._blank_result(None))
        res['busy'] = 1
        return res

    def _measure_worker(self, fps=None):
        t0 = time.time()
        try:
            calib = self._calib_ready()
            if not (calib and calib.get('calib_ok')):
                res = self._blank_result(calib)
            else:
                try:
                    res = self.mod.measure(None, calib, self.cfg) or {}
                except Exception as exc:
                    res = self._blank_result(calib, why='measure 异常: %s: %s'
                                             % (type(exc).__name__, exc))
                res = self._strip_values(res)
            pm = dict(res.get('perf_ms') or {})
            pm['loop_total'] = round((time.time() - t0) * 1000, 2)
            if fps:
                pm['fps'] = round(float(fps), 1)
            res['perf_ms'] = pm
            # ⚠ 不再往结果里塞 measured_t(t24): 顶层只允许契约里的 17 键,
            #   "上次测量是什么时候" 一律用契约内的 age_s 表达(单位 s, 从没量过为 null)。
            with self._lock:
                res['state'] = self.state
                self.result = dict(res)
                self.last_meas_t = time.time()
            return dict(res)
        finally:
            with self._lock:
                self._measuring = False

    def tick(self, frame, fps=None, auto_sec=None):
        """主循环每帧调(dim=1 时) —— **O(1)**, 默认不测量。

        · 还没标定 -> 起一次后台标定(HUD 显示"标定中"), 本帧不测量;
        · auto_sec>0 且距上次测量超过 auto_sec -> **当场量一次**(卡这一帧,
          这是自动模式的代价; 板上建议 5 s, 不许低于 2 s);
        · 其余情况什么都不做, 只回最近一次结果给 HUD 画。
        """
        if auto_sec is not None:
            try:
                self.auto_sec = max(0.0, float(auto_sec))
            except (TypeError, ValueError):
                pass
        if self.mod is None:
            with self._lock:
                self.state = 'bad'
                self.result = self._blank_result(None)
                return dict(self.result)
        calib = self._calib_ready()
        if calib is None:
            if not self.busy():
                self.request_calibrate(frame)
            with self._lock:
                res = dict(self.result or self._blank_result(None))
                res['state'] = self.state
            return res
        with self._lock:
            due = (self.auto_sec > 0 and not self._measuring
                   and (time.time() - self.last_meas_t) >= self.auto_sec)
        if due:
            return self.measure_now(frame, fps=fps, sync=True)
        with self._lock:
            res = dict(self.result or self._blank_result(calib))
            res['state'] = self.state
        return res

    def hud(self):
        """HUD 快照: 最近结果 + 三态 + "测量中/已测 N 秒前"(每帧都调, 很便宜)

        ⚠ 这里**额外**塞两个绘制用的字段(不是 /api/dim 契约的一部分, 别漏到响应里):
          · H_mm2px        : 标定单应, 用来把 mm 坐标换算成像素画到画面上
          · hole_points_px : 4 个安装孔的像素坐标, 画成"定位点"
        t25 的 bug 就是"HUD 画框的代码写好了, 但调用处没把 calib 传进来" -> H 恒为 None
        -> `if pts and len(pts)==4` 永远不成立 -> 画面上一个框都不出。这里直接放进 res,
        调用处就不用再传 calib 了。
        """
        with self._lock:
            res = dict(self.result or self._blank_result(self.calib))
            res['state'] = self.state
            res['measuring'] = 1 if self._measuring else 0
            t = self.last_meas_t
            cal = dict(self.calib) if self.calib else {}
        res['age_s'] = round(time.time() - t, 2) if t else None
        res['H_mm2px'] = cal.get('H_mm2px')
        res['hole_points_px'] = cal.get('hole_points')
        res['coarse_quad_px'] = cal.get('coarse_quad_px')
        return res

    # ---------------- 对外快照 ----------------
    def snapshot(self):
        """GET /api/dim 的响应: 冻结 schema(RESULT_KEYS) + 板卡侧补充字段

        补充字段(冻结 schema 之外, 只增不改): state / scale_mm_per_px /
        dim_available / dim_module_error / calib_ts
        """
        with self._lock:
            res = dict(self.result) if self.result else self._blank_result(self.calib)
            calib = dict(self.calib) if self.calib else {}
            res['state'] = self.state
            res['scale_mm_per_px'] = calib.get('scale_mm_per_px')
            res['dim_available'] = bool(self.mod is not None)
            res['dim_module_error'] = self.err
            res['calib_ts'] = self.t_calib
            res['calib_note'] = res.get('calib_note') or self.note
            # 冻结 schema 之外的补充(只增不改), UI 要把"精度"和"判定线"分开:
            #   accuracy_mm = sigma_mm(实测不确定度, 可能 null); tol_mm 只是判定线
            res['accuracy_mm'] = res.get('sigma_mm')
            res['tol_kind'] = 'judge_line'
            if not res.get('calib_ok'):
                res = self._strip_values(res)
        return res


def dim_mm_to_px(H_mm2px, pts_mm):
    """mm(canonical) -> 原图 px(t18 HUD 用)。H 是 3x3(行主序 list 也行); 失败返回 None"""
    if H_mm2px is None or not pts_mm:
        return None
    try:
        H = np.asarray(H_mm2px, float).reshape(3, 3)
        out = []
        for p in pts_mm:
            v = H @ np.array([float(p[0]), float(p[1]), 1.0])
            if abs(float(v[2])) < 1e-9:
                return None
            out.append((int(round(float(v[0]) / float(v[2]))),
                        int(round(float(v[1]) / float(v[2])))))
        return out
    except Exception:
        return None


def draw_dim_hud(frame, res, calib=None):
    """尺寸测量 HUD(t18): 四边形 + 4 个角点 + 每边 mm + 每个目标 OK/NG + tol。

    · 文字**只用 ASCII**(cv2 的 Hershey 字体不支持中文), 中文原因留给 /api/dim 的 calib_note
    · 三态一眼可辨: 'DIM: CALIBRATING...' / 'DIM: CALIBRATED (coarse)' / 'DIM: UNTRUSTED'
    · 值一律带 tol, 形如 "board_outline 90.52mm (nom 90.50) OK +-1.0"
    """
    if frame is None or cv2 is None:
        return frame
    res = res or {}
    st = str(res.get('state') or '')
    okk = bool(res.get('calib_ok'))
    sigma = res.get('sigma_mm')
    if st == 'calib':
        tag, col = 'DIM: CALIBRATING...', (0, 220, 255)
    elif okk:
        tag, col = 'DIM: CALIBRATED (coarse)', (0, 255, 0)
    else:
        tag, col = 'DIM: UNTRUSTED - see /api/dim', (0, 0, 255)
    cv2.putText(frame, tag, (16, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, col, 2)
    # 触发式测量: 明确写出"正在量/上次量在几秒前", 否则用户会以为画面卡了
    pm = res.get('perf_ms') or {}
    tot = pm.get('loop_total', pm.get('measure'))
    perf = ('  %.0f ms' % float(tot)) if tot is not None else ''
    if res.get('measuring'):
        sub = 'MEASURING...' + perf
    elif res.get('age_s') is None:
        sub = 'not measured yet - GET /api/dim?run=1'
    else:
        sub = 'measured %.1f s ago' % float(res.get('age_s') or 0.0) + perf
    cv2.putText(frame, sub, (16, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (0, 220, 255) if res.get('measuring') else (200, 200, 200), 1)
    # ⚠ 精度 ≠ 判定线(t22 提醒): sigma=实测不确定度(可能 null), tol=判定线(逐目标给)。
    #   两个数分开显示, 免得出现"标着 ±0.3 实际差 1.7 mm"的信任崩塌。
    if okk:
        acc = ('acc sigma +-%.2f mm' % float(sigma)) if sigma is not None \
            else 'acc sigma -- (unmeasured)'
        cv2.putText(frame, acc, (16, 84),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1)
    tgs = list(res.get('targets') or [])
    # H 优先用调用方传的 calib; 没传就用 hud() 里塞的 H_mm2px(t25: 不塞就永远画不出框)
    H = (calib or {}).get('H_mm2px') or res.get('H_mm2px')
    # ① 四边形 + 4 角点 + 每边 mm(用 board_outline 那个目标; 它有 corners/sides)
    quad = next((t for t in tgs if t.get('name') == 'board_outline'), None)
    # 板框定位框 **不依赖标定成功**: 有 H 就画实测板框(带 mm), 没 H 就直接画粗框像素四角
    pts = dim_mm_to_px(H, quad.get('corners')) if (okk and quad) else None
    if not (pts and len(pts) == 4):
        _cq = res.get('coarse_quad_px') or []
        pts = [(int(round(float(p[0]))), int(round(float(p[1]))))
               for p in _cq] if len(_cq) == 4 else None
    if pts and len(pts) == 4:
            for i in range(4):
                cv2.line(frame, pts[i], pts[(i + 1) % 4], (0, 255, 0), 2)
                cv2.circle(frame, pts[i], 6, (0, 200, 255), -1)
                mx = (pts[i][0] + pts[(i + 1) % 4][0]) // 2
                my = (pts[i][1] + pts[(i + 1) % 4][1]) // 2
                sides = (quad or {}).get('sides') or []
                if i < len(sides) and sides[i] is not None:
                    cv2.putText(frame, '%.2f mm' % float(sides[i]), (mx + 6, my - 6),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
    # ② 其它目标: 画出它们的 4 角(小方块) + 在右下角列一行结果
    y = frame.shape[0] - 18 * max(1, min(len(tgs), 6)) - 10
    for t in tgs[:6]:
        name = str(t.get('name') or '?')
        val = t.get('value_mm')
        tol = t.get('tol_mm')
        verdict = ('OK' if t.get('ok') else 'NG') if val is not None else '--'
        if str(name) != 'board_outline':
            pts = dim_mm_to_px(H, t.get('corners')) if val is not None else None
            if pts and len(pts) == 4:
                for i in range(4):
                    cv2.line(frame, pts[i], pts[(i + 1) % 4], (255, 160, 0),
                             1)
        if name == 'hole_pitch' and t.get('consistency_pct') is not None:
            # hole_pitch 不是"独立测量", 而是**两方向尺度一致性**(t22 口径):
            # 显示 consistency_pct 比显示 86.2 mm 好懂; ok 判据就是 |cons| <= 3.5%
            _pl = t.get('pitch_long_mm')
            _ps = t.get('pitch_short_mm')
            _dim = ('  %.2fx%.2f' % (float(_pl), float(_ps))) \
                if (_pl is not None and _ps is not None) else ''
            line = '%-13s cons %+.2f%% (<=3.5%%)  %s%s' % (
                name, float(t['consistency_pct']), verdict, _dim)
        elif val is None:
            line = '%-13s --  tol %s' % (name, ('%.2f' % tol) if tol is not None else '--')
        else:
            line = '%-13s %.2f mm  %s  tol +-%.2f' % (
                name, float(val), verdict, float(tol if tol is not None else 0.0))
        c = (0, 255, 0) if t.get('ok') else (0, 180, 255)
        cv2.putText(frame, line, (16, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, c, 2)
        y += 18
    return frame


class RuntimeConfig(object):
    """运行时开关(HTTP 改 / 主循环读), 改完**下一帧就生效**, 不用重启。
       autosave: 1=检出就自动存图(默认)  0=不自动存(手动截图 /api/snap 仍可用)
       detect  : 1=跑 YOLO 推理         0=跳过推理(画面无框、帧率明显上升)
       dim     : 1=尺寸测量模式(t18)     0=关
                 **与 detect 互斥**(用户明确不要求同时开): dim=1 自动 detect=0,
                 detect=1 自动 dim=0 —— 检测要 NPU 24 ms, 测量要 warp+ROI, 同时跑两头都慢。
       model_loaded: RKNN 模型是否加载成功; detect=1 的请求在没模型时被拒(保持 0 + warning)
       构造: detect 不传(None)=跟着 model_loaded 走(有模型就默认开检测, 和 set_model_loaded(True) 一致);
             显式传 0/1 就按传的算, 但没模型恒为 0

    ⚠ get() **只返回 autosave/detect**(/api/status 的 15 字段契约靠它);
      dim 走 dim()/state(), 不要往 get() 里塞, 否则 /api/status 会变成 16 字段。"""

    def __init__(self, autosave=1, detect=None, model_loaded=False, dim=0,
                 dim_auto=0):
        self._lock = threading.Lock()
        self._autosave = 1 if autosave else 0
        self._model_loaded = bool(model_loaded)
        self._detect = 1 if (self._model_loaded and (detect is None or detect)) else 0
        self._dim = 0
        self._dim_auto = DIM_AUTO_SEC_DEFAULT if dim_auto else 0
        if dim:                                   # dim 优先: 与 detect 互斥
            self._dim = 1
            self._detect = 0

    def get(self):
        """只给 /api/status 用: autosave + detect(恰好 2 个键, 别加)"""
        with self._lock:
            return dict(autosave=self._autosave, detect=self._detect)

    def state(self):
        """完整快照(给 /api/config 用): autosave/detect/dim/dim_auto_sec"""
        with self._lock:
            return dict(autosave=self._autosave, detect=self._detect,
                        dim=self._dim, dim_auto_sec=self._dim_auto)

    def dim_auto_sec(self):
        """自动测量间隔(秒); 0 = 关。给主循环 tick() 用"""
        with self._lock:
            return 0 if not self._dim else int(self._dim_auto)

    def autosave(self):
        with self._lock:
            return self._autosave

    def detect(self):
        with self._lock:
            return self._detect

    def dim(self):
        with self._lock:
            return self._dim

    def model_loaded(self):
        with self._lock:
            return self._model_loaded

    def set_model_loaded(self, loaded=True):
        """模型加载成功后调: 有模型就把 detect 默认打开(命令行给了 --model 就是要检测)。
        已经开了 dim(测量模式)就不抢: 互斥语义优先, detect 保持 0。"""
        with self._lock:
            self._model_loaded = bool(loaded)
            if self._model_loaded and not self._dim:
                self._detect = 1

    def apply(self, args):
        """只改 args 里出现的项({'autosave':0/1, 'detect':0/1, 'dim':0/1});
        返回 (state_dict, warning 或 None)。

        互斥(t18): 先按 dim 处理, 再按 detect 处理 —— 谁设成 1 谁生效, 另一个自动清 0,
        被清的那个也会出现在 changed 里, 所以调用方从 changed 就能看出互斥结果。"""
        changed = []
        warn = None
        with self._lock:
            if 'autosave' in args:
                val = 1 if int(args['autosave']) else 0
                if val != self._autosave:
                    self._autosave = val
                    changed.append('autosave')
            if 'dim' in args:
                val = 1 if int(args['dim']) else 0
                if val != self._dim:
                    self._dim = val
                    changed.append('dim')
                if val and self._detect:          # dim=1 -> 强制 detect=0
                    self._detect = 0
                    if 'detect' not in changed:
                        changed.append('detect')
            if 'dim_auto_sec' in args:
                try:
                    val = int(args['dim_auto_sec'])
                except (TypeError, ValueError):
                    val = DIM_AUTO_SEC_DEFAULT
                # **0(及负数) = 关闭自动测量**, 是合法值, 绝不参与钳位(t24 修的就是这个:
                #   以前 0 被 max(2,…) 吃掉 -> "关不掉", 只能每 2 s 量一次)。
                # 只有 >0 才钳到 [2,600]: 板卡 CPU 扛不住 <2 s 的自动测量。
                if val > 0:
                    val = max(DIM_AUTO_SEC_MIN, min(DIM_AUTO_SEC_MAX, val))
                else:
                    val = 0
                if val != self._dim_auto:
                    self._dim_auto = val
                    changed.append('dim_auto_sec')
            if 'detect' in args:
                val = 1 if int(args['detect']) else 0
                if val == 1 and not self._model_loaded:
                    warn = 'no model loaded'       # 契约: ok:true, detect 保持 0
                elif val != self._detect:
                    self._detect = val
                    if 'detect' not in changed:
                        changed.append('detect')
                if val and self._dim:              # detect=1 -> 强制 dim=0
                    self._dim = 0
                    if 'dim' not in changed:
                        changed.append('dim')
            state = dict(autosave=self._autosave, detect=self._detect,
                         dim=self._dim, dim_auto_sec=self._dim_auto,
                         changed=changed)
        return state, warn


RUNTIME = RuntimeConfig()     # 全局运行时开关(HTTP 线程写 / 主循环读)


# --------------------------------------------------------------------------
# 自定义类别（t21）：类别名从 classes.txt 读，nc 跟着变
# --------------------------------------------------------------------------
def load_classes(path):
    """读类别表：一行一个类名，**跳过空行与 # 注释行**；行号 = 类别 id。

    返回 (names, err)：names 非空表示读成功；否则 err 说明原因（读不了/空文件）。"""
    names = []
    try:
        # utf-8-sig: 有人用记事本存的 classes.txt 带 BOM(\\ufeff)，
        # 不剥掉第一个类名会变成 '\\ufeffscratch'（HUD/事件里显示成怪字符）
        f = open(path, 'r', encoding='utf-8-sig')
        try:
            for line in f:
                s = line.replace('\ufeff', '').strip()
                if not s or s.startswith('#'):
                    continue
                names.append(s)
        finally:
            f.close()
    except OSError as exc:
        return [], '读不了(%s)' % exc
    except Exception as exc:
        return [], '解析失败(%s)' % exc
    if not names:
        return [], '里面没有类名(只有空行/注释?)'
    return names, None


def set_class_names(names):
    """**原地**替换 CLASS_NAMES（官方 draw_results 用的就是这个全局 list）。

    不重新赋值是为了保持一致：官方代码里 `CLASS_NAMES[cid]` 用的是模块全局名，
    原地改内容后它自然就用上新类别名，官方那几行一个字都不用改。"""
    CLASS_NAMES[:] = list(names)
    return len(CLASS_NAMES)


def nc():
    """当前类别数（跟着 CLASS_NAMES 走）"""
    return len(CLASS_NAMES)


def null_channels(n_classes=None):
    """detect=0 时全 0 输出的通道数：3*(5+nc)。COCO 80 类时 = 255，PCB 3 类时 = 24"""
    n = len(CLASS_NAMES) if n_classes is None else int(n_classes)
    return 3 * (5 + n)


def resolve_class_names(model_path, cli_path=None):
    """决定用哪套类别名：--classes > <模型同目录>/classes.txt > 内置 COCO 80 类。

    返回 (names 或 None, 来源说明, 警告文本或 None)；
    names=None 表示"用内置 COCO 列表"（CLASS_NAMES 保持官方原样）。"""
    if cli_path:
        names, err = load_classes(cli_path)
        if names:
            return names, '%s (--classes)' % cli_path, None
        return None, None, ('[ERR] --classes 指定的类别表不可用: %s -> %s'
                            % (cli_path, err))
    model_dir = os.path.dirname(os.path.abspath(model_path)) or '.'
    default_path = os.path.join(model_dir, CLASSES_FILE)
    if os.path.exists(default_path):
        names, err = load_classes(default_path)
        if names:
            return names, '%s (模型同目录)' % default_path, None
        return None, None, ('[WARN] 模型同目录的 %s 读不出类名(%s)，'
                            '回退内置 COCO %d 类' % (default_path, err,
                                                     len(CLASS_NAMES)))
    return None, '内置 COCO %d 类' % len(CLASS_NAMES), (
        '[WARN] 没找到 %s（也没给 --classes）：回退内置 COCO %d 类；'
        '板上跑自定义类别模型请加 --classes <classes.txt>'
        % (default_path, len(CLASS_NAMES)))


def _opt_value(argv, flag, default=None):
    """取 `--flag value` / `--flag=value` 的值（取不到返回 default）"""
    pre = flag + '='
    for a in argv:
        if a.startswith(pre):
            return a[len(pre):]
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv) and not argv[i + 1].startswith('--'):
            return argv[i + 1]
    return default


def print_class_names(src=''):
    """启动时打印类别表：<40 类全打；多了打前 8 个 + 总数"""
    n = len(CLASS_NAMES)
    if n <= 40:
        print('[CLASS] nc=%d names=%s  来源=%s' % (n, list(CLASS_NAMES), src))
    else:
        preview = ', '.join("'%s'" % x for x in CLASS_NAMES[:8])
        print('[CLASS] nc=%d names=[%s, ...]  (共 %d 类)  来源=%s'
              % (n, preview, n, src))


def class_name_of(cls_id):
    """cls_id -> 显示名；越界/非整数一律 'cls_<id>'（不抛异常）"""
    try:
        cid = int(cls_id)
    except Exception:
        return 'cls_?'
    if 0 <= cid < len(CLASS_NAMES):
        return CLASS_NAMES[cid]
    return 'cls_%d' % cid


class DetectGate(object):
    """包住真 RKNNLite: detect=0 时 inference 立刻返回"全 0 输出"(= 0 个目标)。

    这样官方"推理 → 后处理 → 画框"那几行**一个字都不用改**(不必缩进/加 if)，
    但 NPU 推理真的被跳过(detect=0 时 real_calls 不增加)，帧率会明显上升。
    全 0 输出经过 postprocess 就是 0 个目标 -> 不会画框、不会存图、不会发 detect 事件。

    全 0 输出的形状(t21)：真推理跑过就按**见过的形状**来；还没跑过就用 3*(5+nc)
    通道 + IMG_SIZE 推出来的网格 —— 3 类模型是 24 通道，不是写死的 255。"""

    def __init__(self, real, runtime=None):
        self._real = real
        self.rt = runtime if runtime is not None else RUNTIME
        self._zeros = None
        self._zero_key = None
        self._shapes = None        # 真推理见过的输出形状 [(1,C,H,W), ...]
        self.real_calls = 0        # 真推理被调用次数(自检/演示用)
        self.skipped_calls = 0     # 因为 detect=0 被跳过的次数

    def _null_outputs(self):
        key = (tuple(self._shapes) if self._shapes else None, len(CLASS_NAMES))
        if self._zeros is not None and self._zero_key == key:
            return self._zeros
        if self._shapes:
            outs = [np.zeros(s, np.float32) for s in self._shapes]
        else:
            ch = null_channels()
            g = max(8, int(IMG_SIZE) // 8)
            outs = [np.zeros((1, ch, g, g), np.float32),
                    np.zeros((1, ch, max(1, g // 2), max(1, g // 2)),
                             np.float32),
                    np.zeros((1, ch, max(1, g // 4), max(1, g // 4)),
                             np.float32)]
        self._zeros, self._zero_key = outs, key
        return outs

    def inference(self, inputs=None):
        if not self.rt.detect():
            self.skipped_calls += 1
            return self._null_outputs()
        self.real_calls += 1
        outs = self._real.inference(inputs=inputs)
        try:                       # 记住真输出形状，跳过时按同样形状喂 0
            if outs:
                self._shapes = [tuple(np.asarray(o).shape) for o in outs]
        except Exception:
            pass
        return outs

    def __getattr__(self, name):
        return getattr(self._real, name)


class FrameHub(object):
    """主循环写 / HTTP 线程读 的「最新一帧 + 最新状态」共享缓冲"""

    def __init__(self):
        self._lock = threading.Lock()
        self._enc = threading.Lock()
        self._frame = None
        self._seq = 0
        self._meta = dict(ok=True, ratio=None, kbps=0.0, qsel=0,
                          fps=0.0, blocks=0, nbytes=0, auto=0, target_kbps=0,
                          cover=0.0, b_per_blk=None)
        self._jpgs = {}                   # (宽, 质量) -> (编码时刻, bytes)

    def publish(self, frame, info, kbps, fps):
        """主循环每帧调一次。传**引用**不拷贝(帧末调用, 此后本帧不再被修改)"""
        info = info or {}
        ratio = info.get('ratio')
        blocks = int(info.get('blocks', 0) or 0)
        nbytes = int(info.get('nbytes', 0) or 0)
        meta = dict(ok=True,
                    ratio=(float(ratio) if ratio is not None else None),
                    kbps=round(float(kbps or 0.0), 1),
                    qsel=int(info.get('qsel', 0) or 0),
                    fps=round(float(fps or 0.0), 1),
                    blocks=blocks,
                    nbytes=nbytes,
                    auto=int(info.get('auto', 0) or 0),
                    target_kbps=int(info.get('target_kbps', 0) or 0),
                    # [t1] 覆盖率估算(本窗口编了多少画面) + 字节/块(压缩狠不狠)
                    cover=round(frame_cover(blocks), 6),
                    b_per_blk=bytes_per_block(nbytes, blocks))
        with self._lock:
            self._frame = frame
            self._seq += 1
            self._meta = meta

    def status(self):
        with self._lock:
            return dict(self._meta)

    def seq(self):
        with self._lock:
            return self._seq

    def frame(self):
        """最新一帧的引用(只读用; /api/snap 手动截图靠它)"""
        with self._lock:
            return self._frame

    def jpeg(self, max_w=HTTP_STREAM_MAXW, quality=75, maxfps=None):
        """取当前帧 JPEG。

        · 按 (宽度, 质量) 分别缓存, 所以 /stream.mjpg?q=40 和默认客户端互不干扰
        · 限频: 同一组参数距上次编码不足 1/maxfps 秒就用缓存(maxfps 缺省=HTTP_JPEG_MAX_FPS)
        · 返回 bytes; 没有帧时 None
        """
        fps = float(maxfps or HTTP_JPEG_MAX_FPS)
        if fps <= 0:
            fps = HTTP_JPEG_MAX_FPS
        key = (int(max_w or 0), int(quality or JPEG_Q_DEFAULT))
        with self._lock:
            frame = self._frame
            if frame is None:
                return None
            hit = self._jpgs.get(key)
            if hit is not None and (time.time() - hit[0]) < (1.0 / fps):
                return hit[1]
        with self._enc:                      # 同时只允许一个线程编码
            with self._lock:
                frame = self._frame
                if frame is None:
                    return None
                hit = self._jpgs.get(key)
                if hit is not None and (time.time() - hit[0]) < (1.0 / fps):
                    return hit[1]
            jpg = encode_jpeg(frame, key[0], key[1])
            if jpg is None:
                with self._lock:
                    hit = self._jpgs.get(key)
                return hit[1] if hit is not None else None
            with self._lock:
                self._jpgs[key] = (time.time(), jpg)
                if len(self._jpgs) > 8:       # 缓存别无限长(每个 q/w 组合一个槽)
                    old = sorted(self._jpgs.items(), key=lambda kv: kv[1][0])[0][0]
                    if old != key:
                        self._jpgs.pop(old, None)
            return jpg


def list_defect_files(dirpath, limit=500):
    """save-dir 下的缺陷图列表(新的在前), 供 /api/defects"""
    out = []
    try:
        names = os.listdir(dirpath)
    except OSError:
        return out
    for n in names:
        if n.startswith('.') or not n.lower().endswith(('.jpg', '.jpeg')):
            continue
        p = os.path.join(dirpath, n)
        try:
            if not os.path.isfile(p):
                continue
            st = os.stat(p)
        except OSError:
            continue
        out.append(dict(name=n, size=int(st.st_size), mtime=int(st.st_mtime)))
    out.sort(key=lambda d: d['mtime'], reverse=True)
    return out[:limit]


def _safe_defect_path(dirpath, rawname):
    """请求里的文件名 -> save-dir 内的安全绝对路径; 不合法返回 None(防路径穿越)"""
    safe = unquote(rawname or '').strip()
    if (not safe) or len(safe) > 200:
        return None
    for ch in ('/', '\\', ':', '\x00'):
        if ch in safe:
            return None
    if safe in ('.', '..') or safe.startswith('.'):
        return None
    if os.path.basename(safe) != safe:
        return None
    base = os.path.abspath(dirpath)
    path = os.path.abspath(os.path.join(base, safe))
    if os.path.dirname(path) != base:            # 再兜一层(理论上到不了这儿)
        return None
    return path


def free_bytes(dirpath):
    """save-dir 所在分区剩余字节; 算不出来返回 0(不影响服务)"""
    p = os.path.abspath(dirpath or '.')
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        return int(shutil.disk_usage(p).free)
    except Exception:
        return 0


def unique_name(dirpath, stem, ext):
    """dirpath 下不重名的 stem+ext(同一秒多次截图会变成 _2/_3...)"""
    name = stem + ext
    k = 1
    while os.path.exists(os.path.join(dirpath, name)) and k <= 99:
        k += 1
        name = '%s_%d%s' % (stem, k, ext)
    return name


def _say(msg):
    """安全地打一行; 控制台编码不认某些字符(如 Windows GBK 下的 ⚠)时用 ? 代替, 绝不抛异常"""
    try:
        print(msg)
    except UnicodeEncodeError:
        try:
            print(msg.encode('ascii', 'replace').decode('ascii'))
        except Exception:
            pass
    return msg


def _write_test(dirpath, tag='.pcb_wtest'):
    """**真写测试**: 在目录里试建一个临时子目录再删掉, 返回 (ok, reason)。

    为什么不只用 os.access(W_OK): vfat 挂载没带 uid=/gid= 时, 卡上目录是 root:root 0755,
    普通用户(linaro)其实写不了, 而 access() 在某些情况下会给出过于乐观的答案。
    真实 mkdir 才能反映"这张卡到底能不能写"。"""
    if not dirpath or not os.path.isdir(dirpath):
        return False, 'ENOENT(目录不存在)'
    probe = os.path.join(dirpath, '%s_%d' % (tag, os.getpid()))
    try:
        os.mkdir(probe)
    except OSError as exc:
        code = getattr(exc, 'errno', None)
        if code == 13:
            return False, 'EACCES(权限不足: 目录属主/权限不对或只读属性)'
        if code == 30:
            return False, 'EROFS(只读挂载)'
        if code == 28:
            return False, 'ENOSPC(空间不足)'
        return False, 'ERR(%s)' % exc
    try:
        os.rmdir(probe)
    except OSError:
        pass
    return True, ''


def save_jpeg(path, frame, quality=85):
    """把一帧写成 jpg 文件。cv2 优先, 没有 cv2 就用 PIL。返回 True/False"""
    if frame is None:
        return False
    if cv2 is not None:
        try:
            return bool(cv2.imwrite(
                path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]))
        except Exception:
            return False
    try:
        from PIL import Image
        Image.fromarray(np.ascontiguousarray(frame[:, :, ::-1])).save(
            path, format='JPEG', quality=int(quality))
        return True
    except Exception:
        return False


def ensure_store_dir(path, fallback=SAVE_DIR_FALLBACK):
    """确保 save-dir 存在且可写; 失败回落到 fallback 并打印警告。
    返回绝对路径(永远不会抛异常, 保证主程序不崩)"""
    cands = []
    if path:
        cands.append(path)
    if fallback:
        cands.append(fallback)
    last_err = None
    for i, cand in enumerate(cands):
        try:
            os.makedirs(cand, exist_ok=True)
            if not os.path.isdir(cand):
                raise OSError('不是目录')
            if not os.access(cand, os.W_OK):
                raise OSError('目录不可写')
            absd = os.path.abspath(cand)
            if i > 0:
                print('[WARN] save-dir %s 不可用(%s) -> 回落到 %s'
                      % (path, last_err, absd))
            return absd
        except OSError as exc:
            last_err = exc
    tmpd = os.path.join(tempfile.gettempdir(), 'pcb_defect')
    try:
        os.makedirs(tmpd, exist_ok=True)
        print('[WARN] save-dir %s 与回落目录都不可用(%s) -> 用临时目录 %s'
              % (path, last_err, tmpd))
        return os.path.abspath(tmpd)
    except OSError:
        print('[ERR] 没有可用存储目录(%s), 存图/事件日志会失败, 主循环继续'
              % (last_err,))
        return os.path.abspath(path or '.')


def _read_mounts(mounts_path='/proc/mounts'):
    """读挂载表 -> [(fsname, mountpoint, fstype, opts)]; 读不到(比如 Windows)返回 []"""
    out = []
    try:
        f = open(mounts_path, 'r')
        try:
            lines = f.read().splitlines()
        finally:
            f.close()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        fsname, mnt, fstype, opts = parts[0], parts[1], parts[2], parts[3]
        # /proc/mounts 里空格/制表符/反斜杠是八进制转义; Windows 盘符路径不按 POSIX 解
        # (否则 C:\Users\...\0403-02011 里的 "\040" 会被错误地换成空格)
        if not (len(mnt) >= 2 and mnt[1] == ':'):
            mnt = (mnt.replace('\\040', ' ').replace('\\011', '\t')
                   .replace('\\134', '\\'))
        out.append((fsname, mnt, fstype, opts))
    return out


def _st_dev(path):
    """路径所在设备号; 取不到返回 None"""
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def _looks_like_mount(path):
    """路径看起来应该是个挂载点吗(/media/…、/run/media/…、/mnt/…)"""
    p = '/' + os.path.abspath(path).replace('\\', '/').strip('/') + '/'
    for hint in SD_PATH_HINTS:
        if hint in p:
            return True
    return False


def find_sd_mount(mounts_path='/proc/mounts', notes=None):
    """按挂载表找**可写的真实块设备**挂载点(SD 卡/U 盘), 返回 '<挂载点>/defect'。

    优先级: fsname /dev/mmcblk* (SD/MMC, 我们要的卡) > /dev/sd* (U 盘)
            > /media、/run/media、/mnt 下的其它真实块设备挂载;
    同一优先级里取**剩余空间最大**的。
    排除: 和 / 同一个设备的、tmpfs/devtmpfs/proc/sysfs/cgroup/overlay 等伪文件系统、
          只读挂载、**真写测试失败**的、非块设备(网络盘/fuse)。
    一个都没有 -> 返回 None(调用方回落默认目录, 绝不抛异常)。
    notes: 可选 dict, 会被填上 'rejected'=[(挂载点, 设备名, fstype, 原因)], 便于告警/排障。"""
    entries = _read_mounts(mounts_path)
    if notes is not None:
        notes.setdefault('rejected', [])
    if not entries:
        return None
    root_dev = _st_dev('/')
    cands = []
    for fsname, mnt, fstype, opts in entries:
        if fstype in SD_EXCLUDE_FSTYPES or fstype.startswith('fuse.'):
            continue
        if 'ro' in opts.split(','):
            continue
        if not fsname.startswith('/dev/'):        # 只认真实块设备
            continue
        dev = _st_dev(mnt)
        if dev is None or (root_dev is not None and dev == root_dev):
            continue                              # 和 / 同设备 -> 不是独立挂载
        if not os.path.isdir(mnt):
            continue
        if fsname.startswith('/dev/mmcblk'):
            rank = 0
        elif fsname.startswith('/dev/sd'):
            rank = 1
        elif any(mnt.startswith(h) for h in SD_PATH_HINTS):
            rank = 2
        else:
            continue                              # 其它位置的真实块设备(如独立 /home)不算
        ok, reason = _write_test(mnt)             # 真写测试: 不能只看 access()
        if not ok:
            if notes is not None:
                notes['rejected'].append((mnt, fsname, fstype, reason))
            continue
        free = free_bytes(mnt)
        if free <= 0:
            if notes is not None:
                notes['rejected'].append((mnt, fsname, fstype,
                                          'ENOSPC(剩余空间 0)'))
            continue
        cands.append((rank, -free, mnt, fsname, fstype))
    if not cands:
        return None
    cands.sort()
    rank, _negfree, mnt, _fsname, _fstype = cands[0]
    return os.path.join(mnt, 'defect')


def describe_store_dir(path, mounts_path='/proc/mounts'):
    """存图目录来源核验: 到底在 SD/U 盘上, 还是其实落在 rootfs?

    返回 dict(dir, dev, mount, fstype, free, on_rootfs, removable,
              writable, mount_opts)
      dir        绝对路径
      dev        目录所在挂载点的设备名(如 /dev/mmcblk1p1); 落在 rootfs 时给 /dev/root
      mount      目录所在的挂载点(最具体的那个), 找不到给 None
      fstype     文件系统类型(如 vfat/ext4)
      free       该分区剩余字节(int)
      on_rootfs  st_dev == / 的 st_dev 时为 True(说明不是独立分区)
      removable  按 _device_kind() 判: SD/USB -> True, eMMC/rootfs/其它 -> False
                 (eMMC 上的 /userdata 不是 removable, 别当成 SD 卡)
                 ⚠ 不读 /sys/block/mmcblkN/removable: Rockchip 厂商内核里 SD 卡的该值
                 常为 0(DT 按固定槽处理), 不可靠, 不作主判据
      writable   真实 mkdir/rmdir 写测试是否通过(不用 os.access 糊弄)
      mount_opts 该挂载点的挂载选项原文(截断, 排障用: 看有没有 uid=/dmask=)"""
    absdir = os.path.abspath(path)
    entries = _read_mounts(mounts_path)
    root_dev = _st_dev('/')
    dev_name, mount, fstype, mount_opts = None, None, None, ''
    best = -1
    for fsname, mnt, ft, opts in entries:
        mnt_abs = os.path.abspath(mnt)
        prefix = mnt_abs if mnt_abs.endswith(os.sep) else mnt_abs + os.sep
        if absdir == mnt_abs or absdir.startswith(prefix):
            if len(mnt_abs) > best:
                best, dev_name, mount, fstype = len(mnt_abs), fsname, mnt_abs, ft
                mount_opts = opts[:160]
    d_dev = _st_dev(absdir)
    if d_dev is None:
        d_dev = root_dev
    if not entries:
        # 没有挂载表(Windows / 无 /proc/mounts): 说明没有独立挂载 -> 视作和 rootfs 同源
        on_rootfs = True
    else:
        on_rootfs = bool(d_dev is not None and root_dev is not None
                         and d_dev == root_dev)
    if dev_name is None and on_rootfs and entries:
        dev_name = '/dev/root'
    # removable 统一由 _device_kind() 判(SD/USB 才算可移动; eMMC 上的 /userdata 不算)
    removable = (_device_kind(dev_name) in ('SD', 'USB')) if dev_name else False
    writable, _why = _write_test(absdir) if os.path.isdir(absdir) else (False, '')
    return dict(dir=absdir, dev=dev_name, mount=mount, fstype=fstype,
                free=free_bytes(absdir), on_rootfs=on_rootfs,
                removable=removable, writable=bool(writable),
                mount_opts=mount_opts)


def _cur_user():
    try:
        return os.environ.get('USER') or os.environ.get('USERNAME') or '?'
    except Exception:
        return '?'


def _read_sys_text(path):
    """读 sysfs 里的一行文本(去首尾空白); 读不到就返回 '' —— 绝不抛异常"""
    try:
        f = open(path, 'r')
        try:
            return f.read().strip()
        finally:
            f.close()
    except Exception:
        return ''


def _device_kind(dev_name):
    """设备类型: SD / USB / eMMC / 未知 / 其它。

    判据顺序(顺序很重要, 之前踩过坑):
      ① /sys/block/mmcblk<N>/device/type —— 内核给的**权威值**:
         'SD*' -> SD, 'MMC*' -> eMMC, 'SDIO'/其它 -> 继续往下判;
      ② 编号约定回退(**只在 ① 读不到时**用): RK3576 上 mmcblk0 = eMMC, mmcblk1 及以后 = SD;
      ③ /sys/block/mmcblk<N>/removable **只作参考, 不再当判据**:
         Rockchip 厂商内核里 SD 卡的 removable 常为 0(DT 按固定槽处理),
         用它当主判据会把 SD 卡误判成 eMMC(于是错报"⚠ 不在 SD 卡上")。
    ① 和 ② 都给不出结论(比如设备号不是数字) -> 返回 '未知', **不默认 eMMC**
    (宁可不结论, 也不把不确定包装成确定)。非 Linux / 无 sysfs 时安全回退, 不抛异常。"""
    d = str(dev_name or '')
    if d.startswith('/dev/mmcblk'):
        blk = d.rsplit('/', 1)[-1]
        idx = blk.replace('mmcblk', '').split('p')[0] or ''
        # ① 内核 device/type: SD / MMC / SDIO ...
        typ = _read_sys_text(os.path.join(
            SYS_BLOCK, 'mmcblk%s' % idx, 'device', 'type')).upper()
        if typ.startswith('SD'):
            return 'SD'
        if typ.startswith('MMC'):
            return 'eMMC'
        # ② 编号约定回退(RK3576: 0=eMMC, 1+=SD); ③ removable 不参与
        if not idx.isdigit():
            return '未知'          # 编号都没有 -> 明确说"未知", 不能猜成 eMMC
        return 'SD' if idx != '0' else 'eMMC'
    if d.startswith('/dev/sd'):
        return 'USB'
    return '其它'


def log_store_dir(path, mounts_path='/proc/mounts'):
    """启动时打一行信息量足够的 [STORE]; 不在 SD 卡上要显式标出来;
    "看起来是挂载点却落在 rootfs" 打醒目 [WARN]。

    固定格式(手册/验收可以直接照抄):
      [STORE] <最终目录>  (<fstype>, <SD|USB|eMMC|未知|rootfs|其它>)  设备 <dev> @ <mount>  可用 x.x GB[  ⚠ ...]

    尾注规则:
      · kind=='未知' 且**不在** rootfs -> `⚠ 设备类型未知(请用 lsblk 确认是不是 SD 卡)`
        (**不**打"不在 SD 卡上" —— 那是断言, 我们并不知道)
      · kind=='未知' 且在 rootfs     -> `⚠ 不在 SD 卡上`(落在 rootfs 是确定事实)
      · 其它不在 SD 上的情况(eMMC/rootfs/其它) -> `⚠ 不在 SD 卡上`"""
    info = describe_store_dir(path, mounts_path)
    gb = info['free'] / (1024.0 ** 3)
    kind = _device_kind(info['dev']) if info['dev'] else '其它'
    on_sd = kind in ('SD', 'USB') and not info['on_rootfs']
    if not on_sd and info['on_rootfs'] and kind == '其它':
        kind = 'rootfs'
    tail = ''
    if kind == '未知' and not info['on_rootfs']:
        tail += '  ⚠ 设备类型未知(请用 lsblk 确认是不是 SD 卡)'
    elif not on_sd:
        tail += '  ⚠ 不在 SD 卡上'
    if not info['writable']:
        tail += '  ⚠ 目录不可写'
    if info['mount']:
        _say('[STORE] %s  (%s, %s)  设备 %s @ %s  可用 %.1f GB%s'
             % (info['dir'], info['fstype'] or '?', kind, info['dev'] or '?',
                info['mount'], gb, tail))
    else:
        _say('[STORE] %s  (%s, %s)  设备 %s  可用 %.1f GB%s'
             % (info['dir'], info['fstype'] or '?', kind, info['dev'] or '?',
                gb, tail))
    if info['on_rootfs'] and _looks_like_mount(info['dir']):
        _say('[WARN] save-dir %s 看起来是挂载点, 但实际落在 rootfs(设备 %s)'
             ' —— SD 卡可能没挂上, 图会存到根分区!'
             % (info['dir'], info['dev'] or '/dev/root'))
    if info['mount'] and info['fstype'] == 'vfat' and not info['writable']:
        _say('[WARN] 挂载选项 = %s （vfat 没有 uid=/dmask= 时卡上目录是 root:root 0755, '
             '普通用户写不进去）' % (info['mount_opts'] or '?'))
    return info


def resolve_store_dir(save_dir, fallback=DEFECT_DIR, log=True):
    """--save-dir 的值 -> 真实可写目录(绝对路径), 永不抛异常。

    'auto'(大小写都认) -> find_sd_mount() 自动找 SD/U 盘; 找不到就 [WARN] 并回落 fallback;
    其它值 -> 原样交给 ensure_store_dir(不存在会创建、不可写会回落)。
    坑就在这里: 把 /mnt/sdcard/defect 传进来时, 若 SD 没挂上, ensure_store_dir 会在 rootfs
    上把这个目录建出来 —— 所以最后一定要用 log_store_dir 把真实来源打出来并告警;
    若是 auto 且找到了 /dev/mmcblk* 但**不可写**, 必须先打一条可操作的 [WARN] 再回落。"""
    want = save_dir if save_dir else fallback
    used_auto = isinstance(want, str) and want.strip().lower() == 'auto'
    notes = {}
    sd_rejects = []
    if used_auto:
        picked = find_sd_mount(notes=notes)
        sd_rejects = [r for r in (notes.get('rejected') or [])
                      if str(r[1]).startswith('/dev/mmcblk')]
        if picked is None:
            if not sd_rejects:
                print('[WARN] --save-dir auto: 没找到可写的 SD/U 盘挂载点'
                      '(卡插好了吗? 桌面/udisks 挂上了吗? 看 /proc/mounts)')
            want = fallback
        else:
            print('[STORE] --save-dir auto 选中: %s' % picked)
            want = picked
    real = ensure_store_dir(want)
    info = log_store_dir(real) if log else None
    if used_auto and sd_rejects and (info is None or not info.get('writable')
                                     or info.get('on_rootfs')):
        mnt, fsname, fstype, reason = sd_rejects[0]
        # 一行同时给出: 原候选/原因/实际落地目录 + 为什么会这样 + 怎么修
        _say('[WARN] %s 不可写(%s) -> 用 %s%s'
             ' —— 找到 SD 卡 %s @ %s，但当前用户(%s)不可写'
             '（vfat 挂载缺少 uid= 时卡上目录是 root:root 0755）。'
             % (os.path.join(mnt, 'defect'), reason,
                (info or {}).get('dir') or real,
                '(rootfs!)' if (info or {}).get('on_rootfs') else '',
                fsname, mnt, _cur_user()))
        # 用户就在终端前: 给出可以**直接照抄**的两条命令(程序不自我提权, 只提示)
        _say('[WARN]   可照抄①（sudo 启动）:  sudo python3 %s --http %d --save-dir auto'
             % (os.path.basename(sys.argv[0] or 'rk_pcb_view.py'),
                HTTP_DEFAULT_PORT))
        _say('[WARN]   可照抄②（带 uid 重挂载）:  sudo mount -o remount,'
             'uid=$(id -u),gid=$(id -g),utf8,umask=0002 %s' % mnt)
    if (not used_auto) and _looks_like_mount(str(save_dir or '')) and \
            (info is None or info.get('on_rootfs')) and \
            os.path.abspath(real) != os.path.abspath(str(save_dir or '')):
        _say('[WARN] 你指定的 save-dir %s 没能用上, 实际存到 %s%s'
             ' —— SD 卡没挂上或不可写, 图不会存到卡上'
             % (save_dir, real,
                '(rootfs!)' if (info or {}).get('on_rootfs') else ''))
    return real


def _resolve_store_dir_cli(argv):
    """--store-info: 不开摄像头/不开服务, 只解析 --save-dir(含 auto) 并打印 [STORE]/[WARN]。
    板上排查"图到底存哪去了"用:  python3 rk_pcb_view.py --save-dir auto --store-info"""
    opts = _parse_http_opts(list(argv))
    want = opts.get('save_dir')
    print('[INFO] --save-dir = %r' % (want,))
    if isinstance(want, str) and want.strip().lower() == 'auto':
        notes = {}
        picked = find_sd_mount(notes=notes)
        print('[INFO] find_sd_mount() -> %r' % (picked,))
        for mnt, fsname, fstype, reason in (notes.get('rejected') or []):
            print('[INFO] 候选被拒: %s (%s, %s) 原因 %s'
                  % (mnt, fsname, fstype, reason))
        if not picked:
            print('[INFO] 没有可用的可写块设备挂载点(/proc/mounts 里找不到)')
    real = resolve_store_dir(want)
    if not opts.get('save_dir_given'):
        _say('[STORE] 未指定 --save-dir，用默认目录 %s'
             '（如不是 SD 卡，请加 --save-dir auto 或显式指定）' % real)
    print('[INFO] 最终存图目录 = %s' % real)
    return real


class EventLog(object):
    """事件日志: 内存环形队列(最近 EVENT_MAX_MEM 条) + 单独线程追加写 events.jsonl。
    主循环/HTTP 线程只调 add() -> 内存入队, 磁盘由后台线程慢慢写,
    所以绝不拖慢主循环; 磁盘满/不可写只丢日志, 不影响推理。"""

    def __init__(self, store_dir, max_mem=EVENT_MAX_MEM, filename=EVENT_FILE):
        self.lock = threading.Lock()
        self.events = deque(maxlen=int(max_mem or EVENT_MAX_MEM))
        self.seq = 0
        self.dir = store_dir
        self.path = os.path.join(store_dir, filename)
        self.q = queue.Queue()
        self.warned = False
        self.th = threading.Thread(target=self._writer, name='pcb-events',
                                   daemon=True)
        self.th.start()

    def add(self, etype, file=None, size=0, cls=None, score=None,
            ratio=None, fps=0.0, mode=None):
        """产生一条事件(字段与 /api/events 契约一致); 返回该事件 dict"""
        with self.lock:
            self.seq += 1
            ev = dict(seq=self.seq,
                      t=time.time(),
                      type=str(etype),
                      file=(None if file is None else str(file)),
                      size=int(size or 0),
                      cls=(None if cls is None else str(cls)),
                      score=(None if score is None else float(score)),
                      ratio=(None if ratio is None else float(ratio)),
                      fps=float(fps or 0.0),
                      mode=(None if mode is None else int(mode)))
            self.events.append(ev)
        self.q.put(dict(ev))
        return ev

    def since(self, since=0, limit=EVENT_MAX_RETURN):
        """seq > since 的事件, 最多 limit 条。
        next = 本次返回的最后一条 seq; 没有新事件时 next = 最新 seq"""
        try:
            since = int(since)
        except (TypeError, ValueError):
            since = 0
        with self.lock:
            last = self.seq
            out = [dict(e) for e in self.events if e['seq'] > since][:int(limit)]
        return dict(next=(out[-1]['seq'] if out else last), events=out)

    def last_seq(self):
        with self.lock:
            return self.seq

    def _writer(self):
        while True:
            ev = self.q.get()
            if ev is None:
                return
            try:
                f = open(self.path, 'a', encoding='utf-8')
                try:
                    f.write(json.dumps(ev, ensure_ascii=False) + '\n')
                finally:
                    f.close()
            except Exception as exc:
                if not self.warned:
                    self.warned = True
                    print('[WARN] 事件落盘失败(%s): %s' % (self.path, exc))


_HTTP_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>PCB 缺陷检测 · 板卡状态</title>
<style>
 body{background:#111;color:#eee;font-family:system-ui,sans-serif;margin:16px}
 h1{font-size:16px;font-weight:600;margin:0 0 10px}
 #s{font-size:15px;color:#7f7}
 .bar{height:14px;background:#333;border-radius:7px;overflow:hidden;width:520px;margin:8px 0 12px}
 .bar>div{height:100%;width:0;background:#3d3;transition:width .3s}
 img{max-width:100%;border:1px solid #333;border-radius:4px}
 code{background:#222;padding:1px 4px;border-radius:3px;font-size:12px}
 p{color:#999;font-size:12px}
 button{background:#2a5;color:#fff;border:0;border-radius:4px;padding:6px 12px;
        font-size:13px;cursor:pointer;margin:8px 0}
 #t{color:#9cf;font-size:12px;margin-left:8px}
</style></head><body>
<h1>RK3576 · FPGA 硬件 JPEG + YOLOv5 缺陷检测（板卡 HTTP 服务）</h1>
<div id="s">加载中…</div>
<div class="bar"><div id="bar"></div></div>
<div><button id="snap">手动截图</button><span id="t"></span></div>
<img src="/stream.mjpg" alt="MJPEG 实时画面">
<p>接口: <code>/api/status</code> <code>/api/events?since=N</code> <code>/api/snap</code>
 <code>/snap.jpg</code> <code>/stream.mjpg</code> <code>/api/defects</code>
 <code>/defects/&lt;name&gt;</code> <code>/api/push</code>
 ｜ 手机看画面: <a href="/m"><code>/m</code></a></p>
<script>
function pct(j){return (j.ratio===null||j.ratio===undefined)?'--':(j.ratio*100).toFixed(1)+'%';}
function cov(j){return (j.cover===null||j.cover===undefined)?'--':(j.cover*100).toFixed(1)+'%';}
function bpb(j){return (j.b_per_blk===null||j.b_per_blk===undefined)?'--':j.b_per_blk.toFixed(1)+' B/块';}
function gb(n){return (n/1073741824).toFixed(1);}
async function tick(){
  try{
    const r = await fetch('/api/status', {cache:'no-store'});
    const j = await r.json();
    document.getElementById('s').textContent =
      '压缩率 ' + pct(j) + ' ｜ 码率 ' + j.kbps.toFixed(1) + ' KB/s ｜ 帧率 ' +
      j.fps.toFixed(1) + ' fps ｜ 质量档 Q' + [30,50,75,90][j.qsel] +
      ' ｜ ' + (j.auto ? '自适应' : '手动') + ' 目标 ' +
      (j.target_kbps ? (j.target_kbps + ' KB/s') : '--') +
      ' ｜ 覆盖率 ' + cov(j) + ' ｜ ' + bpb(j) +
      ' ｜ 检测 ' + (j.detect ? 'ON' : 'OFF') +
      ' 存图 ' + (j.autosave ? 'ON' : 'OFF') +
      ' ｜ 块数 ' + j.blocks + ' ｜ 字节 ' + j.nbytes +
      ' ｜ SD 剩余 ' + gb(j.sd_free || 0) + ' GB';
    document.getElementById('bar').style.width =
      (j.ratio===null||j.ratio===undefined)?'0%':(Math.min(j.ratio*100,100)+'%');
  }catch(e){
    document.getElementById('s').textContent = '板卡连接中断: ' + e;
  }
}
document.getElementById('snap').onclick = async function(){
  const el = document.getElementById('t');
  try{
    const r = await fetch('/api/snap', {cache:'no-store'});
    const j = await r.json();
    el.textContent = j.ok ? ('已存 ' + j.file + ' (' + j.size + ' B) -> ' + j.dir)
                          : ('截图失败: ' + (j.error || r.status));
  }catch(e){ el.textContent = '截图失败: ' + e; }
};
setInterval(tick, 1000); tick();
</script>
</body></html>"""


# ============================================================================
# 手机低带宽页 /m (t22) —— 纯内联 HTML+JS, 不引任何外部资源(现场可能没网)
#   顶部: 本机各网卡 IP(手机照着输) + 压缩率/fps/档位/单窗口抽样
#   中段: <img src="/stream.mjpg?q=40&w=640&maxfps=5">(低带宽 MJPEG)
#   下段: 缺陷缩略图墙(/api/defects 前 6 张) + 最近 10 条事件
#   告警: 每 1 s 拉一次 /api/events?since=N, 命中新 detect -> 顶部红条 3 s + 一声 beep
#         (WebAudio 现场合成, 不加载 mp3; 手机浏览器要求用户先点一下"开启提示音")
# 模板里的 @@XXX@@ 由 mobile_page_html() 替换(不用 % 格式化: 页面里 % 太多)
# ============================================================================
_M_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PCB 缺陷检测 · 手机页</title>
<style>
 *{box-sizing:border-box}
 body{margin:0;padding:10px 10px 28px;background:#0d0d0d;color:#eee;
      font-family:system-ui,-apple-system,"PingFang SC",sans-serif;font-size:16px}
 h1{font-size:17px;margin:0 0 8px;font-weight:600;color:#9cf}
 .ips{font-size:26px;font-weight:700;line-height:1.25;color:#7f7;word-break:break-all;
      background:#14210f;border:1px solid #2a5;border-radius:10px;padding:10px;margin-bottom:8px}
 .ips small{display:block;font-size:12px;font-weight:400;color:#9a9;margin-top:6px}
 .big{font-size:19px;font-weight:600;background:#161616;border:1px solid #333;
      border-radius:10px;padding:10px;margin-bottom:10px;line-height:1.5}
 .big b{color:#7f7}
 img.stream{width:100%;border:1px solid #333;border-radius:10px;background:#000}
 h2{font-size:15px;margin:16px 0 8px;color:#9cf;font-weight:600}
 #wall{display:flex;flex-wrap:wrap;margin:0 -1%}
 #wall a{width:31.33%;margin:1%;display:block}
 #wall img{width:100%;border:1px solid #333;border-radius:8px;background:#111;display:block}
 #evs{font-size:13px;line-height:1.6;color:#bbb}
 #evs div{border-bottom:1px solid #222;padding:4px 0}
 #evs .det{color:#ff9a9a}
 #banner{position:fixed;left:0;right:0;top:0;z-index:99;display:none;
         background:#c62828;color:#fff;font-size:19px;font-weight:700;
         padding:14px 12px;text-align:center;box-shadow:0 2px 12px #000}
 .tip{font-size:12px;color:#888;margin-top:8px}
 button{background:#2a5;color:#fff;border:0;border-radius:8px;padding:10px 14px;
        font-size:15px;margin-top:8px}
</style></head><body>
<div id="banner">⚠ 检出缺陷</div>
<h1>RK3576 · FPGA 硬件 JPEG 缺陷检测（手机页）</h1>
<div class="ips">@@IPS@@<small>手机与板卡连同一个网，浏览器输入上面地址 + /m</small></div>
<div class="big" id="big">@@STATUS_LINE@@</div>
<img class="stream" src="/stream.mjpg?q=40&w=640&maxfps=5" alt="实时画面">
<div class="tip">画面走低带宽参数 q=40 / w=640 / maxfps=5（静止画面几乎不耗流量）</div>
<h2>最新缺陷图（点开看原图）</h2>
<div id="wall"></div>
<h2>最近事件</h2>
<div id="evs"></div>
<button id="snd">开启提示音</button>
<div class="tip">提示音要手机浏览器允许播放媒体，点一下上面的按钮即可</div>
<script>
var NEXT = @@NEXT@@;        // /api/events 的游标(首屏由服务端填, 之后只拉增量)
var EVS = [];               // 最近 10 条
var AC = null;              // WebAudio 上下文(点按钮时创建/恢复)

function pct(v){ return (v === null || v === undefined) ? '--' : (v * 100).toFixed(1) + '%'; }
function qname(q){ return 'Q' + [30, 50, 75, 90][q | 0]; }

function beep(){
  try{
    if(!AC){ AC = new (window.AudioContext || window.webkitAudioContext)(); }
    if(AC.state === 'suspended'){ AC.resume(); }
    var o = AC.createOscillator(), g = AC.createGain();
    o.type = 'square'; o.frequency.value = 880;
    g.gain.setValueAtTime(0.0001, AC.currentTime);
    g.gain.exponentialRampToValueAtTime(0.22, AC.currentTime + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, AC.currentTime + 0.28);
    o.connect(g); g.connect(AC.destination);
    o.start(); o.stop(AC.currentTime + 0.30);
  }catch(e){}
}

function banner(ev){
  var el = document.getElementById('banner');
  var who = (ev && ev.cls) ? ev.cls : '目标';
  var sc = (ev && ev.score !== null && ev.score !== undefined)
           ? (' ' + Number(ev.score).toFixed(2)) : '';
  el.textContent = '⚠ 检出 ' + who + sc;
  el.style.display = 'block';
  clearTimeout(banner._t);
  banner._t = setTimeout(function(){ el.style.display = 'none'; }, 3000);
  beep();
  refreshWall();
}

function renderEvents(){
  var box = document.getElementById('evs');
  if(!EVS.length){ box.innerHTML = '<div>（还没有事件）</div>'; return; }
  var h = '';
  for(var i = 0; i < EVS.length; i++){
    var e = EVS[i];
    var t = new Date((e.t || 0) * 1000);
    var hh = ('0' + t.getHours()).slice(-2) + ':' + ('0' + t.getMinutes()).slice(-2) +
             ':' + ('0' + t.getSeconds()).slice(-2);
    var who = e.type === 'detect' ? ('检出 ' + (e.cls || '?') +
              (e.score === null || e.score === undefined ? '' : (' ' + Number(e.score).toFixed(2))))
                                  : (e.type === 'manual' ? '手动截图' : e.type);
    h += '<div' + (e.type === 'detect' ? ' class="det"' : '') + '>' + hh + ' · ' + who +
         (e.file ? (' · ' + e.file) : '') + '</div>';
  }
  box.innerHTML = h;
}

function refreshWall(){
  fetch('/api/defects', {cache:'no-store'}).then(function(r){ return r.json(); })
  .then(function(j){
    var fs = (j.files || []).slice(0, @@DEFECTS@@);
    var h = '';
    for(var i = 0; i < fs.length; i++){
      var u = '/defects/' + encodeURIComponent(fs[i].name);
      h += '<a href="' + u + '" target="_blank"><img loading="lazy" src="' + u + '"></a>';
    }
    document.getElementById('wall').innerHTML = h || '<div class="tip">（还没有缺陷图）</div>';
  }).catch(function(){});
}

function tickStatus(){
  fetch('/api/status', {cache:'no-store'}).then(function(r){ return r.json(); })
  .then(function(j){
    document.getElementById('big').innerHTML =
      '压缩率 <b>' + pct(j.ratio) + '</b> ｜ 帧率 <b>' + Number(j.fps || 0).toFixed(1) +
      '</b> fps<br>档位 <b>' + qname(j.qsel) + '</b> ｜ 抽样 <b>' + pct(j.cover) +
      '</b>（本窗口） ｜ ' + (j.auto ? '自适应' : '手动') +
      (j.target_kbps ? (' 目标 ' + j.target_kbps + 'KB/s') : '');
  }).catch(function(){});
}

function pollEvents(){
  fetch('/api/events?since=' + NEXT, {cache:'no-store'}).then(function(r){ return r.json(); })
  .then(function(j){
    var evs = j.events || [];
    if(typeof j.next === 'number'){ NEXT = j.next; }
    if(!evs.length){ return; }
    for(var i = evs.length - 1; i >= 0; i--){ EVS.unshift(evs[i]); }
    EVS = EVS.slice(0, @@EVENTS@@);
    renderEvents();
    var lastDet = null;
    for(var k = 0; k < evs.length; k++){ if(evs[k].type === 'detect'){ lastDet = evs[k]; } }
    if(lastDet){ banner(lastDet); }
  }).catch(function(){});
}

document.getElementById('snd').onclick = function(){ beep(); this.textContent = '提示音已开启'; };
refreshWall(); renderEvents();
setInterval(tickStatus, 1000); tickStatus();
setInterval(pollEvents, 1000); pollEvents();
</script>
</body></html>"""


def mobile_page_html(status, ips, push=None, next_seq=0):
    """手机页 /m: 把"本机 IP + 首屏数值 + 事件游标"塞进模板(只做字符串替换, 不用 % 格式化)"""
    st = dict(status or {})
    ips = list(ips or [])
    if not ips:
        ip_html = '（没取到网卡地址，用 ifconfig/串口查）'
    else:
        ip_html = '&nbsp;&nbsp;'.join('http://' + ip + ':@@PORT@@/m' for ip in ips)
    try:
        qname = QNAME.get(int(st.get('qsel') or 0), '?')
    except (TypeError, ValueError):
        qname = '?'
    ratio = st.get('ratio')
    cover = st.get('cover')
    line = ('压缩率 <b>%s</b> ｜ 帧率 <b>%.1f</b> fps<br>档位 <b>%s</b> ｜ 抽样 <b>%s</b>（本窗口）'
            % (('--' if ratio is None else '%.1f%%' % (float(ratio) * 100.0)),
               float(st.get('fps') or 0.0), qname,
               ('--' if cover is None else '%.1f%%' % (float(cover) * 100.0))))
    push = push or {}
    if push.get('enabled'):
        line += '<br>推送 <b>开</b> %s（ok %s / fail %s）' % (
            push.get('url') or '', push.get('ok', 0), push.get('fail', 0))
    else:
        line += '<br>推送 <b>关</b>'
    port = int(st.get('_port') or HTTP_DEFAULT_PORT)
    return (_M_PAGE
            .replace('@@IPS@@', ip_html)
            .replace('@@STATUS_LINE@@', line)
            .replace('@@PORT@@', str(port))
            .replace('@@DEFECTS@@', str(MOBILE_PAGE_DEFECTS))
            .replace('@@EVENTS@@', str(MOBILE_PAGE_EVENTS))
            # 首屏往回退 10 条, 这样"最近 10 条事件"一打开就有内容(不是空的)
            .replace('@@NEXT@@', str(max(0, int(next_seq or 0) - MOBILE_PAGE_EVENTS))))


class _HttpHandler(BaseHTTPRequestHandler):
    server_version = 'rk-pcb-view/1.0'
    protocol_version = 'HTTP/1.0'        # 无 Content-Length 的 MJPEG 只能这样

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Cache-Control', 'no-store')

    def log_message(self, fmt, *args):
        if getattr(self.server, 'quiet', True):
            return
        BaseHTTPRequestHandler.log_message(self, fmt, *args)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                self._send(500, 'text/plain; charset=utf-8',
                           ('ERR %s' % exc).encode('utf-8'))
            except Exception:
                pass

    def do_POST(self):
        """POST 只用于 /api/push(t22): 参数可以放 query 也可以放 form body"""
        try:
            raw = self.path or '/'
            path = raw.split('?', 1)[0].split('#', 1)[0]
            query = raw.split('?', 1)[1] if '?' in raw else ''
            body = ''
            try:
                n = int(self.headers.get('Content-Length') or 0)
            except (TypeError, ValueError):
                n = 0
            if 0 < n <= 8192:
                body = self.rfile.read(n).decode('utf-8', 'replace')
            merged = '&'.join([x for x in (query, body.strip()) if x])
            if path == '/api/push':
                self._push(merged)
            else:
                self._send(404, 'text/plain; charset=utf-8',
                           b'404 post not supported here')
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                self._send(500, 'text/plain; charset=utf-8',
                           ('ERR %s' % exc).encode('utf-8'))
            except Exception:
                pass

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self._cors()
        for k, v in (extra or {}).items():
            self.send_header(str(k), str(v))
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        hub = self.server.hub
        raw = self.path
        path = raw.split('?', 1)[0].split('#', 1)[0]
        query = raw.split('?', 1)[1] if '?' in raw else ''
        store = getattr(self.server, 'store_dir', DEFECT_DIR)
        if path in ('/', '/index.html'):
            self._send(200, 'text/html; charset=utf-8', _HTTP_PAGE.encode('utf-8'))
        elif path in ('/m', '/m/', '/mobile'):
            self._mobile()
        elif path == '/api/net':
            self._net()
        elif path == '/api/dim':
            self._dim(query)
        elif path == '/api/status':
            st = hub.status()
            rt = getattr(self.server, 'runtime', RUNTIME)
            st.update(rt.get())                        # autosave / detect (t11 运行时开关)
            st['sd_free'] = free_bytes(store)          # SD 卡剩余字节(int)
            st['save_dir'] = os.path.abspath(store)    # 存图目录(绝对路径)
            self._send(200, 'application/json', json.dumps(st).encode('utf-8'))
        elif path == '/api/config':
            self._config(query)
        elif path == '/api/push':
            self._push(query)
        elif path == '/api/events':
            self._events(query)
        elif path == '/api/snap':
            self._snap()
        elif path in ('/snap.jpg', '/snap', '/snapshot.jpg'):
            o = parse_jpeg_opts(query, default_w=getattr(self.server, 'maxw', 0) or 0)
            jpg = hub.jpeg(max_w=o['w'], quality=o['q'])
            if jpg is None:
                self._send(503, 'text/plain; charset=utf-8', b'503 no frame yet')
            else:
                self._send(200, 'image/jpeg', jpg,
                           extra=self._jpeg_headers(o))
        elif path == '/stream.mjpg':
            self._stream(query)
        elif path == '/api/defects':
            body = json.dumps(dict(files=list_defect_files(store))).encode('utf-8')
            self._send(200, 'application/json', body)
        elif path.startswith('/defects/'):
            self._defect(path[len('/defects/'):])
        else:
            self._send(404, 'text/plain; charset=utf-8', b'404 not found')

    def _jpeg_headers(self, o):
        """把**实际生效**的低带宽参数回在响应头里, 现场能一眼核对钳位结果"""
        return {'X-JPEG-Q': str(o['q']), 'X-JPEG-W': str(o['w']),
                'X-JPEG-FPS': '%.1f' % o['maxfps'],
                'X-JPEG-DIFF': '%.1f' % o['diff']}

    def _net(self):
        """GET /api/net —— 本机网卡 IPv4 + 手机页 URL(t14), 给 Qt 的二维码取地址用。

        {"ok":true,"ifs":[{name,addr,up,carrier,wireless},...],
         "urls":{"mobile":["http://<ip>:<实际监听端口>/m",...]}}
        · ifs / urls.mobile 都是**无线优先**
        · 端口取服务**真正监听的端口**(--http 给的那个), 不是写死 8000
        · 枚举失败/没有网卡 -> ifs=[] 且 ok:true(绝不 500)
        """
        try:
            port = int(self.server.server_address[1])
        except Exception:
            port = int(getattr(self.server, 'server_port', 0) or HTTP_DEFAULT_PORT)
        ifs = net_ifaces()
        body = dict(ok=True, ifs=ifs, urls=net_urls(port, ifs))
        self._send(200, 'application/json', json.dumps(body).encode('utf-8'))

    def _mobile(self):
        """GET /m —— 手机低带宽页(IP/压缩率/fps/档位/cover + 低带宽推流 + 缺陷墙 + 告警)"""
        hub = self.server.hub
        st = hub.status()
        rt = getattr(self.server, 'runtime', RUNTIME)
        st.update(rt.get())
        p = getattr(self.server, 'pusher', None)
        push = p.get() if p is not None else {}
        log = getattr(self.server, 'events', None)
        try:
            nxt = log.last_seq() if log is not None else 0
        except Exception:
            nxt = 0
        st['_port'] = int(self.server.server_address[1])
        html = mobile_page_html(st, local_ips(), push, nxt)
        self._send(200, 'text/html; charset=utf-8', html.encode('utf-8'))

    def _push(self, query):
        """GET/POST /api/push —— 远端主动推送开关(t22), 契约见 HTTP_BLOCK 头注释。

        · 不传参数 = 只查询(GET 的字段: enabled/url/queued/ok/fail/last_error)
        · ?enabled=0|1 / ?url=http(s)://... / ?test=1(立刻推一张测试图)
        · 非 http/https 的 url -> 400 + {"ok":false,"error":"bad url"}
        """
        p = getattr(self.server, 'pusher', None)
        if p is None:
            self._send(503, 'application/json',
                       b'{"ok": false, "error": "no pusher"}')
            return
        args, bad = {}, None
        for part in (query or '').split('&'):
            if not part:
                continue
            key, _sep, val = part.partition('=')
            key, val = unquote(key).strip(), unquote(val).strip()
            if key == 'enabled':
                if val not in ('0', '1'):
                    bad = 'bad enabled=%s' % val
                else:
                    args['enabled'] = int(val)
            elif key == 'url':
                if val and not is_http_url(val):
                    self._send(400, 'application/json',
                               json.dumps(dict(ok=False, error='bad url')
                                          ).encode('utf-8'))
                    return
                args['url'] = val
            elif key == 'test':
                args['test'] = 1 if val in ('1', 'true', 'yes') else 0
        if bad:
            self._send(400, 'application/json',
                       json.dumps(dict(ok=False, error=bad)).encode('utf-8'))
            return
        if not args:                       # 不传参数 = 只查询(字段与契约完全一致)
            self._send(200, 'application/json',
                       json.dumps(p.get()).encode('utf-8'))
            return
        # [D29] 开推送但板卡上还没存 url -> 明确报错, 不许静默忽略.
        #   旧行为: enabled=1 被 `want = ... and bool(self._url)` 静默变成 0,
        #   返回 ok:true 但 changed 里没有 enabled -> Qt 的 pending 永远等不到回读,
        #   两个按钮一直置灰, 用户看到的就是"关不掉/开不了推送".
        if args.get('enabled') == 1 and not (args.get('url')
                                             or (p.get() or {}).get('url')):
            self._send(400, 'application/json',
                       json.dumps(dict(ok=False,
                                       error='url empty (set ?url= first)')
                                  ).encode('utf-8'))
            return
        st = p.apply(args)
        out = dict(ok=True, enabled=st['enabled'], url=st['url'],
                   changed=st['changed'])
        if args.get('test'):
            # 立刻塞一张测试图进同一个队列(走完全一样的 POST 路径)
            hub = self.server.hub
            jpg = hub.jpeg(max_w=640, quality=60)
            meta = hub.status()
            n = p.enqueue(jpg, dict(seq=0, type='test', cls=None, score=None,
                                    ratio=meta.get('ratio'),
                                    fps=meta.get('fps'),
                                    cover=meta.get('cover'), box=None))
            out['test_queued'] = 1 if n else 0
            if not n:
                out['warning'] = 'not enabled or no frame'
        self._send(200, 'application/json', json.dumps(out).encode('utf-8'))

    def _config(self, query):
        """GET /api/config?autosave=0|1&detect=0|1&dim=0|1 —— 运行时开关, 改完立刻生效。

        不传参数 = 只查询; 只改传了的那个; 非法值 400 + {"ok":false,"error":...}
        **detect 与 dim 互斥**(t18): dim=1 自动 detect=0, detect=1 自动 dim=0,
        被自动关掉的那项会出现在 changed 里 —— 回读就能看出互斥结果。
        """
        rt = getattr(self.server, 'runtime', RUNTIME)
        args = {}
        for part in (query or '').split('&'):
            if not part:
                continue
            key, _sep, val = part.partition('=')
            key, val = unquote(key).strip(), unquote(val).strip()
            if key in ('autosave', 'detect', 'dim'):
                if val not in ('0', '1'):
                    self._send(400, 'application/json',
                               json.dumps(dict(ok=False,
                                               error='bad %s=%s' % (key, val))
                                          ).encode('utf-8'))
                    return
                args[key] = int(val)
            elif key == 'dim_auto_sec':
                # 数值型: 非法/越界**钳位**(不 400), 免得 Qt 拖个滑条就被打回
                try:
                    args['dim_auto_sec'] = int(float(val))
                except (TypeError, ValueError):
                    args['dim_auto_sec'] = DIM_AUTO_SEC_DEFAULT
            elif key == 'dim_auto':
                # 便捷开关: dim_auto=1 -> 用默认间隔(DIM_AUTO_SEC_DEFAULT), 0 -> 关
                if val not in ('0', '1'):
                    self._send(400, 'application/json',
                               json.dumps(dict(ok=False,
                                               error='bad %s=%s' % (key, val))
                                          ).encode('utf-8'))
                    return
                args['dim_auto_sec'] = DIM_AUTO_SEC_DEFAULT if val == '1' else 0
        state, warn = rt.apply(args)
        out = dict(ok=True, autosave=state['autosave'],
                   detect=state['detect'], dim=state.get('dim', 0),
                   dim_auto_sec=state.get('dim_auto_sec', 0),
                   changed=state['changed'])
        # 开了测量模式但板上没有 dim_measure.py(或缺 scipy) -> ok:true + warning,
        # 状态保持 dim=1(操作员去 /api/dim 能看清真实原因), 和"detect 没模型"一个风格
        if state.get('dim'):
            eng = getattr(self.server, 'dim', None)
            if eng is not None and not eng.available():
                out['warning'] = 'dim module not loaded'
        if warn:
            out['warning'] = warn
        self._send(200, 'application/json', json.dumps(out).encode('utf-8'))

    def _dim(self, query):
        """GET /api/dim[?run=1][?calib=1] —— 尺寸测量(t18), schema 由 tools/dim_measure.py 冻结。

        · 不带参数: **只读最近一次结果**, 不测量、不阻塞(Qt 轮询走这条)
        · ?run=1  : 用最近一帧**立刻量一次**(Qt 的"测量"按钮走这条, 会等 ~0.3 s;
                    还没标定就先起后台标定并返回 state=calib, 不干等)
        · ?calib=1: 用最新帧后台重标一次(不阻塞)
        · calib_ok=false 时**一个 value_mm 都不给**, 只给 calib_note(刻意如此)
        · /api/status 的 15 个字段不受影响 —— 测量信息只在 /api/dim
        """
        eng = getattr(self.server, 'dim', None)
        want_run = want_calib = False
        for part in (query or '').split('&'):
            key, _sep, val = part.partition('=')
            key, val = unquote(key).strip(), unquote(val).strip()
            if key == 'run':
                want_run = val in ('1', 'true', 'yes')
            elif key == 'calib':
                want_calib = val in ('1', 'true', 'yes')
        if eng is None:                       # 理论上不会: 服务总带一个 DimEngine
            self._send(503, 'application/json',
                       b'{"ok": false, "error": "no dim engine"}')
            return
        frame = self.server.hub.frame() if (want_run or want_calib) else None
        queued = 0
        if want_calib:
            queued = 1 if eng.request_calibrate(frame) else 0
        fresh = None
        if want_run:
            # 【2026-10-26 S1】测量已是标定驱动、**不再读帧** → 只要标定过就能出结果,
            #   不再要求 hub 当前有帧(以前没帧时 run=1 只能回 why="no frame")。
            if eng.calibrated():
                st = self.server.hub.status()
                fresh = eng.measure_now(fps=st.get('fps'), sync=True)
            elif frame is not None:
                # 还没标定 -> 只把标定排进后台, 不干等(客户端看 state + age_s=null 就知道)
                queued = 1 if eng.request_calibrate(frame) else queued
        body = dict(eng.snapshot())           # 13 键(冻结 schema + 板卡侧补充)
        if fresh is not None:                 # 本次触发的新鲜结果盖掉缓存里的那几项
            for k in ('schema', 'calib_ok', 'calib_note', 'sigma_mm', 'perf_ms',
                      'targets', 'state'):
                if k in fresh:
                    body[k] = fresh[k]
            body['accuracy_mm'] = body.get('sigma_mm')
        body['ok'] = True
        body['recalib_queued'] = queued
        body['busy'] = 1 if eng.busy() else 0
        _ag = eng.age()
        body['age_s'] = None if _ag is None else round(_ag, 2)
        # **只按冻结契约出这 17 个键**: 引擎内部字段(例如曾经的 measured_t)一律不漏到
        # 响应里 —— "上次什么时候量的"用契约内的 age_s 表达(t24 与 verifier 对齐)。
        out = dict((k, body.get(k)) for k in DIM_API_KEYS)
        self._send(200, 'application/json',
                   json.dumps(out, ensure_ascii=False).encode('utf-8'))

    def _events(self, query):
        """GET /api/events?since=N -> {"next": M, "events": [...]}, 最多 200 条"""
        log = getattr(self.server, 'events', None)
        since = 0
        for part in (query or '').split('&'):
            if part.startswith('since='):
                try:
                    since = int(float(unquote(part[len('since='):]) or 0))
                except (TypeError, ValueError):
                    since = 0
        if log is None:
            self._send(200, 'application/json',
                       b'{"next": 0, "events": []}')
            return
        self._send(200, 'application/json',
                   json.dumps(log.since(max(since, 0))).encode('utf-8'))

    def _snap(self):
        """GET /api/snap -> 把当前最新帧存成 manual_HHMMSS.jpg 并记一条 manual 事件;
        开了主动推送就把这张图也塞进推送队列(type=manual)"""
        hub = self.server.hub
        store = getattr(self.server, 'store_dir', DEFECT_DIR)
        frame = hub.frame()
        if frame is None:
            self._send(503, 'application/json',
                       b'{"ok": false, "error": "no frame"}')
            return
        stem = time.strftime('manual_%H%M%S', time.localtime())
        name = unique_name(store, stem, '.jpg')
        path = os.path.join(store, name)
        if not save_jpeg(path, frame, 85):
            self._send(500, 'application/json',
                       b'{"ok": false, "error": "write failed"}')
            return
        try:
            size = int(os.path.getsize(path))
        except OSError:
            size = 0
        log = getattr(self.server, 'events', None)
        seq = 0
        if log is not None:
            st = hub.status()
            ev = log.add('manual', name, size, None, None,
                         st.get('ratio'), st.get('fps'), None)
            try:
                seq = int(ev.get('seq') or 0)
            except Exception:
                seq = 0
        # [t22] 主动推送: 只入队(纯内存), 网络 IO 在 pcb-push 线程里
        p = getattr(self.server, 'pusher', None)
        if p is not None:
            try:
                data = None
                f = open(path, 'rb')
                try:
                    data = f.read()
                finally:
                    f.close()
                st2 = hub.status()
                p.enqueue(data, dict(seq=seq, type='manual', cls=None, score=None,
                                     ratio=st2.get('ratio'), fps=st2.get('fps'),
                                     cover=st2.get('cover'), box=None))
            except Exception as exc:
                print('[WARN] manual 推送入队失败: %s' % exc)
        self._send(200, 'application/json',
                   json.dumps(dict(ok=True, file=name, size=size,
                                   dir=os.path.abspath(store))).encode('utf-8'))

    def _defect(self, rawname):
        path = _safe_defect_path(getattr(self.server, 'store_dir', DEFECT_DIR),
                                 rawname)
        if path is None:
            self._send(403, 'text/plain; charset=utf-8', b'403 bad name')
            return
        if not os.path.isfile(path):
            self._send(404, 'text/plain; charset=utf-8', b'404 no such defect')
            return
        try:
            f = open(path, 'rb')
            try:
                data = f.read()
            finally:
                f.close()
        except OSError as exc:
            self._send(500, 'text/plain; charset=utf-8', ('ERR %s' % exc).encode())
            return
        ctype = ('image/jpeg' if path.lower().endswith(('.jpg', '.jpeg'))
                 else 'application/octet-stream')
        self._send(200, ctype, data)

    def _stream(self, query=''):
        """GET /stream.mjpg[?q=&w=&maxfps=&diff=] —— 低带宽参数见 parse_jpeg_opts()

        diff>0 时: 与"上次**真正发出去**的那帧"的 64x36 灰度平均差 < diff 就**不发**,
        手机上画面静止时几乎没有流量(但连接不断, 一变就立刻补一帧)。"""
        hub = self.server.hub
        o = parse_jpeg_opts(query, default_w=getattr(self.server, 'maxw', 0) or 0)
        interval = 1.0 / max(o['maxfps'], 0.1)
        self.send_response(200)
        self.send_header('Content-Type',
                         'multipart/x-mixed-replace; boundary=frame')
        self._cors()
        for k, v in self._jpeg_headers(o).items():
            self.send_header(k, v)
        self.end_headers()
        last = None
        last_sig = None
        sleep_s = max(0.02, min(0.2, interval / 5.0))
        try:
            while True:
                jpg = hub.jpeg(max_w=o['w'], quality=o['q'], maxfps=o['maxfps'])
                if jpg is not None and jpg is not last:
                    sig = _gray_sig(hub.frame()) if o['diff'] > 0 else None
                    skip = False
                    if o['diff'] > 0 and sig is not None and last_sig is not None:
                        d = gray_diff(sig, last_sig)
                        skip = (d is not None and d < o['diff'])
                    if not skip:
                        self.wfile.write(b'--frame\r\n')
                        self.wfile.write(b'Content-Type: image/jpeg\r\n')
                        self.wfile.write(b'Content-Length: ' +
                                         str(len(jpg)).encode() + b'\r\n\r\n')
                        self.wfile.write(jpg)
                        self.wfile.write(b'\r\n')
                        self.wfile.flush()
                        last = jpg
                        if sig is not None:
                            last_sig = sig
                time.sleep(sleep_s)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class HttpFrameServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, hub, store_dir=DEFECT_DIR, events=None,
                 runtime=None, quiet=True, maxw=0, pusher=None, dim=None):
        self.hub = hub
        self.store_dir = store_dir
        self.events = events
        self.runtime = runtime if runtime is not None else RUNTIME
        self.quiet = quiet
        self.maxw = maxw
        # [t22] 主动推送线程: 没有外部实例就自己建一个(**启动即建**, 默认关着不发)
        self.pusher = pusher if pusher is not None else FramePusher()
        # [t18] 尺寸测量适配层: 没有外部实例就自己建(模块不可用时 state=bad, /api/dim 照样有结构)
        self.dim = dim if dim is not None else DimEngine()
        ThreadingHTTPServer.__init__(self, addr, _HttpHandler)


class DefectSaver(object):
    """检出缺陷时把整帧存成 det_HHMMSS.jpg, 供 PC 端 /api/defects 拉取"""

    def __init__(self, outdir=DEFECT_DIR, min_interval=DEFECT_MIN_INTERVAL):
        self.outdir = outdir
        self.min_interval = min_interval
        self.last_t = 0.0
        self.saved = 0
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError:
            pass

    def save(self, frame, boxes=None):
        now = time.time()
        if now - self.last_t < self.min_interval:
            return None
        self.last_t = now
        name = time.strftime('det_%H%M%S', time.localtime(now)) + '.jpg'
        path = os.path.join(self.outdir, name)
        if save_jpeg(path, frame, 85):
            self.saved += 1
            print('\n[SAVE] 缺陷图 %s (%d 个目标)' %
                  (path, len(boxes) if boxes is not None else 0))
            return path
        return None


def save_defect_if_enabled(saver, frame, boxes, scores, cls_ids, events=None,
                           wm_info=None, fps=0.0, runtime=None, pusher=None):
    """主循环里的"检出就存一张 det_HHMMSS.jpg + 记一条 detect 事件"。

    autosave=0 时**直接返回 None**（不写盘、不发事件、不碰 SD 卡），
    手动截图 /api/snap 走的是另一条路，不受这个开关影响。
    给了 pusher(主动推送) 时: 存成功后**只入队**(纯内存)一张 JPEG + 事件头,
    网络 IO 全在 pcb-push 线程里, 主循环不会被拖住(t22)。
    抽成函数是为了让 --http-selftest 能直接验"关掉自动存图后不再写盘/不再发事件"。"""
    rt = runtime if runtime is not None else RUNTIME
    if saver is None or not rt.autosave():
        return None
    try:
        n = len(scores)
    except TypeError:
        n = 0
    if not n:
        return None
    path = saver.save(frame, boxes)
    if path is None:
        return None
    seq = 0
    if events is not None:
        try:
            best = int(np.argmax(scores))
            cls_name = class_name_of(cls_ids[best])   # 自定义类别/越界 cls_<id> 都走这里
            ev = events.add('detect', os.path.basename(path),
                            os.path.getsize(path), cls_name, float(scores[best]),
                            (wm_info or {}).get('ratio'), float(fps or 0.0), None)
            try:
                seq = int(ev.get('seq') or 0)
            except Exception:
                seq = 0
        except Exception as exc:
            print('[WARN] detect 事件记录失败: %s' % exc)
    # [t22] 主动推送: 只入队; 队列满了由 enqueue 丢最旧(fail+1), 不会阻塞主循环
    if pusher is not None and pusher.enabled():
        try:
            best = int(np.argmax(scores))
            cls_name = class_name_of(cls_ids[best])
            box = None
            try:
                if boxes is not None and len(boxes) > best:
                    box = [int(v) for v in list(boxes[best])[:4]]
            except Exception:
                box = None
            pusher.enqueue(encode_jpeg(frame, 0, 80),
                           dict(seq=seq, type='detect', cls=cls_name,
                                score=float(scores[best]),
                                ratio=(wm_info or {}).get('ratio'),
                                fps=float(fps or 0.0),
                                cover=frame_cover((wm_info or {}).get('blocks')),
                                box=box))
        except Exception as exc:
            print('[WARN] detect 推送入队失败: %s' % exc)
    return path


def start_http_server(hub, port=HTTP_DEFAULT_PORT, store_dir=DEFECT_DIR,
                      events=None, runtime=None, quiet=True,
                      maxw=HTTP_STREAM_MAXW, host='0.0.0.0', pusher=None,
                      dim=None):
    """启动后台 HTTP 线程(daemon)。port=0 用系统临时端口。返回 (server, thread)"""
    try:
        os.makedirs(store_dir, exist_ok=True)
    except OSError:
        pass
    srv = HttpFrameServer((host, int(port)), hub, store_dir, events, runtime,
                          quiet, maxw, pusher, dim)
    th = threading.Thread(target=srv.serve_forever,
                          kwargs=dict(poll_interval=0.2),
                          name='pcb-http', daemon=True)
    th.start()
    print('[HTTP] 服务已启动 http://%s:%d/  手机页 /m  存图目录 %s'
          % ('<板卡IP>' if host == '0.0.0.0' else host,
             srv.server_address[1], store_dir))
    return srv, th


def _parse_http_opts(argv):
    """从命令行解析 HTTP 相关参数（--selftest 也要能用）。
    支持 `--http 8000` 与 `--http=8000` 两种写法。"""
    def _val(flag, default=None):
        pre = flag + '='
        for a in argv:
            if a.startswith(pre):
                return a[len(pre):]
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv) and not argv[i + 1].startswith('--'):
                return argv[i + 1]
        return default

    has_http = ('--http' in argv) or any(a.startswith('--http=') for a in argv)
    sd_given = ('--save-dir' in argv) or any(
        a.startswith('--save-dir=') for a in argv)
    opts = dict(enabled=False, port=0, selftest='--http-selftest' in argv,
                save_dir=(_val('--save-dir') or DEFECT_DIR),
                save_dir_given=sd_given,
                maxw=HTTP_STREAM_MAXW, verbose='--http-verbose' in argv,
                defect_save=('--no-defect-save' not in argv))
    mw = _val('--http-maxw')
    if mw is not None:
        try:
            opts['maxw'] = int(mw)
        except ValueError:
            pass
    if has_http:
        try:
            opts['port'] = int(_val('--http'))
        except (TypeError, ValueError):
            opts['port'] = HTTP_DEFAULT_PORT
        opts['enabled'] = True
    if opts['selftest']:
        # 自检: --http 给了就用它，没给就让系统分临时端口(避免 8000 被占)
        opts['enabled'] = True
    return opts


def _synth_frame(w=480, h=270):
    """自检用的合成帧(渐变+白块), 不依赖摄像头"""
    yy = np.linspace(0, 255, h, dtype=np.float32).reshape(h, 1)
    xx = np.linspace(0, 255, w, dtype=np.float32).reshape(1, w)
    f = np.empty((h, w, 3), dtype=np.uint8)
    f[:, :, 0] = xx.astype(np.uint8)
    f[:, :, 1] = yy.astype(np.uint8)
    f[:, :, 2] = ((xx + yy) * 0.5).astype(np.uint8)
    f[8:24, 8:120] = 255
    f[h - 24:h - 8, w - 120:w - 8] = 255
    return f


def _wm_frame(value, w=640, h=360):
    """把 64bit 水印画进一帧(自检用): 左上角 512x8, 每 bit 8x8, 左=MSB。
    走这条路径才能验证"画面 -> decode_wm -> parse_wm -> publish -> /api/status"整链"""
    img = np.full((h, w, 3), 60, dtype=np.uint8)
    img[:, :, 1] = 90
    for i in range(64):
        bit = (value >> (63 - i)) & 1
        img[0:ROI_H, i * CELL:(i + 1) * CELL] = 235 if bit else 16
    return img


def _wm_encode_value(auto=0, target5=0, qsel=2, blocks=240, nbytes=2216,
                     fcnt=7):
    """按新布局拼 64bit 水印值(自检/回归用)"""
    return (((int(auto) & 1) << 63) | ((int(target5) & 0x1F) << 58) |
            ((int(qsel) & 3) << 56) | ((int(blocks) & 0xFFFF) << 40) |
            ((int(nbytes) & 0xFFFFFF) << 16) | ((int(fcnt) & 0xFF) << 8) |
            WM_MAGIC)


def _http_selftest(argv=None):
    """--http-selftest: 合成帧注入共享缓冲 -> 起 HTTP 服务 -> urllib 验契约。
    本机(Windows)无摄像头、无 cv2 也能跑。"""
    import tempfile
    import shutil
    import urllib.request
    import urllib.error

    argv = list(sys.argv[1:] if argv is None else argv)
    opts = _parse_http_opts(argv)
    results = []

    def check(name, cond, extra=''):
        results.append((name, bool(cond)))
        print('[http-selftest] %-32s %s%s'
              % (name, 'PASS' if cond else 'FAIL', ('  ' + extra) if extra else ''))
        return bool(cond)

    tmpdir = tempfile.mkdtemp(prefix='pcb_http_selftest_')
    store = os.path.join(tmpdir, 'store')
    srv = None
    try:
        # ① save-dir: 建目录, 放一张 jpg + 一个 txt(非图片必须被过滤掉)
        os.makedirs(store, exist_ok=True)
        seed = encode_jpeg(_synth_frame(64, 48), 0, 80) or _MIN_JPEG
        f = open(os.path.join(store, 'det_142233.jpg'), 'wb')
        f.write(seed)
        f.close()
        f = open(os.path.join(store, 'readme.txt'), 'w')
        f.write('x')
        f.close()
        events = EventLog(store)
        rt_sel = RuntimeConfig()          # 自检用独立开关实例(默认 autosave=1/detect=0)

        # ② 合成帧注入共享缓冲(等价于主循环 publish)
        hub = FrameHub()
        wm_ok = dict(qsel=2, blocks=1440, nbytes=13200, fcnt=3, ratio=0.856)
        hub.publish(_synth_frame(), wm_ok, 10.8, 14.0)

        # ③ 起服务(指定端口被占就退到临时端口)
        port = int(opts.get('port') or 0)
        try:
            srv, _th = start_http_server(hub, port, store, events,
                                         runtime=rt_sel, quiet=True,
                                         host='127.0.0.1')
        except OSError:
            srv, _th = start_http_server(hub, 0, store, events, runtime=rt_sel,
                                         quiet=True, host='127.0.0.1')
        base = 'http://127.0.0.1:%d' % srv.server_address[1]
        print('[http-selftest] 服务地址 %s' % base)
        print('[http-selftest] 存图目录 %s' % store)
        print('[http-selftest] JPEG 编码器 %s'
              % ('cv2' if cv2 is not None else 'PIL/内置'))

        def get(path, timeout=5.0):
            req = urllib.request.Request(base + path)
            try:
                r = urllib.request.urlopen(req, timeout=timeout)
                return r.getcode(), dict(r.getheaders()), r.read()
            except urllib.error.HTTPError as exc:
                return exc.code, dict(exc.headers.items()), exc.read()

        # ---- /api/status ----
        code, hdr, body = get('/api/status')
        try:
            j = json.loads(body.decode('utf-8'))
        except Exception:
            j = {}
        check('/api/status 200', code == 200, 'code=%s' % code)
        check('/api/status 字段齐全',
              set(['ok', 'ratio', 'kbps', 'qsel', 'fps', 'blocks',
                   'nbytes', 'sd_free', 'save_dir', 'auto', 'target_kbps'])
              <= set(j.keys()), str(sorted(j.keys())))
        check('/api/status ok=true', j.get('ok') is True, repr(j.get('ok')))
        check('/api/status ratio=0.856',
              isinstance(j.get('ratio'), float) and abs(j['ratio'] - 0.856) < 1e-6,
              str(j.get('ratio')))
        check('/api/status kbps=10.8',
              abs(float(j.get('kbps', -1)) - 10.8) < 1e-6, str(j.get('kbps')))
        check('/api/status qsel=2', j.get('qsel') == 2, str(j.get('qsel')))
        check('/api/status fps=14.0',
              abs(float(j.get('fps', -1)) - 14.0) < 1e-6, str(j.get('fps')))
        check('/api/status blocks=1440', j.get('blocks') == 1440,
              str(j.get('blocks')))
        check('/api/status nbytes=13200', j.get('nbytes') == 13200,
              str(j.get('nbytes')))
        check('CORS 头 *',
              hdr.get('Access-Control-Allow-Origin') == '*',
              str(hdr.get('Access-Control-Allow-Origin')))
        print('[http-selftest] /api/status -> %s' % json.dumps(j, sort_keys=True))

        # ---- 没解到水印时 ratio 必须是 null ----
        hub.publish(_synth_frame(), None, 0.0, 0.0)
        _c, _h, b2 = get('/api/status')
        j2 = json.loads(b2.decode('utf-8'))
        check('无数据时 ratio=null', j2.get('ratio') is None, repr(j2.get('ratio')))
        hub.publish(_synth_frame(), wm_ok, 10.8, 14.0)

        # ---- /snap.jpg ----
        code, hdr, body = get('/snap.jpg')
        check('/snap.jpg 200', code == 200, 'code=%s' % code)
        check('/snap.jpg image/jpeg',
              str(hdr.get('Content-Type', '')).startswith('image/jpeg'),
              str(hdr.get('Content-Type')))
        check('/snap.jpg 合法 JPEG',
              body[:2] == b'\xff\xd8' and body[-2:] == b'\xff\xd9'
              and len(body) > 200, 'len=%d' % len(body))

        # ---- /api/defects ----
        code, hdr, body = get('/api/defects')
        try:
            d = json.loads(body.decode('utf-8'))
        except Exception:
            d = {}
        files = d.get('files')
        check('/api/defects files 是列表',
              code == 200 and isinstance(files, list), 'code=%s' % code)
        names = [x.get('name') for x in (files or [])]
        check('/api/defects 只列 jpg', names == ['det_142233.jpg'], str(names))
        f0 = (files or [{}])[0]
        check('/api/defects name/size/mtime',
              isinstance(f0.get('name'), str) and isinstance(f0.get('size'), int)
              and isinstance(f0.get('mtime'), int), str(f0))
        check('/api/defects size 正确', f0.get('size') == len(seed),
              '%s vs %d' % (f0.get('size'), len(seed)))
        print('[http-selftest] /api/defects -> %s' % json.dumps(d, sort_keys=True))

        # ---- /defects/<name> 下载 + 路径穿越防护 ----
        code, hdr, body = get('/defects/det_142233.jpg')
        check('/defects/<name> 内容一致', code == 200 and body == seed,
              'code=%s len=%d' % (code, len(body)))
        c1, _h, _b = get('/defects/%2e%2e%2fdet_142233.jpg')
        check('/defects 穿越(编码)被拒', c1 in (400, 403, 404), 'code=%s' % c1)
        c2, _h, _b = get('/defects/..%2F..%2Fetc%2Fpasswd')
        check('/defects 穿越(斜杠)被拒', c2 in (400, 403, 404), 'code=%s' % c2)
        bad = ['', '.', '..', '../x.jpg', 'a/b.jpg', '/etc/passwd',
               'C:\\x.jpg', '%2e%2e%2fx.jpg', '.hidden']
        check('_safe_defect_path 拒恶意名',
              all(_safe_defect_path(store, n) is None for n in bad),
              str(bad))
        check('_safe_defect_path 收正常名',
              _safe_defect_path(store, 'det_142233.jpg') ==
              os.path.join(os.path.abspath(store), 'det_142233.jpg'))

        # ---- /api/status 新增: sd_free + save_dir ----
        code, hdr, body = get('/api/status')
        js = json.loads(body.decode('utf-8'))
        check('/api/status sd_free 为 int 字节',
              isinstance(js.get('sd_free'), int) and js['sd_free'] > 0,
              str(js.get('sd_free')))
        check('/api/status save_dir 是存图目录',
              js.get('save_dir') == os.path.abspath(store),
              str(js.get('save_dir')))
        check('/api/status 原有 7 字段仍在',
              set(['ok', 'ratio', 'kbps', 'qsel', 'fps', 'blocks',
                   'nbytes']) <= set(js.keys()), str(sorted(js.keys())))

        # ---- 水印新布局: auto(bit63) + target5([62:58]) 走完整画面链路 ----
        wm_new = _wm_encode_value(auto=1, target5=4, qsel=2, blocks=240,
                                  nbytes=2216, fcnt=7)
        info_new = parse_wm(decode_wm(_wm_frame(wm_new)))
        check('画面->decode_wm->parse_wm 能解出 auto/target5',
              info_new is not None and info_new.get('auto') == 1
              and info_new.get('target5') == 4, str(info_new))
        hub.publish(_wm_frame(wm_new), info_new, 10.8, 14.0)
        _c, _h, b_new = get('/api/status')
        j_new = json.loads(b_new.decode('utf-8'))
        check('/api/status auto=1 (bit63 自适应)',
              j_new.get('auto') == 1, str(j_new.get('auto')))
        # [t22] 单位随位流版本变了: p20+ 是 40 KB/s/单位 -> target5=4 应为 160 KB/s
        # (本条原本断言 20 KB/s, 那是 p19 及以前的 5 KB/s/单位; 只改期望值, 断言仍在)
        check('/api/status target_kbps=160 (target5=4 x40KB/s, p20+)',
              j_new.get('target_kbps') == 160, str(j_new.get('target_kbps')))
        check('新布局不影响 qsel/blocks/nbytes',
              (j_new.get('qsel'), j_new.get('blocks'), j_new.get('nbytes'))
              == (2, 240, 2216),
              '%s/%s/%s' % (j_new.get('qsel'), j_new.get('blocks'),
                            j_new.get('nbytes')))
        check('新布局 ratio 仍按块数/字节算',
              abs((j_new.get('ratio') or 0) - (1 - 2216 / (240 * 64.0))) < 1e-9,
              str(j_new.get('ratio')))
        print('[http-selftest] 新布局 wm=0x%016X -> auto=%s target_kbps=%s'
              % (wm_new, j_new.get('auto'), j_new.get('target_kbps')))

        # ---- 旧位流兼容: 保留位 [63:58] 全 0 -> auto=0 / target_kbps=0, 不崩 ----
        wm_old = _wm_encode_value(auto=0, target5=0, qsel=2, blocks=240,
                                  nbytes=2216, fcnt=7)
        info_old = parse_wm(decode_wm(_wm_frame(wm_old)))
        hub.publish(_wm_frame(wm_old), info_old, 10.8, 14.0)
        _c, _h, b_old = get('/api/status')
        j_old = json.loads(b_old.decode('utf-8'))
        check('旧位流 auto=0(手动)',
              j_old.get('auto') == 0, str(j_old.get('auto')))
        check('旧位流 target_kbps=0(上位机显示 --)',
              j_old.get('target_kbps') == 0, str(j_old.get('target_kbps')))
        check('旧位流 qsel/ratio 照旧',
              j_old.get('qsel') == 2
              and abs((j_old.get('ratio') or 0) - 0.8557291666666667) < 1e-9,
              '%s / %s' % (j_old.get('qsel'), j_old.get('ratio')))
        hub.publish(_synth_frame(), wm_ok, 10.8, 14.0)   # 恢复, 不影响后面检查

        # ---- /api/events: 首次(无事件) ----
        code, _h, body = get('/api/events?since=0')
        je = json.loads(body.decode('utf-8'))
        check('/api/events 首次 200 + next/events',
              code == 200 and isinstance(je.get('next'), int)
              and isinstance(je.get('events'), list), str(body[:90]))
        check('/api/events 无事件时 events=[] 且 next=0',
              je.get('events') == [] and je.get('next') == 0,
              '%s / %s' % (je.get('events'), je.get('next')))
        next0 = je.get('next') or 0

        # ---- /api/snap: 落盘 + 返回 size + 产生 manual 事件 ----
        code, _h, body = get('/api/snap')
        jsn = json.loads(body.decode('utf-8'))
        check('/api/snap ok + file/size/dir',
              code == 200 and jsn.get('ok') is True
              and isinstance(jsn.get('file'), str) and int(jsn.get('size') or 0) > 0
              and jsn.get('dir') == os.path.abspath(store), str(jsn))
        snap_name = jsn.get('file') or ''
        snap_path = os.path.join(store, snap_name)
        check('/api/snap 文件真的落盘且 size 对',
              os.path.isfile(snap_path)
              and os.path.getsize(snap_path) == jsn.get('size'),
              '%s (%s B)' % (snap_path, jsn.get('size')))
        check('/api/snap 文件名 manual_HHMMSS.jpg',
              snap_name.startswith('manual_') and snap_name.endswith('.jpg')
              and len(snap_name) == len('manual_HHMMSS.jpg'), snap_name)
        c_dl, _h, b_dl = get('/defects/' + snap_name)
        check('截图可从 /defects/<name> 下回',
              c_dl == 200 and len(b_dl) == jsn.get('size'),
              'code=%s len=%d' % (c_dl, len(b_dl)))

        # ---- /api/events: 增量(manual) ----
        code, _h, body = get('/api/events?since=%d' % next0)
        je2 = json.loads(body.decode('utf-8'))
        evs = je2.get('events') or []
        check('/api/events 增量拿到 manual 事件',
              len(evs) == 1 and evs[0].get('type') == 'manual'
              and evs[0].get('file') == snap_name
              and evs[0].get('seq') == next0 + 1, str(evs[:1]))
        ev = evs[0] if evs else {}
        check('事件字段齐全(10 个)',
              set(['seq', 't', 'type', 'file', 'size', 'cls', 'score',
                   'ratio', 'fps', 'mode']) <= set(ev.keys()),
              str(sorted(ev.keys())))
        check('事件类型正确(seq/t/size/fps)',
              isinstance(ev.get('seq'), int) and isinstance(ev.get('t'), float)
              and isinstance(ev.get('size'), int)
              and isinstance(ev.get('fps'), float)
              and ev.get('cls') is None and ev.get('score') is None
              and ev.get('mode') is None, str(ev))
        print('[http-selftest] /api/events manual -> %s'
              % json.dumps(ev, sort_keys=True, ensure_ascii=False))

        # ---- /api/events: 空增量 next=last_seq ----
        code, _h, body = get('/api/events?since=%d' % (je2.get('next') or 0))
        je3 = json.loads(body.decode('utf-8'))
        check('/api/events 空增量 events=[] next=last_seq',
              je3.get('events') == [] and je3.get('next') == (je2.get('next') or 0),
              '%s / %s' % (je3.get('events'), je3.get('next')))

        # ---- /api/events: detect 事件(cls/score 取最高分) ----
        events.add('detect', 'det_142233.jpg', len(seed), 'person', 0.87,
                   0.856, 14.0, None)
        code, _h, body = get('/api/events?since=%d' % (je2.get('next') or 0))
        je4 = json.loads(body.decode('utf-8'))
        ev4 = (je4.get('events') or [{}])[0]
        check('/api/events detect 事件带 cls/score',
              ev4.get('type') == 'detect' and ev4.get('cls') == 'person'
              and abs(float(ev4.get('score') or 0.0) - 0.87) < 1e-6
              and abs(float(ev4.get('ratio') or 0.0) - 0.856) < 1e-6, str(ev4))

        # ---- events.jsonl 全量落盘 ----
        time.sleep(0.5)
        jl = os.path.join(store, EVENT_FILE)
        lines = []
        if os.path.isfile(jl):
            fh = io.open(jl, encoding='utf-8')
            try:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
            finally:
                fh.close()
        check('events.jsonl 已追加全量',
              len(lines) >= 2 and json.loads(lines[-1]).get('type') == 'detect',
              '%s lines=%d' % (jl, len(lines)))

        # ---- 最多返回 200 条 ----
        for _k in range(210):
            events.add('manual', 'bulk_%d.jpg' % _k, 1, None, None, None, 14.0,
                       None)
        code, _h, body = get('/api/events?since=0')
        jb = json.loads(body.decode('utf-8'))
        check('/api/events 单次最多 200 条',
              len(jb.get('events') or []) == 200
              and (jb.get('next') or 0) == 200, 'n=%d next=%s'
              % (len(jb.get('events') or []), jb.get('next')))

        # ---- save-dir 不可写: 回落 + 服务不崩 ----
        badpath = os.path.join(tmpdir, 'not_a_dir')
        _f = open(badpath, 'w')
        _f.write('x')
        _f.close()
        fback = os.path.join(tmpdir, 'fallback')
        resolved = ensure_store_dir(badpath, fallback=fback)
        check('ensure_store_dir 不可用则回落',
              resolved == os.path.abspath(fback) and os.path.isdir(fback),
              str(resolved))
        srv2 = None
        try:
            srv2, _th2 = start_http_server(hub, 0, resolved, EventLog(resolved),
                                           runtime=rt_sel, quiet=True,
                                           host='127.0.0.1')
            base2 = 'http://127.0.0.1:%d' % srv2.server_address[1]

            def get2(p):
                try:
                    r2 = urllib.request.urlopen(base2 + p, timeout=5.0)
                    return r2.getcode(), r2.read()
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read()

            c2, b2 = get2('/api/status')
            jj = json.loads(b2.decode('utf-8'))
            check('不可写 save-dir 不崩(status)',
                  c2 == 200 and isinstance(jj.get('sd_free'), int)
                  and jj.get('save_dir') == resolved, str(jj.get('save_dir')))
            c2, b2 = get2('/api/snap')
            js2 = json.loads(b2.decode('utf-8'))
            check('回落目录照样能手动截图',
                  c2 == 200 and js2.get('ok') is True
                  and os.path.isfile(os.path.join(resolved,
                                                  js2.get('file') or '')),
                  str(b2[:80]))
        finally:
            if srv2 is not None:
                try:
                    srv2.shutdown()
                    srv2.server_close()
                except Exception:
                    pass

        # ================= /api/config 运行时开关 (t11) =================
        # ① 默认值: autosave=1(保持原行为), detect=0(自检里没有加载模型)
        _c, _h, body = get('/api/status')
        jc0 = json.loads(body.decode('utf-8'))
        check('/api/config 默认 autosave=1 / detect=0(无模型)',
              jc0.get('autosave') == 1 and jc0.get('detect') == 0,
              'autosave=%s detect=%s' % (jc0.get('autosave'),
                                         jc0.get('detect')))
        check('/api/status 13 个字段齐全(11 + autosave/detect)',
              set(['ok', 'ratio', 'kbps', 'qsel', 'auto', 'target_kbps',
                   'fps', 'blocks', 'nbytes', 'sd_free', 'save_dir',
                   'autosave', 'detect']) <= set(jc0.keys()),
              str(sorted(jc0.keys())))

        # ============ /api/status 15 字段: cover + b_per_blk (t1/t7) ============
        # ① 字段集合**恰好** 15 个(13 + cover/b_per_blk), 多一个少一个都算回归
        check('FULL_FRAME_BLOCKS = 240*135 = 32400(一帧满块数)',
              FULL_FRAME_BLOCKS == 32400, str(FULL_FRAME_BLOCKS))
        _want15 = set(['ok', 'ratio', 'kbps', 'qsel', 'fps', 'blocks', 'nbytes',
                       'auto', 'target_kbps', 'sd_free', 'save_dir',
                       'autosave', 'detect', 'cover', 'b_per_blk'])
        check('/api/status 恰好 15 字段(含 cover/b_per_blk)',
              set(jc0.keys()) == _want15,
              '多=%s 少=%s' % (sorted(set(jc0.keys()) - _want15),
                              sorted(_want15 - set(jc0.keys()))))

        # ② b_per_blk = nbytes/blocks, cover = blocks/32400(单窗口抽样率, 估算, 0~1)
        hub.publish(_synth_frame(),
                    dict(qsel=2, blocks=1440, nbytes=13200, fcnt=1,
                         ratio=0.856), 10.8, 14.0)
        _c, _h, body = get('/api/status')
        jbp = json.loads(body.decode('utf-8'))
        check('b_per_blk == nbytes/blocks',
              isinstance(jbp.get('b_per_blk'), float)
              and abs(jbp['b_per_blk'] - 13200 / 1440.0) < 1e-9,
              'b_per_blk=%r (13200/1440=%.4f)'
              % (jbp.get('b_per_blk'), 13200 / 1440.0))
        check('cover ≈ blocks/32400 且 0~1',
              isinstance(jbp.get('cover'), float)
              and abs(jbp['cover'] - 1440 / float(FULL_FRAME_BLOCKS)) < 1e-6
              and 0.0 <= jbp['cover'] <= 1.0,
              'cover=%r (1440/32400=%.6f)'
              % (jbp.get('cover'), 1440 / float(FULL_FRAME_BLOCKS)))
        print('[http-selftest] 15 字段 -> cover=%.6f  b_per_blk=%.4f'
              % (jbp.get('cover'), jbp.get('b_per_blk') or 0))

        # ②b p19 现场: 条带轮转后 4 帧窗口 ≈ 41.5 条带 ≈ 9960 块 -> cover ≈ 30.7%
        #     (RTL 仿真「4 帧走 41.5/135 条带」); 分母若还是 129600 只会读出 7.7%
        _cov_p19 = frame_cover(9960)
        hub.publish(_synth_frame(),
                    dict(qsel=2, blocks=9960, nbytes=77688, fcnt=1,
                         ratio=(1 - 77688 / (9960 * 64.0))), 311.2, 14.0)
        _c, _h, body = get('/api/status')
        jp19 = json.loads(body.decode('utf-8'))
        check('cover: p19 9960 块 -> ≈0.3074(30.7% 单窗口抽样)',
              abs(_cov_p19 - 0.3074) < 1e-4
              and isinstance(jp19.get('cover'), float)
              and abs(jp19['cover'] - 0.3074) < 1e-4,
              'frame_cover(9960)=%.4f  status.cover=%.4f (分母 %d)'
              % (_cov_p19, jp19.get('cover') or 0, FULL_FRAME_BLOCKS))

        # ②c 旧位流(p18, 无轮转): 4 帧窗口重复编同一批条带 = 12960 块 ->
        #     12960/32400 = 0.40, 这是**重复编码 4 次**的虚高, 是已知口径不是 bug
        _cov_old = frame_cover(12960)
        hub.publish(_synth_frame(),
                    dict(qsel=2, blocks=12960, nbytes=100586, fcnt=1,
                         ratio=(1 - 100586 / (12960 * 64.0))), 311.2, 14.0)
        _c, _h, body = get('/api/status')
        jold = json.loads(body.decode('utf-8'))
        check('cover: 旧位流 12960 块 -> 0.40(重复编码虚高, 已知口径)',
              abs(_cov_old - 0.40) < 1e-9
              and abs(jold.get('cover') - 0.40) < 1e-6,
              'frame_cover(12960)=%.4f  status.cover=%.4f'
              % (_cov_old, jold.get('cover') or 0))
        print('[http-selftest] cover 分母 %d: 9960 -> %.4f, 12960 -> %.4f, 0 -> %.4f'
              % (FULL_FRAME_BLOCKS, _cov_p19, _cov_old, frame_cover(0)))

        # ③ blocks=0(这一窗口没编码) -> b_per_blk 必须是 null, cover=0
        hub.publish(_synth_frame(),
                    dict(qsel=2, blocks=0, nbytes=0, fcnt=1, ratio=None),
                    0.0, 14.0)
        _c, _h, body = get('/api/status')
        jz = json.loads(body.decode('utf-8'))
        check('blocks=0 -> b_per_blk=null + cover=0',
              jz.get('b_per_blk') is None and jz.get('cover') == 0.0
              and jz.get('blocks') == 0,
              'b_per_blk=%r cover=%r blocks=%r'
              % (jz.get('b_per_blk'), jz.get('cover'), jz.get('blocks')))
        hub.publish(_synth_frame(), wm_ok, 10.8, 14.0)     # 恢复, 不影响后面

        # ④ kbps 新算法: 用**构造样本 + 假时钟**(不 sleep, 离线可跑)。
        #    水印 nbytes 是「当前窗口累计值」: [(0,0),(1,100),(2,200),(3,50 回绕),
        #    (4,150),(5,250)] 里 t=3 的 50 是 FPGA 开了新窗口。
        #    老写法把窗内 nbytes 直接相加 -> 650 B; 新写法只认真实增量:
        #    100(t2) + 50(t3 回绕) + 100(t4) + 100(t5) = 350 B
        _sm = WmSmoother(win=3.0)
        _seq = [(0, 0), (1, 100), (2, 200), (3, 50), (4, 150), (5, 250)]
        for _t, _n in _seq:
            _sm.update(dict(qsel=2, blocks=1, nbytes=_n, fcnt=0, ratio=0.9,
                            auto=0, target_kbps=0), float(_t))
        _acc = _sm.window_bytes()
        _old_sum = float(sum(_n for _t, _n in _seq if _t >= 2))   # 老口径
        check('kbps 新算法: 3s 窗累计 =350 B(不是累计值相加)',
              abs(_acc - 350.0) < 1e-9, '累计 %.1f B (老写法 %.0f B)'
              % (_acc, _old_sum))
        check('kbps 老口径(650 B)已被改掉',
              abs(_old_sum - 650.0) < 1e-9 and abs(_old_sum - _acc) > 100.0,
              'old=%.0f B new=%.0f B' % (_old_sum, _acc))
        _kbps_350 = _sm.kbps()                        # = 350 B / (5-2=3 s) / 1024
        check('kbps = 窗内增量/实际跨度/1024',
              abs(_kbps_350 - 350.0 / 3.0 / 1024.0) < 1e-9,
              'kbps=%.6f (期望 %.6f)' % (_kbps_350, 350.0 / 3.0 / 1024.0))

        # ⑤ 真实量级回归: 14fps x 每帧窗口累计涨 22KB(8 帧一个窗口后回绕)
        #    -> 真实增量 22KB x 14/s ≈ 308 KB/s; 老口径会算成 1.3 MB/s 量级
        _sm2 = WmSmoother(win=3.0)
        _step = 22 * 1024
        _hist = []
        for _i in range(42):                          # 14 fps x 3 s
            _t = _i / 14.0
            _n = _step * ((_i % 8) + 1)               # 窗口内累计涨, 第 8 帧后回绕
            _hist.append((_t, _n))
            _sm2.update(dict(qsel=2, blocks=1440, nbytes=_n, fcnt=0,
                             ratio=0.88, auto=0, target_kbps=0), _t)
        _kb_new = _sm2.kbps()
        _span = _hist[-1][0] - _hist[0][0]
        _kb_old = sum(n for _t, n in _hist) / _span / 1024.0
        check('kbps 真实量级: 14fps x 22KB/帧 -> ~308 KB/s(不是 MB/s)',
              295.0 <= _kb_new <= 330.0, 'kbps=%.1f' % _kb_new)
        check('同场景老口径 >=3 倍(906 KB/s 那类读数已被修正)',
              _kb_old > 3.0 * _kb_new,
              'old=%.1f KB/s vs new=%.1f KB/s (%.1fx)'
              % (_kb_old, _kb_new, _kb_old / max(_kb_new, 1e-9)))
        print('[http-selftest] kbps 构造样本: [(0,0),(1,100),(2,200),(3,50 回绕),'
              '(4,150),(5,250)] -> 窗内 %.0f B / %.1f KB/s;'
              ' 14fps x 22KB/帧 -> %.1f KB/s (老口径 %.1f)'
              % (_acc, _kbps_350, _kb_new, _kb_old))
        hub.publish(_synth_frame(), wm_ok, 10.8, 14.0)     # 恢复

        # ② 不传参数 = 只查询不改
        code, _h, body = get('/api/config')
        jq = json.loads(body.decode('utf-8'))
        check('/api/config 不传参: 只查询不改(changed=[])',
              code == 200 and jq.get('ok') is True and jq.get('changed') == []
              and jq.get('autosave') == 1 and jq.get('detect') == 0, str(jq))

        # ③ autosave=0: 只改这一项, status 立刻回读 0(不用重启)
        code, _h, body = get('/api/config?autosave=0')
        ja = json.loads(body.decode('utf-8'))
        _c, _h, b_st = get('/api/status')
        jsa = json.loads(b_st.decode('utf-8'))
        check('/api/config?autosave=0 -> changed=[autosave]',
              code == 200 and ja.get('autosave') == 0
              and ja.get('detect') == 0
              and ja.get('changed') == ['autosave'], str(ja))
        check('status.autosave 立即变 0(无需重启)',
              jsa.get('autosave') == 0, str(jsa.get('autosave')))

        # ④ autosave=0 时"检出也不存图/不发 detect 事件"(直接触发主循环那段逻辑)
        store2 = os.path.join(tmpdir, 'autosave_store')
        tmp_saver = DefectSaver(outdir=store2, min_interval=0)
        n_det_before = len([e for e in events.since(0, 99999)['events']
                            if e['type'] == 'detect'])
        fake_scores = np.array([0.93], dtype=np.float32)
        fake_cls = np.array([0], dtype=np.int32)
        fake_boxes = np.array([[10, 10, 60, 60]], dtype=np.int32)
        r_off = save_defect_if_enabled(tmp_saver, _synth_frame(), fake_boxes,
                                       fake_scores, fake_cls, events,
                                       dict(ratio=0.856), 14.0, rt_sel)
        det_files = [n for n in os.listdir(store2) if n.startswith('det_')]
        n_det_after = len([e for e in events.since(0, 99999)['events']
                           if e['type'] == 'detect'])
        check('autosave=0 时检出也不存图(不写 SD)',
              r_off is None and not det_files,
              'ret=%r files=%s' % (r_off, det_files))
        check('autosave=0 时不产生 detect 事件',
              n_det_after == n_det_before, '%d -> %d'
              % (n_det_before, n_det_after))

        # ⑤ autosave=0 时手动截图 /api/snap 仍然可用 + manual 事件照常记
        code, _h, body = get('/api/snap')
        js_off = json.loads(body.decode('utf-8'))
        check('autosave=0 时 /api/snap 仍然成功',
              code == 200 and js_off.get('ok') is True
              and os.path.isfile(os.path.join(store, js_off.get('file') or '')),
              str(js_off))
        man_after = [e for e in events.since(0, 99999)['events']
                     if e['type'] == 'manual']
        check('autosave=0 时 manual 事件照常记',
              len(man_after) >= 2, 'manual 事件 %d 条' % len(man_after))

        # ⑥ autosave 恢复 1 + detect=1 但没模型 -> ok + warning, detect 保持 0
        code, _h, body = get('/api/config?autosave=1&detect=1')
        jw = json.loads(body.decode('utf-8'))
        check('/api/config detect=1 无模型 -> warning, detect 保持 0',
              code == 200 and jw.get('ok') is True and jw.get('detect') == 0
              and jw.get('warning') == 'no model loaded'
              and jw.get('changed') == ['autosave'], str(jw))

        # ⑦ autosave=1 时正常存图 + detect 事件回来
        r_on = save_defect_if_enabled(tmp_saver, _synth_frame(), fake_boxes,
                                      fake_scores, fake_cls, events,
                                      dict(ratio=0.856), 14.0, rt_sel)
        det_files2 = [n for n in os.listdir(store2) if n.startswith('det_')]
        det_evs2 = [e for e in events.since(0, 99999)['events']
                    if e['type'] == 'detect']
        check('autosave=1 时正常存图 + detect 事件',
              r_on is not None and os.path.isfile(r_on) and len(det_files2) == 1
              and len(det_evs2) == n_det_before + 1,
              'file=%s events=%d' % (os.path.basename(r_on or ''), len(det_evs2)))

        # ⑧ detect 开关: 真的跳过 NPU 推理(用假 rknn 数调用次数)
        class _FakeRknn(object):
            def __init__(self):
                self.calls = 0

            def inference(self, inputs=None):
                self.calls += 1
                g = int(IMG_SIZE) // 8
                return [np.zeros((1, DETECT_NULL_CH, g, g), np.float32),
                        np.zeros((1, DETECT_NULL_CH, g // 2, g // 2), np.float32),
                        np.zeros((1, DETECT_NULL_CH, g // 4, g // 4), np.float32)]

            def release(self):
                pass

        fake_rknn = _FakeRknn()
        gate = DetectGate(fake_rknn, rt_sel)
        rt_sel.set_model_loaded(True)          # 模拟板上模型加载成功 -> detect 默认开
        code, _h, body = get('/api/config?detect=1')
        jd1 = json.loads(body.decode('utf-8'))
        check('有模型后 detect 默认开; 重复设 1 时 changed=[]',
              code == 200 and jd1.get('detect') == 1
              and jd1.get('changed') == [] and rt_sel.get()['detect'] == 1,
              str(jd1))
        outs1 = gate.inference(inputs=[None])
        check('detect=1 时走真推理',
              fake_rknn.calls == 1 and gate.real_calls == 1,
              'fake=%d gate=%d' % (fake_rknn.calls, gate.real_calls))
        _c, _h, body = get('/api/status')
        jsd_on = json.loads(body.decode('utf-8'))

        code, _h, body = get('/api/config?detect=0')
        jd0 = json.loads(body.decode('utf-8'))
        _c, _h, body = get('/api/status')
        jsd = json.loads(body.decode('utf-8'))
        outs0 = gate.inference(inputs=[None])
        check('/api/config?detect=0 -> status.detect=0, changed=[detect]',
              code == 200 and jd0.get('detect') == 0
              and jd0.get('changed') == ['detect']
              and jsd_on.get('detect') == 1 and jsd.get('detect') == 0,
              '%s / status %s->%s' % (jd0, jsd_on.get('detect'),
                                      jsd.get('detect')))
        check('detect=0 时不再调用真推理(只 skipped+1)',
              fake_rknn.calls == 1 and gate.skipped_calls == 1,
              'fake=%d skipped=%d' % (fake_rknn.calls, gate.skipped_calls))
        b0, s0, c0 = postprocess_yolov5s(outs0, 1.0, 0, 0, 480, 270)
        check('detect=0 的"全 0 输出"过完 postprocess = 0 个目标',
              len(s0) == 0 and len(b0) == 0, 'n=%d' % len(s0))
        check('detect=0 时不会产生 detect 事件(0 目标不进存图)',
              save_defect_if_enabled(tmp_saver, _synth_frame(), b0, s0, c0,
                                     events, dict(ratio=0.856), 14.0,
                                     rt_sel) is None)
        check('detect=0 不写新的缺陷图',
              len([n for n in os.listdir(store2)
                   if n.startswith('det_')]) == 1,
              str(os.listdir(store2)))
        code, _h, body = get('/api/config?detect=1')
        jd2 = json.loads(body.decode('utf-8'))
        check('/api/config?detect=1 (0->1) -> changed=[detect]',
              code == 200 and jd2.get('detect') == 1
              and jd2.get('changed') == ['detect'], str(jd2))
        rt_sel.apply({'detect': 0})        # 还原, 让下面"非法值不改状态"稳定

        # ⑨ 非法值 -> 400 + {"ok":false,"error":"bad xxx=vv"}
        code, _h, body = get('/api/config?autosave=2')
        jbad = json.loads(body.decode('utf-8'))
        check('非法 autosave=2 -> 400 + bad autosave=2',
              code == 400 and jbad.get('ok') is False
              and jbad.get('error') == 'bad autosave=2',
              'code=%s %s' % (code, jbad))
        code, _h, body = get('/api/config?detect=x')
        jbad2 = json.loads(body.decode('utf-8'))
        check('非法 detect=x -> 400 + bad detect=x',
              code == 400 and jbad2.get('error') == 'bad detect=x',
              'code=%s %s' % (code, jbad2))
        check('非法值不改当前状态(仍 autosave=1/detect=0)',
              rt_sel.get() == dict(autosave=1, detect=0),
              str(rt_sel.get()))

        # ================= 尺寸测量模式 dim (t18) =================
        # ① dim=1 -> detect 自动 0(互斥); ② detect=1 -> dim 自动 0
        code, _h, body = get('/api/config?dim=1')
        jd1 = json.loads(body.decode('utf-8'))
        check('/api/config?dim=1 -> dim=1 且 detect 自动变 0(互斥)',
              code == 200 and jd1.get('ok') is True and jd1.get('dim') == 1
              and jd1.get('detect') == 0 and 'dim' in (jd1.get('changed') or []),
              json.dumps(jd1))
        code, _h, body = get('/api/config')
        jd1r = json.loads(body.decode('utf-8'))
        check('/api/config 回读 dim=1 / detect=0',
              jd1r.get('dim') == 1 and jd1r.get('detect') == 0, json.dumps(jd1r))
        code, _h, body = get('/api/config?detect=1')
        jd2 = json.loads(body.decode('utf-8'))
        check('/api/config?detect=1 -> dim 自动变 0(互斥反向)',
              code == 200 and jd2.get('detect') == 1 and jd2.get('dim') == 0
              and 'dim' in (jd2.get('changed') or []), json.dumps(jd2))
        # ③ 非法值 400 + ④ 自动测量间隔钳位(板卡扛不住 <2 s)
        code, _h, body = get('/api/config?dim=2')
        jdb = json.loads(body.decode('utf-8'))
        check('非法 dim=2 -> 400 bad dim=2',
              code == 400 and jdb.get('ok') is False
              and jdb.get('error') == 'bad dim=2', 'code=%s %s' % (code, jdb))
        code, _h, body = get('/api/config?dim=1&dim_auto_sec=1')
        jd3 = json.loads(body.decode('utf-8'))
        check('dim_auto_sec=1 被钳到下限 2 s',
              code == 200 and jd3.get('dim_auto_sec') == DIM_AUTO_SEC_MIN
              and jd3.get('dim') == 1, json.dumps(jd3))
        code, _h, body = get('/api/config?dim_auto=1')
        jd4 = json.loads(body.decode('utf-8'))
        check('dim_auto=1 -> 默认间隔 5 s',
              jd4.get('dim_auto_sec') == DIM_AUTO_SEC_DEFAULT, json.dumps(jd4))
        # ④b t24: 0 是合法值 = 关闭自动测量, 绝不能被钳到最小值; 其余钳位规则不变
        code, _h, body = get('/api/config?dim_auto=0')
        jd5 = json.loads(body.decode('utf-8'))
        check('dim_auto=0 -> 关(回读 dim_auto_sec=0, 不被钳成 2)',
              code == 200 and jd5.get('dim_auto_sec') == 0
              and jd5.get('dim') == 1, json.dumps(jd5))
        code, _h, body = get('/api/config?dim_auto_sec=0')
        jd6 = json.loads(body.decode('utf-8'))
        check('dim_auto_sec=0 -> 0(关闭; 0 不参与钳位)',
              jd6.get('dim_auto_sec') == 0, json.dumps(jd6))
        code, _h, body = get('/api/config?dim_auto_sec=9999')
        jd7 = json.loads(body.decode('utf-8'))
        check('dim_auto_sec=9999 -> 钳到上限 600',
              jd7.get('dim_auto_sec') == DIM_AUTO_SEC_MAX, json.dumps(jd7))
        code, _h, body = get('/api/config?dim_auto_sec=2')
        jd8 = json.loads(body.decode('utf-8'))
        check('dim_auto_sec=2 -> 2(下限合法, 不钳)',
              jd8.get('dim_auto_sec') == DIM_AUTO_SEC_MIN, json.dumps(jd8))
        # ⑤ /api/status 仍然恰好 15 字段, 测量信息一个都不许漏进去
        _c, _h, body = get('/api/status')
        jst = json.loads(body.decode('utf-8'))
        check('/api/status 仍恰好 15 字段(不含 dim/dim_auto/accuracy/targets)',
              len(jst) == 15
              and not (set(['dim', 'dim_auto_sec', 'accuracy_mm', 'state',
                            'targets', 'calib_ok']) & set(jst)),
              '%d %s' % (len(jst), sorted(jst.keys())))
        # ⑥ /api/dim: 只读不触发(不阻塞), 结构按冻结 schema
        t0 = time.time()
        code, _h, body = get('/api/dim')
        _dim_ms = (time.time() - t0) * 1000.0
        jdim = json.loads(body.decode('utf-8'))
        check('/api/dim 200 + 顶层字段齐(dim-v1 schema)',
              code == 200 and jdim.get('schema') == 'dim-v1'
              and set(['calib_ok', 'calib_note', 'sigma_mm', 'perf_ms',
                       'targets']) <= set(jdim)
              and isinstance(jdim.get('targets'), list)
              and jdim.get('state') in ('idle', 'calib', 'ok', 'bad')
              and isinstance(jdim.get('calib_note'), str),
              '%s state=%r' % (sorted(jdim.keys()), jdim.get('state')))
        check('/api/dim 不带 run 只读缓存(不测量、不阻塞: <300 ms)',
              _dim_ms < 300.0 and 'busy' in jdim and 'age_s' in jdim,
              '%.0f ms busy=%r age=%r' % (_dim_ms, jdim.get('busy'),
                                          jdim.get('age_s')))
        # ⑥b t24: 顶层**恰好**契约那 17 个键(不许再漏 measured_t 之类的内部字段)
        check('/api/dim 顶层键集合恰好 = 契约 17 键(t24)',
              set(jdim) == set(DIM_API_KEYS) and len(jdim) == 17,
              '多=%s 少=%s (共 %d)'
              % (sorted(set(jdim) - set(DIM_API_KEYS)),
                 sorted(set(DIM_API_KEYS) - set(jdim)), len(jdim)))
        check('/api/dim 不含 measured_t(时间信息只用 age_s)',
              'measured_t' not in jdim,
              'keys=%s' % sorted(jdim))
        # ⑦ 注入假模块(不跑真算法/不需要 scipy): 验 schema 组装 + tol/sigma 语义
        class _DimStub(object):
            """最小假模块: 只实现 DimEngine 依赖的那几个入口"""
            SCHEMA_VERSION = 'dim-v1'
            TARGET_ORDER = ('board_outline', 'hole_pitch')
            calls = []

            def calibrate(self, frame, cfg=None, dbg=False):
                _DimStub.calls.append('calibrate')
                return dict(schema='dim-v1', calib_ok=True, calib_note='stub 可信',
                            scale_mm_per_px=0.026,
                            H_mm2px=[1.0, 0.0, 100.0, 0.0, 1.0, 200.0, 0.0, 0.0, 1.0],
                            ppm=10.0, margin_mm=12.0, canonical_px=[1090, 605],
                            flip=[False, False], sigma_mm=1.2,
                            perf_ms={'calibrate': 1.0},
                            board_len_mm=90.6, board_wid_mm=43.0,
                            outline_corners=[[-2.75, -3.2], [87.75, -3.2],
                                             [87.75, 39.7], [-2.75, 39.7]],
                            outline_sides=[90.5, 42.9, 90.5, 42.9])

            def measure(self, frame, calib, cfg=None, dbg=False):
                _DimStub.calls.append('measure')
                return dict(schema='dim-v1', calib_ok=True, calib_note='stub',
                            sigma_mm=(calib or {}).get('sigma_mm'),
                            perf_ms={'measure': 2.0},
                            targets=[
                                dict(name='board_outline', value_mm=90.52,
                                     nominal_mm=90.5, err_mm=0.02, tol_mm=1.0,
                                     ok=True,
                                     corners=[[-2.75, -3.2], [87.75, -3.2],
                                              [87.75, 39.7], [-2.75, 39.7]],
                                     sides=[90.5, 42.9, 90.5, 42.9],
                                     diag=[100.1, 100.1], area_mm2=3882.4,
                                     kind='quad', short_mm=42.9),
                                dict(name='hole_pitch', value_mm=85.02,
                                     nominal_mm=85.0, err_mm=0.02, tol_mm=1.0,
                                     ok=True,
                                     corners=[[0, 0], [85, 0], [85, 36.5], [0, 36.5]],
                                     sides=[85.0, 36.5, 85.0, 36.5],
                                     diag=[92.5, 92.5], area_mm2=3102.5),
                            ])

            def validate_schema(self, obj):
                return []

        _stub = _DimStub()
        _eng = DimEngine(module=_stub)
        _cal = _eng.calibrate_now(np.zeros((64, 64, 3), np.uint8))
        check('DimEngine 标定: state=ok + H_mm2px/sigma 透传',
              _eng.state == 'ok' and _eng.calibrated()
              and (_cal or {}).get('H_mm2px') is not None
              and (_cal or {}).get('sigma_mm') == 1.2, str(_eng.state))
        _r1 = _eng.measure_now(np.zeros((64, 64, 3), np.uint8), fps=14.0, sync=True)
        _tg = {t.get('name'): t for t in (_r1.get('targets') or [])}
        _tkeys = set(['name', 'value_mm', 'nominal_mm', 'err_mm', 'tol_mm', 'ok',
                      'corners', 'sides', 'diag', 'area_mm2'])
        check('测量结果: 只有 2 个目标(board_outline/hole_pitch)且字段齐 + tol_mm 都有',
              set(_tg) == set(['board_outline', 'hole_pitch'])
              and all(_tkeys <= set(t) for t in _tg.values())
              and all(t.get('tol_mm') is not None for t in _tg.values()),
              json.dumps(sorted(_tg)))
        # t25: HUD 画"定位框/四角/孔位"要靠 hud() 里的 H_mm2px + hole_points_px；
        # 之前调用处没传 calib, H 恒 None -> 画面上一个框都不出。这里锁住这两个字段。
        _hud = _eng.hud()
        check('hud() 带 H_mm2px/hole_points_px 两个绘制字段(定位框/四角/孔位靠它们画)',
              'H_mm2px' in _hud and 'hole_points_px' in _hud
              and _hud.get('H_mm2px') is not None,
              'H=%r holes=%r' % (str(_hud.get('H_mm2px'))[:24],
                                 _hud.get('hole_points_px')))
        check('这两个绘制字段**不渗到** /api/dim(顶层仍恰好 17 键)',
              set(_eng.snapshot()) <= set(DIM_API_KEYS),
              '多=%s' % sorted(set(_eng.snapshot()) - set(DIM_API_KEYS)))
        check('精度与判定线分开: sigma_mm/accuracy_mm=1.2 而 tol_mm 是逐目标判定线',
              _r1.get('sigma_mm') == 1.2
              and _eng.snapshot().get('accuracy_mm') == 1.2
              and _eng.snapshot().get('tol_kind') == 'judge_line'
              and _tg['hole_pitch'].get('tol_mm') == 1.0,
              'sigma=%r tol[hp]=%r' % (_r1.get('sigma_mm'),
                                       _tg['hole_pitch'].get('tol_mm')))
        check('值/角点/边长/对角/面积按模块给的透传(不自己编数)',
              _tg['hole_pitch'].get('value_mm') == 85.02
              and _tg['hole_pitch'].get('corners')
              == [[0, 0], [85, 0], [85, 36.5], [0, 36.5]]
              and _tg['hole_pitch'].get('sides') == [85.0, 36.5, 85.0, 36.5]
              and _tg['hole_pitch'].get('area_mm2') == 3102.5,
              json.dumps(_tg['hole_pitch'], ensure_ascii=False)[:120])
        # ⑦b t24: state=ok(量过一帧)时, 组装出来的响应键集合必须**恰好**是契约 17 键
        #     (verifier 就是在这个状态下数出 18 键的, 原因是 measured_t 漏出去了)
        _ok_keys = set(_eng.snapshot()) | set(['ok', 'recalib_queued', 'busy',
                                               'age_s'])
        check('/api/dim state=ok 时键集合恰好 17 键(引擎侧不渗内部字段)',
              _ok_keys == set(DIM_API_KEYS) and 'measured_t' not in _r1,
              '多=%s 少=%s' % (sorted(_ok_keys - set(DIM_API_KEYS)),
                              sorted(set(DIM_API_KEYS) - _ok_keys)))
        # ⑧ 触发式语义: tick 默认不测量; 假时钟过 10 s + auto_sec=5 才自动量一次
        _nm = len([c for c in _DimStub.calls if c == 'measure'])
        _eng.tick(np.zeros((64, 64, 3), np.uint8), fps=14.0, auto_sec=0)
        _nm2 = len([c for c in _DimStub.calls if c == 'measure'])
        check('触发式: tick(auto_sec=0) 不跑 measure(否则帧率被打到 2~3 fps)',
              _nm2 == _nm, '%d -> %d' % (_nm, _nm2))
        _eng.last_meas_t -= 10.0            # 假时钟: 假装已经过了 10 s(不 sleep)
        _eng.tick(np.zeros((64, 64, 3), np.uint8), fps=14.0,
                  auto_sec=DIM_AUTO_SEC_DEFAULT)
        _nm3 = len([c for c in _DimStub.calls if c == 'measure'])
        _ag = _eng.age()
        check('触发式: 假时钟过 10 s + auto_sec=5 -> 自动量一次(age 归零)',
              _nm3 == _nm + 1 and _ag is not None and _ag < 1.0,
              '%d -> %d, age=%s' % (_nm, _nm3,
                                    ('%.2f' % _ag) if _ag is not None else 'None'))
        # ⑨ calib_ok=false 分支: 一个 value_mm 都不许给(即使模块内部返回了数)
        class _DimBadStub(_DimStub):
            def calibrate(self, frame, cfg=None, dbg=False):
                return dict(schema='dim-v1', calib_ok=False,
                            calib_note='标定不可信：孔距长宽比偏差 6.20%',
                            scale_mm_per_px=None, H_mm2px=None, sigma_mm=None,
                            perf_ms={'calibrate': 1.0})

        _bad = DimEngine(module=_DimBadStub())
        _bad.calibrate_now(np.zeros((32, 32, 3), np.uint8))
        _bsnap = _bad.snapshot()
        _brun = _bad.measure_now(np.zeros((32, 32, 3), np.uint8), sync=True)
        check('calib_ok=false -> value_mm 全 null + 只给 calib_note(两处快照都如此)',
              _bsnap.get('calib_ok') is False
              and all(t.get('value_mm') is None for t in _bsnap['targets'])
              and all(t.get('value_mm') is None for t in (_brun.get('targets') or []))
              and len(str(_bsnap.get('calib_note') or '')) > 3,
              '%s / %s' % (_bad.state, _bsnap.get('calib_note'))[:100])
        check('calib_ok=false 时目标仍带 tol_mm(判定线不因失败消失) 且 state=bad',
              all(t.get('tol_mm') is not None for t in _bsnap['targets'])
              and _bad.state == 'bad', _bad.state)
        # ⑩ 真 dim_measure.py 的契约校验(板子上可能没有这个文件 -> SKIP, 不计数)
        _real_mod, _real_err = load_dim_module()
        if _real_mod is not None and hasattr(_real_mod, 'validate_schema'):
            _verrs = _real_mod.validate_schema(DimEngine(module=_stub).snapshot())
            if not _verrs:
                _eng2 = DimEngine(module=_stub)
                _eng2.calibrate_now(np.zeros((64, 64, 3), np.uint8))
                _eng2.measure_now(np.zeros((64, 64, 3), np.uint8), sync=True)
                _verrs = _real_mod.validate_schema(_eng2.snapshot())
            check('真 dim_measure.validate_schema 认可 /api/dim 结构(dim-v1)',
                  _verrs == [], str(_verrs[:3]))
            check('dim_measure 冻结入口齐(calibrate/measure/SCHEMA_VERSION/validate_schema)',
                  all(hasattr(_real_mod, k) for k in
                      ('calibrate', 'measure', 'validate_schema', 'SCHEMA_VERSION')),
                  getattr(_real_mod, 'SCHEMA_VERSION', '?'))
        else:
            print('[http-selftest] %-32s SKIP  %s'
                  % ('dim_measure 契约校验', '本机没这个文件(板子上要先拷过去)'))
        # ⑪ ?run=1 触发一次: 未标定时它只起后台标定并回结构(不干等)
        code, _h, body = get('/api/dim?run=1')
        jrun = json.loads(body.decode('utf-8'))
        check('/api/dim?run=1 返回结构 + recalib_queued(未标定则先后台标定)',
              code == 200 and jrun.get('ok') is True
              and 'recalib_queued' in jrun
              and (jrun.get('calib_ok') is True or
                   all(t.get('value_mm') is None
                       for t in (jrun.get('targets') or []))),
              'calib_ok=%r state=%r queued=%r'
              % (jrun.get('calib_ok'), jrun.get('state'),
                 jrun.get('recalib_queued')))
        _c, _h, body = get('/api/config?dim=0')      # 收尾: 关掉测量模式
        _c, _h, body = get('/api/config?detect=0')

        # ================= 存图目录来源 / SD 卡自动找 (t14) =================
        # ① 挂载表里没有任何真实块设备 -> find_sd_mount() 返回 None(且不抛异常)
        fake_mounts = os.path.join(tmpdir, 'fake_mounts_1')
        fh = io.open(fake_mounts, 'w', encoding='utf-8')
        try:
            fh.write('/dev/root / ext4 rw,relatime 0 0\n')
            fh.write('devtmpfs /dev devtmpfs rw,nosuid 0 0\n')
            fh.write('proc /proc proc rw 0 0\n')
            fh.write('overlay /var/lib/docker overlay rw 0 0\n')
        finally:
            fh.close()
        got_sd, exc_sd = 'EXC', None
        try:
            got_sd = find_sd_mount(fake_mounts)
        except Exception as exc:
            exc_sd = exc
        check('find_sd_mount 无候选 -> None 且不抛异常',
              got_sd is None and exc_sd is None,
              'ret=%r err=%r' % (got_sd, exc_sd))

        # ② 只有"和 / 同一个设备"的 /dev/mmcblk 挂载 -> 仍然 None(st_dev 排除)
        fake_mounts2 = os.path.join(tmpdir, 'fake_mounts_2')
        fh = io.open(fake_mounts2, 'w', encoding='utf-8')
        try:
            fh.write('/dev/mmcblk0p6 / ext4 rw,relatime 0 0\n')
            fh.write('/dev/mmcblk1p1 %s vfat rw,relatime,fmask=0022,dmask=0022 0 0\n'
                     % store)
        finally:
            fh.close()
        _orig_dev2 = _st_dev
        try:
            globals()['_st_dev'] = lambda p: 42      # 所有路径都"和 / 同设备"
            same_dev_pick = find_sd_mount(fake_mounts2)
        finally:
            globals()['_st_dev'] = _orig_dev2
        check('和 / 同设备的 mmcblk 挂载被排除(不误判成 SD)',
              same_dev_pick is None, repr(same_dev_pick))

        # ③ describe_store_dir: 绝对路径 / on_rootfs 判定 / 字段齐全 / writable
        d_info = describe_store_dir(store)
        _st_here, _st_root = _st_dev(store), _st_dev('/')
        expect_rootfs = (not _read_mounts()) or (
            _st_here is not None and _st_root is not None and _st_here == _st_root)
        check('describe_store_dir: dir 绝对路径 + on_rootfs 判定正确',
              os.path.isabs(d_info['dir'])
              and d_info['dir'] == os.path.abspath(store)
              and d_info['on_rootfs'] is expect_rootfs,
              '%s on_rootfs=%s(期望 %s)'
              % (d_info['dir'], d_info['on_rootfs'], expect_rootfs))
        check('describe_store_dir 字段齐全(含 writable/mount_opts)',
              set(['dir', 'dev', 'mount', 'fstype', 'free', 'on_rootfs',
                   'removable', 'writable', 'mount_opts']) <= set(d_info.keys()),
              str(sorted(d_info.keys())))
        check('describe_store_dir: 可写目录 writable=True',
              d_info['writable'] is True, str(d_info['writable']))

        # ④ "看起来像挂载点但实际在 rootfs" 要能判出来(告警判定用)
        fake_mnt_dir = os.path.join(tmpdir, 'media', 'linaro', '0403-0201',
                                    'defect')
        os.makedirs(fake_mnt_dir, exist_ok=True)
        _orig_dev4 = _st_dev
        try:
            globals()['_st_dev'] = lambda p: 7       # 假装"和 rootfs 同设备"
            d_fake = describe_store_dir(fake_mnt_dir)
        finally:
            globals()['_st_dev'] = _orig_dev4
        check('看起来像挂载点但落在 rootfs -> 能判出来',
              _looks_like_mount(fake_mnt_dir) is True
              and d_fake['on_rootfs'] is True, str(d_fake))

        # ⑤ 候选不可写 -> 被跳过, 且原因被记进 notes['rejected']
        _orig_wt = _write_test
        _orig_dev2 = _st_dev
        _orig_free = free_bytes
        _notes = {}
        got_rej = 'EXC'
        try:
            globals()['_write_test'] = \
                lambda d, tag='.pcb_wtest': (False, 'EACCES(模拟:vfat 无 uid=)')
            globals()['_st_dev'] = lambda p: 1 if str(p) == '/' else 2
            globals()['free_bytes'] = lambda p: 1024 * 1024 * 1024
            got_rej = find_sd_mount(fake_mounts2, notes=_notes)
        finally:
            globals()['_write_test'] = _orig_wt
            globals()['_st_dev'] = _orig_dev2
            globals()['free_bytes'] = _orig_free
        rej = _notes.get('rejected') or []
        check('不可写候选被跳过且记下原因',
              got_rej is None and len(rej) >= 1
              and rej[0][1].startswith('/dev/mmcblk')
              and 'EACCES' in str(rej[0][3]), str(rej))

        # ⑥ 真·不可写目录的写测试(Windows 上 chmod 不影响建目录 -> SKIP)
        ro_dir = os.path.join(tmpdir, 'ro_probe')
        os.makedirs(ro_dir, exist_ok=True)
        try:
            os.chmod(ro_dir, 0o400)
        except OSError:
            pass
        ok_ro, why_ro = _write_test(ro_dir)
        try:
            os.chmod(ro_dir, 0o700)
        except OSError:
            pass
        if ok_ro:
            print('[http-selftest] %-32s SKIP  %s'
                  % ('不可写目录的写测试',
                     '本平台 chmod 不影响建子目录(Windows), 跳过'))
        else:
            check('不可写目录的写测试: 失败+原因',
                  ('EACCES' in why_ro or 'EROFS' in why_ro or 'ERR' in why_ro),
                  why_ro)

        # ⑦ 目录不存在时写测试也要给出原因(不抛异常)
        ok_ne, why_ne = _write_test(os.path.join(tmpdir, 'no_such_dir_xyz'))
        check('_write_test 目录不存在 -> False + 原因',
              ok_ne is False and 'ENOENT' in str(why_ne), str(why_ne))

        # ⑧ --save-dir auto 找不到候选时: 回落目录仍可写可用(且大小写都认)
        auto_fb = os.path.join(tmpdir, 'auto_fallback')
        auto_fb2 = os.path.join(tmpdir, 'auto_fallback_upper')
        _orig_find = find_sd_mount
        try:
            globals()['find_sd_mount'] = lambda *a, **k: None
            auto_dir = resolve_store_dir('auto', fallback=auto_fb, log=False)
            auto_dir2 = resolve_store_dir('AUTO', fallback=auto_fb2, log=False)
        finally:
            globals()['find_sd_mount'] = _orig_find
        check('--save-dir auto 无候选 -> 回落目录可写可用',
              isinstance(auto_dir, str) and os.path.isabs(auto_dir)
              and os.path.isdir(auto_dir) and _write_test(auto_dir)[0] is True,
              str(auto_dir))
        check('--save-dir AUTO(大写) 同样识别',
              auto_dir2 == os.path.abspath(auto_fb2), str(auto_dir2))

        # ================= 设备类型判定 _device_kind (t16) =================
        # ① 本机没有 sysfs -> 走编号约定回退
        check('_device_kind: mmcblk1p1 -> SD(编号回退)',
              _device_kind('/dev/mmcblk1p1') == 'SD',
              repr(_device_kind('/dev/mmcblk1p1')))
        check('_device_kind: mmcblk0p6 -> eMMC',
              _device_kind('/dev/mmcblk0p6') == 'eMMC',
              repr(_device_kind('/dev/mmcblk0p6')))
        check('_device_kind: sda1 -> USB / 空 -> 其它',
              _device_kind('/dev/sda1') == 'USB'
              and _device_kind('') == '其它',
              '%r / %r' % (_device_kind('/dev/sda1'), _device_kind('')))

        # ② 假 sysfs: device/type 优先于编号与 removable(真机 Rockchip 内核的坑)
        fake_sys = os.path.join(tmpdir, 'fake_sys_block')
        for idx, typ, rem in (('0', 'SD', '0'), ('1', 'MMC', '1'),
                              ('2', 'SDIO', '1')):
            dd = os.path.join(fake_sys, 'mmcblk%s' % idx, 'device')
            os.makedirs(dd, exist_ok=True)
            fh = io.open(os.path.join(dd, 'type'), 'w', encoding='utf-8')
            try:
                fh.write(typ + '\n')
            finally:
                fh.close()
            fh = io.open(os.path.join(fake_sys, 'mmcblk%s' % idx, 'removable'),
                         'w', encoding='utf-8')
            try:
                fh.write(rem + '\n')
            finally:
                fh.close()
        _orig_sys = SYS_BLOCK
        try:
            globals()['SYS_BLOCK'] = fake_sys
            k0 = _device_kind('/dev/mmcblk0p6')    # type=SD, 编号 0 -> 必须听 sysfs
            k1 = _device_kind('/dev/mmcblk1p1')    # type=MMC, 编号 1 -> 必须听 sysfs
            k2 = _device_kind('/dev/mmcblk2p1')    # type=SDIO -> 回编号(2 -> SD)
            k_missing = _device_kind('/dev/mmcblk9p1')   # sysfs 里没有 -> 回编号
        finally:
            globals()['SYS_BLOCK'] = _orig_sys
        check('假 sysfs: device/type 优先于编号与 removable',
              k0 == 'SD' and k1 == 'eMMC' and k2 == 'SD',
              'mmcblk0:%r mmcblk1:%r mmcblk2:%r' % (k0, k1, k2))
        check('假 sysfs 缺失该设备 -> 安全回退编号',
              k_missing == 'SD', repr(k_missing))

        # ③ 防回归: 板子上真实布局(mmcbk0=eMMC/含 /userdata, mmcblk1=SD)
        #    要求 eMMC 上的 /userdata 不能算成 removable / on_sd, SD 卡要算上
        fake_board = os.path.join(tmpdir, 'fake_sys_board')
        for idx, typ in (('0', 'MMC'), ('1', 'SD')):
            dd = os.path.join(fake_board, 'mmcblk%s' % idx, 'device')
            os.makedirs(dd, exist_ok=True)
            fh = io.open(os.path.join(dd, 'type'), 'w', encoding='utf-8')
            try:
                fh.write(typ + '\n')
            finally:
                fh.close()
        userdata = os.path.join(tmpdir, 'userdata')       # 假 /userdata(独立分区)
        sdmnt = os.path.join(tmpdir, 'media', 'linaro', '0403-02011')
        os.makedirs(userdata, exist_ok=True)
        os.makedirs(sdmnt, exist_ok=True)
        fake_mounts3 = os.path.join(tmpdir, 'fake_mounts_3')
        fh = io.open(fake_mounts3, 'w', encoding='utf-8')
        try:
            fh.write('/dev/mmcblk0p6 / ext4 rw,relatime 0 0\n')
            fh.write('/dev/mmcblk0p8 %s ext4 rw,relatime 0 0\n' % userdata)
            fh.write('/dev/mmcblk1p1 %s vfat rw,relatime,fmask=0022,dmask=0022 0 0\n'
                     % sdmnt)
        finally:
            fh.close()
        _orig_sys2, _orig_dev3 = SYS_BLOCK, _st_dev
        try:
            globals()['SYS_BLOCK'] = fake_board
            # 每个挂载点都是独立分区(和设备名对得上), 只有 / 和 rootfs 同设备
            globals()['_st_dev'] = (
                lambda p: 1 if os.path.abspath(p) == os.path.abspath('/')
                else (2 if str(p).startswith(userdata)
                      else (3 if str(p).startswith(sdmnt) else 1)))
            d_user = describe_store_dir(os.path.join(userdata, 'defect'),
                                        fake_mounts3)
            d_sd = describe_store_dir(os.path.join(sdmnt, 'defect'),
                                      fake_mounts3)
            k_user = _device_kind(d_user['dev'])
            k_sd = _device_kind(d_sd['dev'])
        finally:
            globals()['SYS_BLOCK'] = _orig_sys2
            globals()['_st_dev'] = _orig_dev3
        on_sd_user = (k_user in ('SD', 'USB')) and not d_user['on_rootfs']
        on_sd_sd = (k_sd in ('SD', 'USB')) and not d_sd['on_rootfs']
        check('eMMC 上的 /userdata: removable=False 且不算 on_sd',
              d_user['removable'] is False and on_sd_user is False,
              'kind=%r removable=%r on_sd=%r'
              % (k_user, d_user['removable'], on_sd_user))
        check('SD 卡挂载点: removable=True 且算 on_sd',
              d_sd['removable'] is True and on_sd_sd is True,
              'kind=%r removable=%r on_sd=%r'
              % (k_sd, d_sd['removable'], on_sd_sd))

        # ④ "未知"：sysfs 与编号都给不出结论时 -> '未知'，且**不**打"不在 SD 卡上"
        _orig_sys3, _orig_dev4 = SYS_BLOCK, _st_dev
        weird = os.path.join(tmpdir, 'weird_mnt')     # 设备名 mmcblkXp1(无编号)
        os.makedirs(weird, exist_ok=True)
        unknown_out = ''
        emmc_out = ''
        try:
            globals()['SYS_BLOCK'] = fake_board      # 里面没有 mmcblkX
            globals()['_st_dev'] = (
                lambda p: 1 if os.path.abspath(p) == os.path.abspath('/')
                else (2 if str(p).startswith(userdata)
                      else (9 if str(p).startswith(sdmnt)
                            else (7 if str(p).startswith(weird) else 1))))
            fake_mounts4 = os.path.join(tmpdir, 'fake_mounts_4')
            fh = io.open(fake_mounts4, 'w', encoding='utf-8')
            try:
                fh.write('/dev/mmcblk0p6 / ext4 rw,relatime 0 0\n')
                fh.write('/dev/mmcblkXp1 %s vfat rw,relatime 0 0\n' % weird)
            finally:
                fh.close()
            k_unknown = _device_kind('/dev/mmcblkXp1')
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                log_store_dir(os.path.join(weird, 'defect'), fake_mounts4)
            unknown_out = buf.getvalue()
            # 对照: eMMC 上的 /userdata 仍必须打"不在 SD 卡上"
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                log_store_dir(os.path.join(userdata, 'defect'), fake_mounts3)
            emmc_out = buf2.getvalue()
        finally:
            globals()['SYS_BLOCK'] = _orig_sys3
            globals()['_st_dev'] = _orig_dev4
        check('未知设备: _device_kind 返回"未知"且不打"不在 SD 卡上"',
              k_unknown == '未知'
              and '(vfat, 未知)' in unknown_out
              and '设备类型未知' in unknown_out
              and '不在 SD 卡上' not in unknown_out,
              'kind=%r out=%r' % (k_unknown, unknown_out.strip()[:120]))
        check('eMMC(/userdata) 仍明确打"不在 SD 卡上"',
              '不在 SD 卡上' in emmc_out,
              emmc_out.strip()[:120])

        # ================= 自定义类别 nc/通道自适应 (t21) =================
        _coco_backup = list(CLASS_NAMES)
        try:
            # ① 读临时 classes.txt（3 行 + 空行 + # 注释）
            cls_txt = os.path.join(tmpdir, 'classes_3.txt')
            fh = io.open(cls_txt, 'w', encoding='utf-8')
            try:
                fh.write('# PCB 缺陷类别\n\nscratch\nmissing\nstain\n\n')
            finally:
                fh.close()
            got_names, got_err = load_classes(cls_txt)
            _n3 = set_class_names(got_names)
            check('classes.txt 跳过 #/空行 -> 3 类且 nc=3',
                  got_err is None and got_names == ['scratch', 'missing', 'stain']
                  and _n3 == 3 and nc() == 3
                  and CLASS_NAMES == ['scratch', 'missing', 'stain'],
                  'names=%s nc=%d' % (got_names, nc()))

            # ①b 带 BOM 的 classes.txt（记事本存的）也要剥掉，否则第一个类名带 \ufeff
            cls_bom = os.path.join(tmpdir, 'classes_bom.txt')
            fb = io.open(cls_bom, 'w', encoding='utf-8-sig')
            try:
                fb.write('scratch\nmissing\nstain\n')
            finally:
                fb.close()
            names_bom, _err_bom = load_classes(cls_bom)
            check('带 BOM 的 classes.txt 也能读对(剥 BOM)',
                  names_bom == ['scratch', 'missing', 'stain'], str(names_bom))

            # ② 没传 --classes 且模型同目录没有 classes.txt -> 回退内置 COCO + WARN
            set_class_names(_coco_backup)
            empty_md = os.path.join(tmpdir, 'modeldir_empty')
            os.makedirs(empty_md, exist_ok=True)
            _nm, _src, _warn = resolve_class_names(
                os.path.join(empty_md, 'yolov5s_relu.rknn'))
            check('没有 classes.txt -> 回退 COCO 80 类 + [WARN]',
                  _nm is None and nc() == 80 and _warn is not None
                  and '[WARN]' in _warn and 'COCO' in _warn,
                  str(_warn)[:90])

            # ③ 3 类模型的后处理：24 通道输出，全 0 -> 0 框，不抛异常
            set_class_names(['scratch', 'missing', 'stain'])
            ch24 = 3 * (5 + 3)
            outs3 = [np.zeros((1, ch24, 20, 20), np.float32),
                     np.zeros((1, ch24, 10, 10), np.float32),
                     np.zeros((1, ch24, 5, 5), np.float32)]
            exc3 = None
            b3 = s3 = c3 = None
            try:
                b3, s3, c3 = postprocess_yolov5s(outs3, 1.0, 0, 0, 640, 640)
            except Exception as exc:
                exc3 = exc
            check('3 类模型(24 通道)后处理不抛异常且 0 框',
                  exc3 is None and b3 is not None and len(s3) == 0
                  and len(b3) == 0,
                  'err=%r n=%r' % (exc3, None if exc3 else len(s3)))

            # ④ DetectGate 从未跑过真推理：全 0 输出通道数 = 3*(5+3) = 24
            class _BoomRknn(object):
                def inference(self, inputs=None):
                    raise RuntimeError('detect=0 时不该调用真推理')

            _rt_off = RuntimeConfig()
            _rt_off.set_model_loaded(True)
            _rt_off.apply({'detect': 0})
            gate3 = DetectGate(_BoomRknn(), _rt_off)
            outs_g = gate3.inference(inputs=[None])
            chs3 = [int(np.asarray(o).shape[1]) for o in outs_g]
            b_g, s_g, c_g = postprocess_yolov5s(outs_g, 1.0, 0, 0, 640, 640)
            check('detect=0 未跑过真推理 -> 通道 3*(5+3)=24 且 0 框',
                  len(outs_g) == 3 and set(chs3) == {24}
                  and gate3.real_calls == 0 and len(s_g) == 0,
                  'channels=%s n=%d' % (chs3, len(s_g)))

            # ⑤ draw_results：cls=2 用新类别名 stain；cls=99 显示 cls_99 不崩
            class _CvStub(object):
                FONT_HERSHEY_SIMPLEX = 0

                def __init__(self):
                    self.texts = []

                def rectangle(self, *a, **k):
                    return None

                def line(self, *a, **k):
                    return None

                def getTextSize(self, label, *a, **k):
                    self.texts.append(label)
                    return ((24, 12), 4)

                def putText(self, img, text, *a, **k):
                    self.texts.append(text)
                    return img

            stub = _CvStub()
            _cv2_backup = cv2
            kept, oob_texts = [], []
            try:
                globals()['cv2'] = stub
                img_t = np.zeros((64, 64, 3), np.uint8)
                draw_results(img_t, np.asarray([[5, 5, 30, 30]]),
                             np.asarray([0.9], np.float32),
                             np.asarray([2], np.int32))
                kept = list(stub.texts)
                stub.texts = []
                draw_results(img_t, np.asarray([[5, 5, 30, 30]]),
                             np.asarray([0.93], np.float32),
                             np.asarray([99], np.int32))
                oob_texts = list(stub.texts)
            finally:
                globals()['cv2'] = _cv2_backup
            check('draw_results: cls=2 -> 显示自定义名 stain',
                  any('stain' in str(t) for t in kept), str(kept))
            check('draw_results: cls=99 -> 显示 cls_99(不抛异常)',
                  any('cls_99' in str(t) for t in oob_texts), str(oob_texts))

            # ⑥ 80 类旧模型不回归：nc=80、通道仍 255
            set_class_names(_coco_backup)
            gate80 = DetectGate(_BoomRknn(), _rt_off)
            outs_80 = gate80.inference(inputs=[None])
            chs80 = [int(np.asarray(o).shape[1]) for o in outs_80]
            check('80 类旧模型不回归：nc=80、通道 3*(5+80)=255',
                  nc() == 80 and null_channels() == 255
                  and set(chs80) == {255},
                  'nc=%d channels=%s' % (nc(), chs80))
        finally:
            set_class_names(_coco_backup)

        # ================= 主动推送 /api/push (t22) =================
        # ① 默认: 关、计数全 0、没地址
        code, _h, body = get('/api/push')
        jp0 = json.loads(body.decode('utf-8'))
        check('/api/push 默认 enabled=0 且计数为 0',
              code == 200 and jp0.get('enabled') == 0
              and (jp0.get('queued'), jp0.get('ok'), jp0.get('fail')) == (0, 0, 0)
              and jp0.get('url') == '', json.dumps(jp0))

        # ② 起一个**真的**接收端(本机临时端口), 验证"板卡真的 POST 出去了"
        recv = dict(items=[])

        class _RecvHandler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def log_message(self, fmt, *a):
                pass

            def do_POST(self):
                try:
                    n = int(self.headers.get('Content-Length') or 0)
                except (TypeError, ValueError):
                    n = 0
                data = self.rfile.read(n) if n > 0 else b''
                recv['items'].append(dict(path=self.path, body=data,
                                          ev=self.headers.get('X-Event'),
                                          ctype=self.headers.get('Content-Type')))
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'ok')

        def post(path, data=b'', timeout=5.0):
            req = urllib.request.Request(base + path, data=data, method='POST')
            try:
                r = urllib.request.urlopen(req, timeout=timeout)
                return r.getcode(), dict(r.getheaders()), r.read()
            except urllib.error.HTTPError as exc:
                return exc.code, dict(exc.headers.items()), exc.read()

        rsrv = ThreadingHTTPServer(('127.0.0.1', 0), _RecvHandler)
        rsrv.daemon_threads = True
        rth = threading.Thread(target=rsrv.serve_forever,
                               kwargs=dict(poll_interval=0.1), daemon=True)
        rth.start()
        rurl = 'http://127.0.0.1:%d/upload' % rsrv.server_address[1]
        try:
            # ③a [D29] 没设置 url 就想开推送 -> 必须 400 明确报错, 不许静默 no-op
            #    (旧行为: enabled=1 被 `want = ... and bool(_url)` 静默忽略 ->
            #     Qt 的 pending 永远等不到回读 -> 两个按钮永久置灰 = "关不掉/开不了")
            code, _h, body = post('/api/push?enabled=1')
            jp0b = json.loads(body.decode('utf-8'))
            _c2, _h2, body2 = get('/api/push')
            jp0b2 = json.loads(body2.decode('utf-8'))
            check('/api/push enabled=1 但 url 为空 -> 400 url empty 且状态没变',
                  code == 400 and jp0b.get('ok') is False
                  and 'url empty' in str(jp0b.get('error') or '')
                  and jp0b2.get('enabled') == 0,
                  '%s / 回读 %s' % (json.dumps(jp0b), json.dumps(jp0b2)))

            # ③ POST enabled=1&url=... -> changed 两项; GET 回读一致
            code, _h, body = post('/api/push?enabled=1&url=' + rurl)
            jp1 = json.loads(body.decode('utf-8'))
            check('/api/push POST enabled=1&url -> changed=[url,enabled]',
                  code == 200 and jp1.get('ok') is True
                  and jp1.get('enabled') == 1 and jp1.get('url') == rurl
                  and set(jp1.get('changed') or []) == {'url', 'enabled'},
                  json.dumps(jp1))
            _c, _h, body = get('/api/push')
            jp1r = json.loads(body.decode('utf-8'))
            check('/api/push GET 回读 enabled=1/url 一致',
                  jp1r.get('enabled') == 1 and jp1r.get('url') == rurl,
                  json.dumps(jp1r))

            # ④ /api/snap 成功后真的 POST 一张到远端(带 X-Event 头)
            _c, _h, body = get('/api/snap')
            t_end = time.time() + 8.0
            while time.time() < t_end and not recv['items']:
                time.sleep(0.05)
            got = recv['items'][0] if recv['items'] else {}
            check('/api/snap 后真的 POST 到远端(image/jpeg + JPEG 字节)',
                  got.get('ctype') == 'image/jpeg'
                  and (got.get('body') or b'')[:2] == b'\xff\xd8'
                  and (got.get('path') or '') == '/upload',
                  'ctype=%r bytes=%d path=%r'
                  % (got.get('ctype'), len(got.get('body') or b''),
                     got.get('path')))
            try:
                ev_push = json.loads(got.get('ev') or '{}')
            except Exception:
                ev_push = {}
            check('推送头 X-Event 是合法 JSON 且字段齐全(seq/type/.../box)',
                  ev_push.get('type') == 'manual'
                  and set(['seq', 'type', 'cls', 'score', 'ratio', 'fps',
                           'cover', 'box']) <= set(ev_push.keys()),
                  json.dumps(ev_push, ensure_ascii=False))
            _c, _h, body = get('/api/push')
            jp2 = json.loads(body.decode('utf-8'))
            check('/api/push ok 计数随真实投递 +1',
                  jp2.get('ok') == 1 and jp2.get('fail') == 0,
                  'ok=%r fail=%r last_error=%r'
                  % (jp2.get('ok'), jp2.get('fail'), jp2.get('last_error')))

            # ⑤ 关掉 -> 回读 0; 非法 url -> 400 bad url
            code, _h, body = post('/api/push?enabled=0')
            jp3 = json.loads(body.decode('utf-8'))
            check('/api/push POST enabled=0 -> 回读一致',
                  code == 200 and jp3.get('enabled') == 0, json.dumps(jp3))
            code, _h, body = post('/api/push?url=ftp://1.2.3.4/x')
            jpb = json.loads(body.decode('utf-8'))
            check('非法 url(ftp://) -> 400 + bad url',
                  code == 400 and jpb.get('ok') is False
                  and jpb.get('error') == 'bad url',
                  'code=%s %s' % (code, json.dumps(jpb)))

            # ⑥ test=1: 立刻推一张测试图(走同一个队列)
            code, _h, body = post('/api/push?enabled=1&url=' + rurl + '&test=1')
            jt = json.loads(body.decode('utf-8'))
            check('/api/push test=1 -> 测试图已入队',
                  code == 200 and jt.get('test_queued') == 1, json.dumps(jt))
        finally:
            try:                       # 别让后面的 /api/snap 一直往死端口重试
                post('/api/push?enabled=0')
            except Exception:
                pass
            try:
                rsrv.shutdown()
                rsrv.server_close()
            except Exception:
                pass

        # ⑦ 有界队列策略(离线, 不起线程): 9 次入队 -> queued<=8 且 fail>=1
        _qp = FramePusher(url='http://127.0.0.1:1/x', enabled=True, start=False)
        for _i in range(9):
            _qp.enqueue(b'\xff\xd8x\xff\xd9', dict(seq=_i, type='detect'))
        _qst = _qp.get()
        check('/api/push 队列有界: 9 次入队 -> queued=8 且 fail>=1(丢最旧)',
              _qst['queued'] == PUSH_QUEUE_MAX and _qst['fail'] >= 1,
              json.dumps(_qst))

        # ================= /m 手机低带宽页 (t22) =================
        code, hdr, body = get('/m')
        mhtml = body.decode('utf-8', 'ignore')
        check('/m 200 + text/html + viewport(手机竖屏可读)',
              code == 200
              and str(hdr.get('Content-Type', '')).startswith('text/html')
              and 'name="viewport"' in mhtml, 'code=%s' % code)
        check('/m 中段是低带宽推流 /stream.mjpg?q=40&w=640&maxfps=5',
              '/stream.mjpg?q=40&w=640&maxfps=5' in mhtml)
        check('/m 有告警: 1s 轮询 /api/events + 红横幅 + WebAudio 提示音',
              '/api/events?since=' in mhtml and 'banner' in mhtml
              and 'AudioContext' in mhtml)
        check('/m 有缺陷墙 + 事件区 + 本机地址块',
              '/api/defects' in mhtml and '/defects/' in mhtml
              and (('http://' in mhtml) or ('没取到网卡地址' in mhtml)))
        check('local_ips 覆盖 end0/end1/wlan0 且不抛异常',
              set(['end0', 'end1', 'wlan0']) <= set(local_ips.__defaults__[0])
              and isinstance(local_ips(), list), str(local_ips()))

        # ================= /api/net 网卡枚举 (t14) =================
        # ① 真机枚举: 200 + ifs 是 list + 每项字段齐(没有网卡也必须是 [] + ok:true)
        code, _h, body = get('/api/net')
        jnet = json.loads(body.decode('utf-8'))
        _ifs = jnet.get('ifs')
        check('/api/net 200 + ok:true + ifs 是 list',
              code == 200 and jnet.get('ok') is True and isinstance(_ifs, list),
              'code=%s ifs=%r' % (code, _ifs))
        check('/api/net ifs 每项含 name/addr/wireless/up/carrier',
              all(isinstance(it.get('name'), str) and isinstance(it.get('addr'), str)
                  and it.get('wireless') in (0, 1)
                  and ('up' in it) and ('carrier' in it) for it in _ifs),
              json.dumps(_ifs))
        # ② 不许出现环回/通配地址
        check('/api/net 不含 127.0.0.1 与 0.0.0.0',
              all(it.get('addr') not in ('127.0.0.1', '0.0.0.0')
                  and not str(it.get('addr') or '').startswith('127.')
                  for it in _ifs), json.dumps(_ifs))
        # ③ urls.mobile: http:// 开头 / /m 结尾 / 端口=服务实际监听端口
        _mobile_urls = ((jnet.get('urls') or {}).get('mobile') or [])
        _port = srv.server_address[1]
        check('/api/net urls.mobile 每个都是 http://<ip>:<实际端口>/m',
              len(_mobile_urls) == len(_ifs)
              and all(u.startswith('http://') and u.endswith('/m')
                      and (':%d/m' % _port) in u for u in _mobile_urls),
              'port=%d %r' % (_port, _mobile_urls))
        print('[http-selftest] /api/net -> %s' % json.dumps(jnet, ensure_ascii=False))

        # ④ 注入假网卡(枚举函数可注入): wlx<mac> 判无线 + 无线优先排序 + 排除 lo/127
        _fake = lambda: [
            dict(name='end0', addr='192.168.10.50'),
            dict(name='wlx8c7ab3f81b37', addr='192.168.43.158'),
            dict(name='wlan1', addr='192.168.5.9'),
            dict(name='lo', addr='127.0.0.1'),
            dict(name='eth9', addr='0.0.0.0'),
        ]
        _fi = net_ifaces(enum=_fake)
        check('net_ifaces(注入): wlx<mac>/wlan 判 wireless, 无线优先, 排除 lo/127/0.0.0.0',
              [it['name'] for it in _fi] == ['wlan1', 'wlx8c7ab3f81b37', 'end0']
              and [it['wireless'] for it in _fi] == [1, 1, 0]
              and [it['name'] for it in _fi] == sorted(
                  [it['name'] for it in _fi],
                  key=lambda n: (0 if is_wireless_iface(n) else 1, n)),
              json.dumps(_fi))
        # ⑤ 端口取自入参, 且不带尾斜杠
        _fu = net_urls(9123, ifs=_fi)['mobile']
        check('net_urls(端口入参): 9123 生效 + 无线在前 + 不带尾斜杠',
              _fu == ['http://192.168.5.9:9123/m',
                      'http://192.168.43.158:9123/m',
                      'http://192.168.10.50:9123/m'], str(_fu))
        # ⑤b 假 sysfs: operstate/carrier 读得到就给 1/0, 读不到给 null(本机没有真实 sysfs)
        _fake_sysnet = os.path.join(tmpdir, 'net_sys_class')
        for _if, _state, _carrier in (('wlx8c7ab3f81b37', 'up', '1'),
                                      ('end0', 'down', '0')):
            _d = os.path.join(_fake_sysnet, _if)
            os.makedirs(_d, exist_ok=True)
            for _fn, _v in (('operstate', _state), ('carrier', _carrier)):
                _fh = io.open(os.path.join(_d, _fn), 'w', encoding='utf-8')
                try:
                    _fh.write(_v + '\n')
                finally:
                    _fh.close()
        _fs = net_ifaces(enum=_fake, sys_net=_fake_sysnet)
        _flags = {it['name']: (it['up'], it['carrier']) for it in _fs}
        check('net_ifaces(假 sysfs): up/carrier 读到=1/0, 读不到=null',
              _flags.get('wlx8c7ab3f81b37') == (1, 1)
              and _flags.get('end0') == (0, 0)
              and _flags.get('wlan1') == (None, None), str(_flags))
        # ⑥ /m 页面用的地址与 /api/net 一致(net_ifaces 的 wireless 优先顺序)
        check('/m 地址来源与 /api/net 一致(local_ips == ifs.addr)',
              local_ips() == [it['addr'] for it in net_ifaces()],
              '%r vs %r' % (local_ips(), [it['addr'] for it in net_ifaces()]))

        # ================= 低带宽 JPEG 参数 (t22) =================
        _po = parse_jpeg_opts('q=999&w=1&maxfps=999&diff=-5', default_w=0)
        check('parse_jpeg_opts 钳位: q=999->95, w=1->160, maxfps=999->30, diff=-5->0',
              (_po['q'], _po['w'], _po['maxfps'], _po['diff'])
              == (95, 160, 30.0, 0.0), str(_po))
        _po2 = parse_jpeg_opts('q=abc&w=0&maxfps=0&diff=abc', default_w=960)
        check('parse_jpeg_opts 缺省/非数字: q=75, w=--http-maxw(960), maxfps=10, diff=0',
              (_po2['q'], _po2['w'], _po2['maxfps'], _po2['diff'])
              == (75, 960, HTTP_JPEG_MAX_FPS, 0.0), str(_po2))

        # /stream.mjpg?q=40&w=640&maxfps=5 -> 生效参数回在响应头, 且真的收到分段
        sq = sw = sf = sd = None
        sbuf = b''
        try:
            r = urllib.request.urlopen(
                base + '/stream.mjpg?q=40&w=640&maxfps=5', timeout=5.0)
            hh = dict(r.headers.items())
            sq, sw = hh.get('X-JPEG-Q'), hh.get('X-JPEG-W')
            sf, sd = hh.get('X-JPEG-FPS'), hh.get('X-JPEG-DIFF')
            t_end = time.time() + 3.0
            while time.time() < t_end and len(sbuf) < 2048:
                chunk = r.read(512)
                if not chunk:
                    break
                sbuf += chunk
            r.close()
        except Exception as exc:
            print('[http-selftest] /stream.mjpg?q=40 异常: %s' % exc)
        check('/stream.mjpg?q=40&w=640&maxfps=5 参数生效(响应头回显)',
              (sq, sw, sf) == ('40', '640', '5.0'),
              'q=%r w=%r fps=%r diff=%r' % (sq, sw, sf, sd))
        check('/stream.mjpg?q=40&w=640 仍能收到 JPEG 分段',
              (b'--frame' in sbuf) or (b'\xff\xd8' in sbuf),
              'len=%d' % len(sbuf))
        # 非法值: q=999 -> 95, w=1 -> 160, maxfps=999 -> 30
        sq2 = sw2 = sf2 = None
        try:
            r = urllib.request.urlopen(
                base + '/stream.mjpg?q=999&w=1&maxfps=999&diff=abc', timeout=5.0)
            hh2 = dict(r.headers.items())
            sq2, sw2, sf2 = hh2.get('X-JPEG-Q'), hh2.get('X-JPEG-W'), hh2.get('X-JPEG-FPS')
            r.close()
        except Exception as exc:
            print('[http-selftest] /stream.mjpg 钳位检查异常: %s' % exc)
        check('/stream.mjpg 非法参数钳位: q=999->95, w=1->160, maxfps=999->30',
              (sq2, sw2, sf2) == ('95', '160', '30.0'),
              'q=%r w=%r fps=%r' % (sq2, sw2, sf2))
        # /snap.jpg 也认这些参数(单帧): q=999 -> 95, w=100 -> 160
        code, hdr, body = get('/snap.jpg?q=999&w=100')
        check('/snap.jpg 非法参数钳位: q=999->95, w=100->160(且仍是合法 JPEG)',
              code == 200 and hdr.get('X-JPEG-Q') == '95'
              and hdr.get('X-JPEG-W') == '160'
              and body[:2] == b'\xff\xd8' and body[-2:] == b'\xff\xd9',
              'q=%r w=%r len=%d' % (hdr.get('X-JPEG-Q'), hdr.get('X-JPEG-W'),
                                    len(body)))

        # ================= 目标码率单位 p20+ (t22) =================
        check('TARGET_STEP_KBPS=40 且复位默认 target5=9 -> 360 KB/s',
              TARGET_STEP_KBPS == 40 and TGT_RESET_KBPS == 360,
              'step=%d reset=%d' % (TARGET_STEP_KBPS, TGT_RESET_KBPS))
        _tmap = [(t, parse_wm(_wm_encode_value(auto=1, target5=t))['target_kbps'])
                 for t in (7, 8, 9, 10, 11)]
        check('target5 7/8/9/10/11 -> 280/320/360/400/440 KB/s',
              [v for _t, v in _tmap] == [280, 320, 360, 400, 440], str(_tmap))

        # ---- /stream.mjpg (multipart, boundary 固定 frame) ----
        stype, buf = '', b''
        try:
            r = urllib.request.urlopen(base + '/stream.mjpg', timeout=5.0)
            stype = str(r.headers.get('Content-Type', ''))
            t_end = time.time() + 3.0
            while time.time() < t_end and len(buf) < 4096:
                chunk = r.read(1024)
                if not chunk:
                    break
                buf += chunk
            r.close()
        except Exception as exc:
            print('[http-selftest] /stream.mjpg 异常: %s' % exc)
        check('/stream.mjpg boundary=frame', 'boundary=frame' in stype, stype)
        check('/stream.mjpg 收到 JPEG 分段',
              (b'--frame' in buf) and (b'\xff\xd8' in buf),
              'len=%d' % len(buf))

        # ---- / 状态页 ----
        code, hdr, body = get('/')
        html = body.decode('utf-8', 'ignore')
        check('/ 状态页 200', code == 200, 'code=%s' % code)
        check('/ 状态页含接口说明',
              ('/api/status' in html) and ('stream.mjpg' in html)
              and ('/api/defects' in html))
    except Exception as exc:
        import traceback
        print('[http-selftest] 自检过程中抛异常: %r' % (exc,))
        traceback.print_exc()
        results.append(('自检未抛异常', False))
    finally:
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    nfail = sum(1 for _n, v in results if not v)
    print('[http-selftest] ---- 共 %d 项检查, 失败 %d 项 ----' % (len(results), nfail))
    allpass = bool(results) and nfail == 0
    print('[http-selftest] %s' % ('PASS' if allpass else 'FAIL'))
    return 0 if allpass else 1


def letterbox(img, new_shape=640, color=(0, 0, 0)):
    h, w = img.shape[:2]
    r = min(new_shape / h, new_shape / w)
    new_w, new_h = int(w * r), int(h * r)
    dw = (new_shape - new_w) / 2
    dh = (new_shape - new_h) / 2
    img_resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    top    = int(round(dh - 0.1))
    bottom = int(round(dh + 0.1))
    left   = int(round(dw - 0.1))
    right  = int(round(dw + 0.1))
    img_padded = cv2.copyMakeBorder(
        img_resized, top, bottom, left, right,
        cv2.BORDER_CONSTANT, value=color
    )
    return img_padded, r, dw, dh


def xywh2xyxy(x):
    y = np.zeros_like(x)
    y[:, 0] = x[:, 0] - x[:, 2] / 2
    y[:, 1] = x[:, 1] - x[:, 3] / 2
    y[:, 2] = x[:, 0] + x[:, 2] / 2
    y[:, 3] = x[:, 1] + x[:, 3] / 2
    return y


def process(input, mask, anchors):
    anchors = [anchors[i] for i in mask]
    grid_h, grid_w = map(int, input.shape[0:2])
    box_confidence = np.expand_dims(input[..., 4], axis=-1)
    box_class_probs = input[..., 5:]
    box_xy = input[..., :2] * 2 - 0.5
    col = np.tile(np.arange(0, grid_w), grid_w).reshape(-1, grid_w)
    row = np.tile(np.arange(0, grid_h).reshape(-1, 1), grid_h)
    col = col.reshape(grid_h, grid_w, 1, 1).repeat(3, axis=-2)
    row = row.reshape(grid_h, grid_w, 1, 1).repeat(3, axis=-2)
    grid = np.concatenate((col, row), axis=-1)
    box_xy += grid
    box_xy *= int(IMG_SIZE / grid_h)
    box_wh = pow(input[..., 2:4] * 2, 2)
    box_wh = box_wh * anchors
    return np.concatenate((box_xy, box_wh), axis=-1), box_confidence, box_class_probs


def filter_boxes(boxes, box_confidences, box_class_probs):
    boxes = boxes.reshape(-1, 4)
    box_confidences = box_confidences.reshape(-1)
    box_class_probs = box_class_probs.reshape(-1, box_class_probs.shape[-1])
    _box_pos = np.where(box_confidences >= OBJ_THRESH)
    boxes = boxes[_box_pos]
    box_confidences = box_confidences[_box_pos]
    box_class_probs = box_class_probs[_box_pos]
    class_max_score = np.max(box_class_probs, axis=-1)
    classes = np.argmax(box_class_probs, axis=-1)
    _class_pos = np.where(class_max_score >= OBJ_THRESH)
    return boxes[_class_pos], classes[_class_pos], (class_max_score * box_confidences)[_class_pos]


def nms_boxes(boxes, scores):
    x = boxes[:, 0]
    y = boxes[:, 1]
    w = boxes[:, 2] - boxes[:, 0]
    h = boxes[:, 3] - boxes[:, 1]
    areas = w * h
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x[i], x[order[1:]])
        yy1 = np.maximum(y[i], y[order[1:]])
        xx2 = np.minimum(x[i] + w[i], x[order[1:]] + w[order[1:]])
        yy2 = np.minimum(y[i] + h[i], y[order[1:]] + h[order[1:]])
        w1 = np.maximum(0.0, xx2 - xx1 + 0.00001)
        h1 = np.maximum(0.0, yy2 - yy1 + 0.00001)
        inter = w1 * h1
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        inds = np.where(ovr <= NMS_THRESH)[0]
        order = order[inds + 1]
    return np.array(keep)


# [v7] yolov5 **v7.0** 的 export.py 导出的是**单输出** (1, N, 5+nc)：
#        [:, 0:4] = cx, cy, w, h  —— 单位是输入图（640）的像素，可能轻微越界
#        [:, 4]   = objectness    —— 已过 sigmoid
#        [:, 5:]  = 每个类的分数   —— 已过 sigmoid
#      而官方板卡脚本只认 yolov5 **6.x** 的**三张原始特征图** (1, 3*(5+nc), 80/40/20, …)，
#      拿 v7 的模型上板，通道数对不上、三张图会被硬拆，框必然全错。
#      这里加一个**按输出形状自动选路**的解码器：两种格式都能跑（旧模型零回归）。
V7_THRESH = {'obj': OBJ_THRESH, 'nms': NMS_THRESH}


def _empty_det():
    return (np.empty((0, 4), dtype=np.int32),
            np.empty((0,), dtype=np.float32),
            np.empty((0,), dtype=np.int32))


def postprocess_yolov5_v7(outputs, ratio, dw, dh, src_w, src_h):
    """yolov5 v7.0 单输出 (1, N, 5+nc) 的解码：conf=obj*cls -> 按类 NMS -> 反 letterbox"""
    pred = np.asarray(outputs[0], dtype=np.float32)
    pred = pred.reshape(-1, pred.shape[-1])
    nc = pred.shape[-1] - 5
    if nc <= 0 or pred.shape[0] == 0:
        return _empty_det()
    cls_probs = pred[:, 5:5 + nc]
    cls_ids = np.argmax(cls_probs, axis=1)
    scores = pred[:, 4] * cls_probs[np.arange(cls_probs.shape[0]), cls_ids]
    keep = np.where(scores >= V7_THRESH['obj'])[0]
    if keep.size == 0:
        return _empty_det()
    boxes = xywh2xyxy(pred[keep, :4])
    scores = scores[keep]
    cls_ids = cls_ids[keep]
    nb, ns, ncl = [], [], []
    for c in sorted(set(cls_ids.tolist())):
        idx = np.where(cls_ids == c)[0]
        k = nms_boxes(boxes[idx], scores[idx])
        nb.append(boxes[idx][k])
        ns.append(scores[idx][k])
        ncl.append(cls_ids[idx][k])
    if not nb:
        return _empty_det()
    boxes = np.concatenate(nb)
    scores = np.concatenate(ns)
    cls_ids = np.concatenate(ncl)
    boxes[:, [0, 2]] = (boxes[:, [0, 2]] - dw) / ratio
    boxes[:, [1, 3]] = (boxes[:, [1, 3]] - dh) / ratio
    boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, src_w)
    boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, src_h)
    return boxes.astype(np.int32), scores, cls_ids.astype(np.int32)


def _sig(x):
    """数值稳定的 sigmoid（防 exp 溢出）"""
    x = np.clip(x, -60.0, 60.0)
    return 1.0 / (1.0 + np.exp(-x))


def postprocess_yolov5_raw(outputs, ratio, dw, dh, src_w, src_h):
    """三张**原始特征图**（sigmoid 之前、anchor 解码之前）的解码

    这是 INT8 量化的推荐导出口（`ai/export/export_raw.py`）：
    三个通道组的量级一致（都是 BN 后的卷积输出），int8 量化不丢精度；
    sigmoid 搬到本地做，效果与官方把 sigmoid 烘进模型完全等价。
    """
    outs = [_sig(np.asarray(o, dtype=np.float32)) for o in outputs]
    return postprocess_yolov5s(outs, ratio, dw, dh, src_w, src_h)


def postprocess_auto(outputs, ratio, dw, dh, src_w, src_h):
    """按模型输出形状自动选解码器

    · 1 个输出、ndim==3、最后一维 5+nc  -> v7.0 export.py（分数已 sigmoid）
    · 3 个输出、ndim==4                 -> 三张特征图；再判断是"原始 logits"还是"已 sigmoid"
    """
    try:
        if len(outputs) == 1 and outputs[0] is not None:
            arr = np.asarray(outputs[0])
            if arr.ndim in (2, 3) and arr.shape[-1] >= 6:
                return postprocess_yolov5_v7(outputs, ratio, dw, dh,
                                             src_w, src_h)
        if len(outputs) == 3 and all(o is not None for o in outputs):
            arrs = [np.asarray(o) for o in outputs]
            if all(a.ndim == 4 for a in arrs):
                # 已 sigmoid 的输出必然落在 [0,1]；logits 会越界（负值或 >1.5）
                lo = min(float(a.min()) for a in arrs)
                hi = max(float(a.max()) for a in arrs)
                if lo < -0.5 or hi > 1.5:
                    if RAW_HINT['said'] is False:
                        RAW_HINT['said'] = True
                        print('[INIT] 解码: 三张原始特征图（logits，本地做 sigmoid）'
                              '  范围 %.2f~%.2f' % (lo, hi))
                    return postprocess_yolov5_raw(outputs, ratio, dw, dh,
                                                  src_w, src_h)
    except Exception as exc:
        print('[WARN] 自动选解码器失败，回退到旧三特征图解码: %s' % exc)
    return postprocess_yolov5s(outputs, ratio, dw, dh, src_w, src_h)


RAW_HINT = {'said': False}


def _postproc_selftest():
    """只依赖 numpy：v7 单输出解码的 6 项断言（--selftest 会一起跑）"""
    checks = []
    n = 25200
    zero = np.zeros((1, n, 6), dtype=np.float32)
    b, s, c = postprocess_auto([zero], 0.4, 80.0, 0.0, 1200, 1600)
    checks.append(('全 0 输出 -> 空框、不抛异常', len(b) == 0 and len(s) == 0, ''))

    one = np.zeros((1, n, 6), dtype=np.float32)
    one[0, 0] = [200.0, 300.0, 100.0, 50.0, 0.90, 0.99]   # 640 空间像素
    b, s, c = postprocess_auto([one], 0.4, 80.0, 0.0, 1200, 1600)
    exp = [175, 687, 425, 812]        # 反 letterbox 后：(200-80)/.4=300 等；
    #                                   y1=687.5 -> int32 截断成 687（不是四舍五入）
    ok = (len(b) == 1 and [int(v) for v in b[0]] == exp
          and abs(float(s[0]) - 0.891) < 1e-3 and int(c[0]) == 0)
    checks.append(('单个框反 letterbox 正确 + conf=obj*cls', ok,
                   '%s %.3f %s vs %s' % (b[0] if len(b) else '-',
                                         float(s[0]) if len(s) else -1,
                                         int(c[0]) if len(c) else -1, exp)))

    dup = np.zeros((1, n, 6), dtype=np.float32)
    dup[0, 0] = [200.0, 300.0, 100.0, 50.0, 0.90, 0.99]
    dup[0, 1] = [202.0, 301.0, 100.0, 50.0, 0.80, 0.99]   # 几乎重合
    dup[0, 2] = [500.0, 100.0, 60.0, 40.0, 0.70, 0.99]    # 不相交
    b2, s2, c2 = postprocess_auto([dup], 0.4, 80.0, 0.0, 1200, 1600)
    checks.append(('同类重叠框被 NMS 合并、不相交的保留', len(b2) == 2,
                   'keep=%d' % len(b2)))

    low = np.zeros((1, n, 6), dtype=np.float32)
    low[0, 0] = [200.0, 300.0, 100.0, 50.0, 0.90, 0.10]   # 0.09 < 阈值
    b3, _s3, _c3 = postprocess_auto([low], 0.4, 80.0, 0.0, 1200, 1600)
    checks.append(('低分框被阈值滤掉', len(b3) == 0, ''))

    old = [np.zeros((1, 3 * 6, 80, 80), dtype=np.float32),
           np.zeros((1, 3 * 6, 40, 40), dtype=np.float32),
           np.zeros((1, 3 * 6, 20, 20), dtype=np.float32)]
    try:
        b4, _s4, _c4 = postprocess_auto(old, 1.0, 0.0, 0.0, 640, 640)
        ok4 = len(b4) == 0
    except Exception as exc:
        ok4 = False
        print('[selftest] 旧三特征图回退报错: %s' % exc)
    checks.append(('三张旧格式特征图仍走旧解码（不误判成 v7）', ok4, ''))

    single2 = np.zeros((1, 1, 6), dtype=np.float32)
    single2[0, 0] = [320.0, 320.0, 40.0, 40.0, 0.99, 0.99]
    b5, _s5, _c5 = postprocess_auto([single2], 1.0, 0.0, 0.0, 640, 640)
    checks.append(('N=1 的退化形状也能解', len(b5) == 1, ''))

    # ---- 三张原始特征图（logits）：必须与"先 sigmoid 再走旧解码"完全等价 ----
    def _mk_maps(fn):
        outs = []
        for g in (80, 40, 20):
            m = np.full((1, 18, g, g), -8.0, dtype=np.float32)   # 背景 logit 很低
            m[0, 0:2, 5, 5] = 0.0      # xy logit -> sigmoid 0.5
            m[0, 2:4, 5, 5] = 0.0      # wh
            m[0, 4, 5, 5] = 6.0        # obj 高
            m[0, 5, 5, 5] = 6.0        # cls 高
            outs.append(np.asarray(fn(m), np.float32))
        return outs

    raw_maps = _mk_maps(lambda a: a)
    sig_maps = _mk_maps(_sig)
    b_raw, s_raw, c_raw = postprocess_auto(raw_maps, 1.0, 0.0, 0.0, 640, 640)
    b_sig, s_sig, c_sig = postprocess_auto(sig_maps, 1.0, 0.0, 0.0, 640, 640)
    checks.append(('logits 特征图解码 == 先 sigmoid 再解码（INT8 推荐路的等价性）',
                   len(b_raw) == len(b_sig) and len(b_raw) > 0
                   and np.allclose(np.asarray(b_raw, float),
                                   np.asarray(b_sig, float), atol=1)
                   and np.allclose(np.asarray(s_raw, float),
                                   np.asarray(s_sig, float), atol=1e-5),
                   '%d vs %d 框' % (len(b_raw), len(b_sig))))
    checks.append(('三张特征图不会被误判成 v7 单输出',
                   len(b_raw) >= 1 and len(b_sig) >= 1, ''))
    fail = 0
    for name, ok, extra in checks:
        print('  [%s] %-46s %s' % ('PASS' if ok else 'FAIL', name, extra))
        if not ok:
            fail += 1
    print('[selftest] postproc: %d 项, 失败 %d 项 -> %s'
          % (len(checks), fail, 'PASS' if fail == 0 else 'FAIL'))
    return 0 if fail == 0 else 1


def postprocess_yolov5s(outputs, ratio, dw, dh, src_w, src_h):
    input_data = []
    for out in outputs:
        data = out.reshape([3, -1] + list(out.shape[-2:]))
        input_data.append(np.transpose(data, (2, 3, 0, 1)))

    boxes, classes, scores = [], [], []
    for input, mask in zip(input_data, MASKS):
        b, c, s = process(input, mask, ANCHORS)
        b, c, s = filter_boxes(b, c, s)
        boxes.append(b)
        classes.append(c)
        scores.append(s)

    if not boxes:
        return np.empty((0, 4), dtype=int), np.empty((0,)), np.empty((0,), dtype=int)

    boxes = np.concatenate(boxes)
    boxes = xywh2xyxy(boxes)
    classes = np.concatenate(classes)
    scores = np.concatenate(scores)

    nboxes, nclasses, nscores = [], [], []
    for c in set(classes.tolist()):
        inds = np.where(classes == c)
        b = boxes[inds]
        s = scores[inds]
        keep = nms_boxes(b, s)
        nboxes.append(b[keep])
        nclasses.append(classes[inds][keep])
        nscores.append(s[keep])
    if not nboxes:
        return np.empty((0, 4), dtype=int), np.empty((0,)), np.empty((0,), dtype=int)

    boxes = np.concatenate(nboxes)
    scores = np.concatenate(nscores)
    classes = np.concatenate(nclasses)

    boxes[:, [0, 2]] = (boxes[:, [0, 2]] - dw) / ratio
    boxes[:, [1, 3]] = (boxes[:, [1, 3]] - dh) / ratio
    boxes[:, 0] = np.clip(boxes[:, 0], 0, src_w)
    boxes[:, 1] = np.clip(boxes[:, 1], 0, src_h)
    boxes[:, 2] = np.clip(boxes[:, 2], 0, src_w)
    boxes[:, 3] = np.clip(boxes[:, 3], 0, src_h)
    return boxes.astype(np.int32), scores, classes.astype(np.int32)


def draw_results(img, boxes, scores, cls_ids):
    for box, score, cid in zip(boxes, scores, cls_ids):
        x1, y1, x2, y2 = box
        name  = CLASS_NAMES[cid] if cid < len(CLASS_NAMES) else str(cid)
        label = '{} {:.2f}'.format(name, score)
        np.random.seed(cid + 42)
        color = tuple(int(c) for c in np.random.randint(60, 255, 3))
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        cv2.rectangle(img, (x1, y1 - th - 8), (x1 + tw + 4, y1), color, -1)
        cv2.putText(img, label, (x1 + 2, y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
    return img


# ============================================================================
# [我们加的] 画框类别名兜底（t21）
#   · 类内 id：交给官方 draw_results()（显示 CLASS_NAMES 里的名字 -> 自定义类别生效）
#   · 越界 id：官方那行只会显示纯数字（"99"），这里改画 'cls_99'
#   · 任何异常都吞掉：类别表与模型对不上时也不能让主循环崩
# 放在官方 draw_results() 定义之后才拿得到它，所以插在 class FPSCounter 前面。
# ============================================================================
_draw_results_official = draw_results


def _draw_oob_results(img, boxes, scores, cls_ids):
    """越界类别 id 的兜底画法：红框 + 'cls_<id> <score>'"""
    for box, score, cid in zip(boxes, scores, cls_ids):
        try:
            x1, y1, x2, y2 = [int(v) for v in list(box)[:4]]
            label = '%s %.2f' % (class_name_of(cid), float(score))
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 2)
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX,
                                          0.55, 1)
            cv2.rectangle(img, (x1, y1 - th - 8), (x1 + tw + 4, y1),
                          (0, 0, 255), -1)
            cv2.putText(img, label, (x1 + 2, y1 - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
        except Exception:
            pass
    return img


def draw_results(img, boxes, scores, cls_ids):
    """类别名跟随 CLASS_NAMES；越界 id 显示 cls_<id>；不抛异常"""
    try:
        n = len(CLASS_NAMES)
        try:
            ids = [int(c) for c in cls_ids]
        except Exception:
            ids = []
        if not ids:
            return _draw_results_official(img, boxes, scores, cls_ids)
        keep = [i for i, c in enumerate(ids) if 0 <= c < n]
        oob = [i for i, c in enumerate(ids) if not (0 <= c < n)]
        bx = np.asarray(boxes)
        sc = np.asarray(scores)
        ci = np.asarray(cls_ids)
        if keep:
            idx = np.asarray(keep, dtype=int)
            _draw_results_official(img, bx[idx], sc[idx], ci[idx])
        if oob:
            idx = np.asarray(oob, dtype=int)
            _draw_oob_results(img, bx[idx], sc[idx], ci[idx])
    except Exception as exc:
        print('[WARN] draw_results 兜底(类别表与模型对不上?): %s' % exc)
    return img


class FPSCounter:
    def __init__(self, window=30):
        self.window = window
        self.times  = []
    def tick(self):
        self.times.append(time.time())
        if len(self.times) > self.window:
            self.times.pop(0)
    def fps(self):
        if len(self.times) < 2:
            return 0.0
        dt = self.times[-1] - self.times[0]
        return (len(self.times) - 1) / dt if dt > 0 else 0.0


def center_window(win_name, fw, fh):
    try:
        import tkinter
        _r = tkinter.Tk()
        _r.withdraw()
        sw, sh = _r.winfo_screenwidth(), _r.winfo_screenheight()
        _r.destroy()
    except Exception:
        sw, sh = 1920, 1080
    cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(win_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)


def main():
    global MODEL_PATH, DEVICE
    import sys
    # 自检路径必须用 SystemExit 传退出码(官方结尾 main() 的返回值会被丢掉)
    if '--selftest' in sys.argv:
        _rc1 = _wm_selftest()
        _rc2 = _postproc_selftest()
        raise SystemExit(1 if (_rc1 or _rc2) else 0)
    http_opts = _parse_http_opts(sys.argv[1:])
    if http_opts['selftest']:
        raise SystemExit(_http_selftest(sys.argv[1:]))
    if '--store-info' in sys.argv:
        # 不用摄像头/不开服务, 只做"存图目录到底在哪"的解析与告警(排障用)
        _resolve_store_dir_cli(sys.argv[1:])
        raise SystemExit(0)
    if '--model' in sys.argv:
        MODEL_PATH = sys.argv[sys.argv.index('--model') + 1]
    if '--src' in sys.argv:
        DEVICE = sys.argv[sys.argv.index('--src') + 1]
    no_hud = '--no-hud' in sys.argv
    probe = '--probe' in sys.argv
    prof = '--profile' in sys.argv
    # [我们加的] 自定义类别(t21): --classes > <模型同目录>/classes.txt > 内置 COCO 80 类
    _cli_classes = _opt_value(sys.argv, '--classes')
    for _k, _flag in (('obj', '--conf'), ('nms', '--nms')):
        _v = _opt_value(sys.argv, _flag)
        if _v:
            try:
                V7_THRESH[_k] = float(_v)
            except Exception:
                print('[WARN] %s 的值无效: %s' % (_flag, _v))
    if V7_THRESH['obj'] != OBJ_THRESH or V7_THRESH['nms'] != NMS_THRESH:
        print('[INIT] 阈值: obj=%.2f nms=%.2f' % (V7_THRESH['obj'], V7_THRESH['nms']))
    _cls_names, _cls_src, _cls_warn = resolve_class_names(MODEL_PATH, _cli_classes)
    if _cls_warn:
        print(_cls_warn)
        if _cli_classes and _cls_names is None:
            raise SystemExit(1)
    if _cls_names:
        set_class_names(_cls_names)
    print_class_names(_cls_src or '')
    # [t22] 目标码率单位提示: p20 起 target5 的单位是 40 KB/s; p19 及以前是 5 KB/s
    #      (用新脚本读旧位流会**放大 8 倍**, 现场先看这一行再怀疑码率)
    print('[TGT] 目标码率单位 = %d KB/s/单位 (target5 x%d: 7/8/9/10/11 -> '
          '280/320/360/400/440 KB/s); 复位默认 target5=%d -> %d KB/s; '
          '注意 p19 及以前的位流是 5 KB/s/单位, 用本脚本读会放大 8 倍'
          % (TARGET_STEP_KBPS, TARGET_STEP_KBPS, TGT_RESET_5, TGT_RESET_KBPS))
    if cv2 is None:
        print('[ERR] 需要 opencv-python（厂商 whls：opencv_python-5.0.0.93-cp311）')
        raise SystemExit(1)
    if RKNNLite is None:
        print('[ERR] 需要 rknn_toolkit_lite2（厂商 whls：rknn_toolkit_lite2-2.3.0-cp311）')
        raise SystemExit(1)

    print('[INIT] 加载 RKNN 模型: {}'.format(MODEL_PATH))
    rknn = RKNNLite()
    ret = rknn.load_rknn(MODEL_PATH)
    if ret != 0:
        raise RuntimeError('load_rknn 失败: {}'.format(ret))
    ret = rknn.init_runtime(core_mask=RKNNLite.NPU_CORE_0)
    if ret != 0:
        raise RuntimeError('init_runtime 失败: {}'.format(ret))
    print('[INIT] NPU 双核初始化完成')
    # [我们加的] 运行时开关(t11): 有模型 -> detect 默认开; 再用门控包住 rknn
    RUNTIME.set_model_loaded(True)
    rknn = DetectGate(rknn, RUNTIME)

    print('[CAM] 打开 {} ...'.format(DEVICE))
    cap = cv2.VideoCapture(DEVICE, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'YUYV'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FPS)
    if not cap.isOpened():
        raise RuntimeError('无法打开 {}，请检查连接'.format(DEVICE))
    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print('[CAM] 分辨率: {}x{}'.format(actual_w, actual_h))

    wm_sm = WmSmoother()
    _acc = dict(read=0.0, pre=0.0, infer=0.0, post=0.0, draw=0.0, n=0)
    _tl = time.time()
    if probe:
        ok_p, fp = cap.read()
        if not ok_p:
            print('[PROBE] 取帧失败')
        else:
            print('[PROBE] 分辨率 %dx%d' % (fp.shape[1], fp.shape[0]))
            roi = fp[0:ROI_H, 0:ROI_W]
            gg = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            cells = [float(gg[:, i * CELL:(i + 1) * CELL].mean()) for i in range(64)]
            print('[PROBE] 64 格均值:')
            print('  ' + ' '.join('%3.0f' % v for v in cells[:32]))
            print('  ' + ' '.join('%3.0f' % v for v in cells[32:]))
            print('[PROBE] min=%.1f max=%.1f diff=%.1f  (diff<20 判为没有水印)'
                  % (min(cells), max(cells), max(cells) - min(cells)))
            v = decode_wm(fp)
            print('[PROBE] 解出 64bit = %s' % (('0x%016X' % v) if v is not None else 'None'))
            print('[PROBE] 解析 = %s' % parse_wm(v))
            cv2.imwrite('wm_roi.png', cv2.resize(roi, (ROI_W, ROI_H * 16),
                                                 interpolation=cv2.INTER_NEAREST))
            print('[PROBE] ROI 已存 wm_roi.png（放大 16 倍）')
        cap.release()
        return 0
    # [我们加的] HTTP 服务 + 事件日志: 与主循环同进程, 绝不再开一次 VideoCapture
    hub = FrameHub()
    # [t22] 主动推送(板卡 -> 远端 HTTP POST): 启动即建线程, 默认关着不发;
    #       主循环/HTTP 线程只 enqueue, 网络 IO 全在这个后台线程里
    pusher = FramePusher()
    # [t18] 尺寸测量适配层: 启动时**只 import 一次**(预热), 逐帧不测量(触发式),
    #       标定一次(进 dim 模式第一帧后台标定 / 或 /api/dim?calib=1)
    dim_engine = DimEngine(_opt_value(sys.argv, '--dim-module'))
    http_srv = None
    defect_saver = None
    events = None
    store_dir = None
    if http_opts['enabled']:
        # [t14] '--save-dir auto' 自动找 SD/U 盘; 其它值原样; 最后一定打印真实来源与告警
        store_dir = resolve_store_dir(http_opts['save_dir'])
        if not http_opts.get('save_dir_given'):
            _say('[STORE] 未指定 --save-dir，用默认目录 %s'
                 '（如不是 SD 卡，请加 --save-dir auto 或显式指定）' % store_dir)
        events = EventLog(store_dir)
        if http_opts['defect_save']:
            defect_saver = DefectSaver(store_dir)
        try:
            http_srv, _http_th = start_http_server(
                hub, http_opts['port'], store_dir, events,
                quiet=not http_opts['verbose'], maxw=http_opts['maxw'],
                host='0.0.0.0', pusher=pusher, dim=dim_engine)
        except OSError as exc:
            print('[ERR] HTTP 服务启动失败(端口 %d 被占用?): %s'
                  % (http_opts['port'], exc))
            cap.release()
            raise SystemExit(1)
    fps_counter = FPSCounter(window=30)
    frame_index = 0
    win_moved = False
    print('[RUN] 开始推理，按 Ctrl+C 退出...\n')

    while True:
        ret, frame = cap.read()
        if not ret:
            print('[WARN] 取帧失败')
            time.sleep(0.01)
            continue
        # [我们加的] 解视频水印(压缩率)
        wm_info = wm_sm.update(parse_wm(decode_wm(frame)), time.time())
        src_h, src_w = frame.shape[:2]
        _t0 = time.time()
        img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img_lb, ratio, dw, dh = letterbox(img_rgb, IMG_SIZE)
        input_data = np.expand_dims(img_lb, axis=0)
        t1 = _t1 = time.time()
        outputs = rknn.inference(inputs=[input_data])
        t2 = time.time(); _t2 = t2
        if outputs is None or outputs[0] is None:
            print('[ERR] 推理返回空结果')
            continue
        boxes, scores, cls_ids = postprocess_auto(
            outputs, ratio, dw, dh, src_w, src_h
        )
        _t3 = time.time()
        frame = draw_results(frame, boxes, scores, cls_ids)
        frame_index += 1
        if frame_index % SAVE_EVERY == 0:
            cv2.imwrite(SAVE_PATH, frame)
        # [t18] 尺寸测量模式: dim=1 时**不跑 YOLO**(detect 被强制 0 -> DetectGate 直接
        # 返回全 0 输出, 省掉 NPU 那 ~24 ms), 画面照常 ~14 fps;
        # 测量是**触发式**的(measure() 0.32 s/帧, 每帧跑会把帧率打到 2~3 fps):
        #   · /api/dim?run=1 立刻量一次(Qt 按钮)
        #   · /api/config?dim_auto=1[&dim_auto_sec=N] 每 N 秒(默认 5, 不许低于 2)量一次
        #   · 其余时间 tick() 只回最近一次结果, O(1) 不测量
        _dim_on = bool(RUNTIME.dim())
        _dim_res = None
        if _dim_on:
            _dim_res = dim_engine.tick(frame, fps=fps_counter.fps(),
                                       auto_sec=RUNTIME.dim_auto_sec())
        # [我们加的] 压缩率 HUD(dim 模式下不画: 面板会盖住测量覆盖层)
        if not no_hud and not _dim_on:
            frame = draw_wm_hud(frame, wm_info, fps_counter.fps(),
                                wm_sm.kbps(), True)
        # [t18] 尺寸测量 HUD: 四边形 + 4 角点 + 每边 mm + 每目标 OK/NG + tol(判定线)
        #       与 sigma(精度) 分开显示; 三态 CALIBRATING / CALIBRATED / UNTRUSTED
        if _dim_on and not no_hud:
            try:
                frame = draw_dim_hud(frame, dim_engine.hud())
            except Exception as _exc:
                print('[WARN] dim HUD 画失败(不影响测量/接口): %s' % _exc)
        # [我们加的] 有缺陷就存一张 det_HHMMSS.jpg 到 save-dir(限频 2s) + detect 事件;
        # autosave=0 时这一步整体跳过(不写盘、不发事件), 手动截图 /api/snap 仍可用
        # [t22] 给了 pusher 时同时在**存成功后**入队一张图 -> 后台线程推到远端
        save_defect_if_enabled(defect_saver, frame, boxes, scores, cls_ids,
                               events, wm_info, fps_counter.fps(), RUNTIME,
                               pusher=pusher)
        try:
            cv2.imshow('MIPI + YOLOv5s Detection', frame)
            if not win_moved:
                win_moved = True
                center_window('MIPI + YOLOv5s Detection',
                              frame.shape[1], frame.shape[0])
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
        except cv2.error:
            pass
        if prof:
            _acc['read'] += _t0 - _tl
            _acc['pre'] += _t1 - _t0
            _acc['infer'] += _t2 - _t1
            _acc['post'] += _t3 - _t2
            _acc['draw'] += time.time() - _t3
            _acc['n'] += 1
            if _acc['n'] >= 15:
                _n = float(_acc['n'])
                print('[PROF] fps=%.1f  read=%.1fms  pre=%.1fms  infer=%.1fms  post=%.1fms  draw=%.1fms'
                      % (fps_counter.fps(), _acc['read']/_n*1e3, _acc['pre']/_n*1e3, _acc['infer']/_n*1e3, _acc['post']/_n*1e3, _acc['draw']/_n*1e3))
                for _k in _acc:
                    _acc[_k] = 0
        _tl = time.time()
        fps_counter.tick()
        fps_val = fps_counter.fps()
        info = 'YOLOv5s-Relu | NPU | {:.1f} FPS | det:{}'.format(fps_val, len(scores))
        cv2.putText(frame, info, (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 0), 2)
        infer_ms = (t2 - t1) * 1000
        print('\r[FPS] {:.1f}  [NPU] {:.1f}ms  [DET] {} obj'.format(
            fps_val, infer_ms, len(scores)), end='', flush=True)

        # [我们加的] 把这一帧发布给 HTTP 线程(传引用, 之后本帧不再被修改)
        if http_srv is not None:
            hub.publish(frame, wm_info, wm_sm.kbps(), fps_val)
    cap.release()
    rknn.release()
    if http_srv is not None:
        try:
            http_srv.shutdown()
            http_srv.server_close()
        except Exception:
            pass
    print('\n[EXIT] 已退出')

if __name__ == '__main__':
    main()
