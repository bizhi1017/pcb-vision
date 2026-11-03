#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量一下 RK3576 CPU 上"尺寸测量"那几个 OpenCV 步骤的真实耗时（1080p）

用法: python3 /home/rock/yolo/size_bench.py [图片]
默认自己造一张 1920x1080 合成图，不依赖数据集。
"""
import sys
import time

import cv2
import numpy as np

img_path = sys.argv[1] if len(sys.argv) > 1 else None
if img_path:
    frame = cv2.imread(img_path)
    frame = cv2.resize(frame, (1920, 1080))
else:
    frame = np.full((1080, 1920, 3), 60, np.uint8)
    cv2.rectangle(frame, (300, 200), (1600, 900), (30, 30, 30), -1)
    for k in range(6):
        cv2.rectangle(frame, (400 + k * 180, 400), (520 + k * 180, 480),
                      (200, 200, 200), -1)

print('图 %dx%d' % (frame.shape[1], frame.shape[0]))

# 模拟一个单应矩阵（板面 -> 正视图）
src = np.float32([[300, 200], [1600, 200], [1600, 900], [300, 900]])
dst = np.float32([[0, 0], [1000, 0], [1000, 600], [0, 600]])
H = cv2.getPerspectiveTransform(src, dst)


def bench(name, fn, n=20):
    fn()                            # 预热
    t0 = time.time()
    for _ in range(n):
        out = fn()
    dt = (time.time() - t0) / n * 1e3
    print('  %-28s %6.2f ms' % (name, dt))
    return out


gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
print('== 全图 1920x1080 ==')
bench('cvtColor BGR2GRAY', lambda: cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
bench('warpPerspective -> 1000x600',
      lambda: cv2.warpPerspective(gray, H, (1000, 600)))
bench('Canny + findContours', lambda: cv2.findContours(
    cv2.Canny(gray, 50, 150), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE))
bench('整图 GaussianBlur(3x3)', lambda: cv2.GaussianBlur(gray, (3, 3), 0))

print('== ROI 局部（200x150，模拟"在每个 YOLO 框里量尺寸"）==')
roi = gray[400:550, 400:600]
bench('ROI Canny + findContours + minAreaRect', lambda: [
    cv2.minAreaRect(c) for c in cv2.findContours(
        cv2.Canny(roi, 50, 150), cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE)[0]][:20])
bench('ROI 尺寸换算(纯 numpy)', lambda: (np.array([12.3, 45.6]) * 0.1).tolist())
print('\n注：以上是 CPU 上 OpenCV 的实测；这些步骤与 NPU 推理并行不冲突。')
