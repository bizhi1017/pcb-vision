# -*- coding: utf-8 -*-
"""逐组、逐类对比：GT 框数 vs 检出框数（按类别分开）

用法（WSL2）：
    ~/train-venv/bin/python ai/train/eval_groups_cls.py [weights.pt] [组1,组2,...]
"""
import collections
from pathlib import Path
import glob
import io
import os
import sys

import cv2
import numpy as np
import torch

sys.path.insert(0, os.environ.get('YOLOV5_DIR', str(Path.home()/'y5v7')))
from models.common import DetectMultiBackend
from utils.augmentations import letterbox as y_letterbox
from utils.general import non_max_suppression

REPO = str(Path(__file__).resolve().parents[2])
NAMES = ['missing', 'stain', 'scratch']
PT = (sys.argv[1] if len(sys.argv) > 1
      else os.path.join(REPO, 'ai/train/runs/p04_3class/weights/best.pt'))
GROUPS = (sys.argv[2].split(',') if len(sys.argv) > 2
          else ['photo01', 'photo02', 'photo03', 'photo04', 'photo05',
                'photo05b', 'photo07', 'photo08', 'photo08v', 'photo09'])
CONF = float(sys.argv[3]) if len(sys.argv) > 3 else 0.35

m = DetectMultiBackend(PT, device=torch.device('cpu'), fuse=True)
print('权重:', PT)
print('%-10s %-4s %s' % ('组', '张数', '  每类: GT -> 检出（差）'))
for g in GROUPS:
    base = os.path.join(REPO, 'ai/dataset/norm', g)
    imgs = sorted(glob.glob(os.path.join(base, '*.jpg')))
    if not imgs:
        continue
    gt = collections.Counter()
    for im in imgs:
        lp = os.path.join(base, 'labels',
                          os.path.splitext(os.path.basename(im))[0] + '.txt')
        if os.path.isfile(lp):
            for line in io.open(lp, encoding='utf-8'):
                p = line.split()
                if len(p) == 5:
                    gt[int(p[0])] += 1
    det = collections.Counter()
    for im in imgs:
        img0 = cv2.imread(im)
        lb, ratio_t, _pad = y_letterbox(img0, 640, stride=32, auto=False)
        rgb = np.ascontiguousarray(lb[:, :, ::-1])
        x = torch.from_numpy(rgb.transpose(2, 0, 1)[None].copy()).float() / 255.0
        pred = m(x)[0]
        d = non_max_suppression(pred, CONF, 0.45, max_det=50)[0]
        if d is not None and len(d):
            for row in d.numpy():
                det[int(row[5])] += 1
    parts = []
    for k in range(3):
        d = gt[k], det[k]
        if d[0] or d[1]:
            parts.append('%s %3d->%3d(%+d)' % (NAMES[k], d[0], d[1], d[1] - d[0]))
    print('%-10s %-4d %s' % (g, len(imgs), '  '.join(parts)))
print('\n判读：检出 - GT 明显为负 = 漏检；为正 = 误检（阈值 %.2f）' % CONF)
