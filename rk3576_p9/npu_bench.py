#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板卡上直接跑 .rknn（不需要 FPGA 视频流）——验 NPU 真机能出框 + 量耗时

用法: python3 /tmp/_npu_bench.py <model.rknn> <img1.jpg> [img2.jpg ...]
"""
import sys
import time

import cv2
import numpy as np
from rknnlite.api import RKNNLite

sys.path.insert(0, '/home/rock/yolo')
import rk_pcb_view as R          # 复用板上那份解码器（postprocess_auto）

model = sys.argv[1]
imgs = sys.argv[2:]

rk = RKNNLite()
if rk.load_rknn(model) != 0:
    raise SystemExit('[ERR] load_rknn 失败')
if rk.init_runtime() != 0:
    raise SystemExit('[ERR] init_runtime 失败')
print('[OK] 模型已加载: %s' % model)

for p in imgs:
    img0 = cv2.imread(p)
    if img0 is None:
        print('[WARN] 读不到 %s' % p)
        continue
    H0, W0 = img0.shape[:2]
    r = min(640 / H0, 640 / W0)
    nw, nh = int(round(W0 * r)), int(round(H0 * r))
    dw, dh = (640 - nw) / 2.0, (640 - nh) / 2.0
    im = cv2.resize(img0, (nw, nh))
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    im = cv2.copyMakeBorder(im, top, bottom, left, right,
                            cv2.BORDER_CONSTANT, value=(114, 114, 114))
    x = np.ascontiguousarray(im[:, :, ::-1]).astype(np.uint8)[None]   # RGB NHWC

    outs = rk.inference(inputs=[x], data_format='nhwc')
    ts = []
    for _ in range(20):
        t = time.time()
        outs = rk.inference(inputs=[x], data_format='nhwc')
        ts.append((time.time() - t) * 1e3)
    b, s, c = R.postprocess_auto(outs, r, dw, dh, W0, H0)
    print('\n%s  %dx%d  输出 %s' % (p.split('/')[-1], W0, H0, outs[0].shape))
    print('   NPU 单次推理: 平均 %.1f ms  最小 %.1f  最大 %.1f  (20 次)'
          % (sum(ts) / len(ts), min(ts), max(ts)))
    print('   -> %d 框' % len(b))
    for bb, ss, cc in zip(b, s, c):
        print('      [%4d,%4d,%4d,%4d]  conf %.3f  cls %d'
              % (int(bb[0]), int(bb[1]), int(bb[2]), int(bb[3]), float(ss), int(cc)))
rk.release()
