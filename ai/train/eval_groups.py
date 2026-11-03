# -*- coding: utf-8 -*-
"""逐组评估：每张图检出几个框（多阈值），跟该组的 GT 框数对齐 —— 看清误检/漏检在哪一组

用法（WSL2）：
    ~/train-venv/bin/python ai/train/eval_groups.py [weights.pt]
默认用 ai/train/runs/p03_110img/weights/best.pt。

判读：某组「平均检出/张」明显高于该组 GT/张 就是误检（负样本组 GT=0，检出应接近 0）。
这张表是**分组**看的，比单个 mAP 更能指出"问题出在哪一类画面"——
实测就是靠它发现"远景全装好（photo05）误检 1.33 个/张"的（见 test/d33）。
"""
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
PT = (sys.argv[1] if len(sys.argv) > 1
      else os.path.join(REPO, 'ai/train/runs/p03_110img/weights/best.pt'))
GROUPS = ('photo01', 'photo02', 'photo03', 'photo04', 'photo05')
THRESHES = (0.25, 0.35, 0.50)

m = DetectMultiBackend(PT, device=torch.device('cpu'), fuse=True)
print('%-9s %-4s %-5s %s' % ('组', 'GT框', '张数', '各阈值下 平均检出/张'))
for g in GROUPS:
    base = os.path.join(REPO, 'ai/dataset/norm', g)
    imgs = sorted(glob.glob(os.path.join(base, '*.jpg')))
    gt = 0
    for im in imgs:
        lp = os.path.join(base, 'labels',
                          os.path.splitext(os.path.basename(im))[0] + '.txt')
        if os.path.isfile(lp):
            gt += sum(1 for line in io.open(lp, encoding='utf-8') if line.strip())
    per = {t: [] for t in THRESHES}
    for im in imgs:
        img0 = cv2.imread(im)
        lb, ratio_t, (dw, dh) = y_letterbox(img0, 640, stride=32, auto=False)
        rgb = np.ascontiguousarray(lb[:, :, ::-1])
        x = torch.from_numpy(rgb.transpose(2, 0, 1)[None].copy()).float() / 255.0
        pred = m(x)[0]
        for t in THRESHES:
            det = non_max_suppression(pred, t, 0.45, max_det=50)[0]
            per[t].append(0 if det is None else len(det))
    line = '  '.join('%.2f:%4.2f' % (t, float(np.mean(per[t]))) for t in THRESHES)
    print('%-9s %-4d %-5d %s' % (g, gt, len(imgs), line))
print('\n（GT框 = 标注给的真值框数；平均检出持续高于 GT/张 说明有误检）')
