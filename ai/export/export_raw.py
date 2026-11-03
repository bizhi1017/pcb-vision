#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_raw.py —— 把 yolov5 权重导出成 **rknn_model_zoo 风格的 3 张原始特征图**
（sigmoid 之前、解码之前），专治 INT8 量化把分数压平成 0

为什么不用 yolov5 自带的 export.py（2026-10-24 实测）：
    v7.0 的 export.py 输出**单张量** `(1, 25200, 5+nc)`，里面**混了两种量级**：
        [:, 0:4] = cx,cy,w,h   0~640（像素）
        [:, 4:]  = obj + 类分数 0~0.05（已过 sigmoid）
    INT8 输出按整张量一个 scale 量化 -> scale≈5 -> 分数全被压成 0，板上 0 框。
    试过"拆成两个输出各自量化"也不行（见 ai/export/split_output.py 头注释与 test/d32 §8.3.1）：
    **根因是 sigmoid 之后的数值太小**，拆几块都一样。

正确做法（本脚本）：让 Detect 头回到"**训练态输出**"——
    `model.model[-1].export = True` 会让 Detect.forward 里的 `self.training |= self.export` 生效，
    于是它**跳过 sigmoid + 完全不做 decoder**，直接吐 3 张原始特征图：
        (1, 3*(5+nc), 80, 80) / (1, 3*(5+nc), 40, 40) / (1, 3*(5+nc), 20, 20)
    这些通道的量级都差不多（都是 BN 之后的卷积输出，典型 -10~+10），INT8 量化不丢精度。
    sigmoid / anchor 解码 / grid 偏移全部搬到**板卡上**做（`postprocess_yolov5_raw`）。

用法（WSL2，需要 torch + yolov5 仓库）：
    ~/train-venv/bin/python ai/export/export_raw.py \\
        --weights ai/train/runs/p01_baseline/weights/best.pt \\
        --out ai/export/runs/p01_baseline/p01_baseline_raw.onnx --imgsz 640

产出：ONNX 三个输出 `f0/f1/f2`（80/40/20 三档），可以直接喂 ai/export/onnx2rknn.py。
"""

from __future__ import print_function

import argparse
import os
import sys

DEFAULT_Y5 = os.path.expanduser('~/y5v7')


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='导出 yolov5 的 3 张原始特征图（sigmoid 前），给 INT8 量化用')
    ap.add_argument('--weights', required=True, help='best.pt 路径（WSL 里的路径）')
    ap.add_argument('--out', required=True, help='输出 ONNX 路径')
    ap.add_argument('--imgsz', type=int, default=640)
    ap.add_argument('--opset', type=int, default=12)
    ap.add_argument('--y5', default=DEFAULT_Y5, help='yolov5 仓库路径（默认 ~/y5v7）')
    a = ap.parse_args(argv)

    if not os.path.isdir(a.y5):
        raise SystemExit('[ERR] 找不到 yolov5 仓库: %s（用 --y5 指定）' % a.y5)
    sys.path.insert(0, a.y5)

    import torch
    from models.yolo import Detect          # noqa: F401  （确认仓库结构对）

    print('[1/3] 载入权重 %s' % a.weights)
    ckpt = torch.load(a.weights, map_location='cpu')
    model = (ckpt.get('ema') or ckpt['model']).float()
    model = model.fuse().eval()
    det = model.model[-1]
    nc = int(getattr(det, 'nc', 0))
    na = int(getattr(det, 'na', 3))
    print('      nc=%d, anchors=%d 组' % (nc, na))

    # ★关键：让 Detect 头**永远**返回 3 张原始卷积输出。
    #   坑：不能只靠 `det.training = True` —— `torch.onnx.export` 默认会
    #   `select_model_mode_for_export(EVAL)` 把整个模型强制置成 eval（连 Detect 一起），
    #   于是又走了 sigmoid+解码那条路（踩过：报 "not enough values to unpack (expected 5, got 3)"）。
    #   所以直接换掉 Detect.forward —— 与 training 标志无关，稳。
    def _raw_detect_forward(self, x):
        for i in range(self.nl):
            x[i] = self.m[i](x[i])          # (bs, na*no, ny, nx)
        return x                            # 原样返回，不做 sigmoid / 解码

    import types
    det.forward = types.MethodType(_raw_detect_forward, det)

    class RawExport(torch.nn.Module):
        """把 Detect 的 (bs,na,ny,nx,no) 整理成 (bs, na*no, ny, nx)，与官方一致"""

        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, x):
            outs = self.m(x)                     # list: [(bs,na*no,ny,nx) x3]
            res = []
            for o in outs:
                if o.dim() == 5:                 # (bs,na,ny,nx,no) -> (bs,na*no,ny,nx)
                    bs, _na, ny, nx, no = o.shape
                    o = o.permute(0, 1, 4, 2, 3).reshape(bs, _na * no, ny, nx)
                res.append(o)
            return tuple(res)

    export_model = RawExport(model).eval()
    # ⚠️ .eval() 会递归把 Detect 也置回 eval —— 必须在 .eval() **之后**再置一次
    det.training = True
    dummy = torch.zeros(1, 3, a.imgsz, a.imgsz)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or '.', exist_ok=True)
    print('[2/3] 导出 ONNX（opset %d, %dx%d）' % (a.opset, a.imgsz, a.imgsz))
    torch.onnx.export(
        export_model, dummy, a.out, verbose=False, opset_version=a.opset,
        input_names=['images'], output_names=['f0', 'f1', 'f2'],
        dynamic_axes=None)

    print('[3/3] 校验形状')
    import onnx
    m = onnx.load(a.out)
    ok = True
    want_ch = 3 * (5 + nc)
    shapes = []
    for o in m.graph.output:
        d = [x.dim_value for x in o.type.tensor_type.shape.dim]
        shapes.append((o.name, d))
        print('      %-4s %s' % (o.name, d))
    strides = sorted(set(d[2] for _n, d in shapes), reverse=True)
    ok = (len(shapes) == 3
          and all(d[1] == want_ch for _n, d in shapes)
          and strides == [a.imgsz // 8, a.imgsz // 16, a.imgsz // 32])
    print('      %s（期望：3 个输出、通道 %d、网格 %d/%d/%d）'
          % ('OK' if ok else 'CHECK', want_ch, a.imgsz // 8, a.imgsz // 16,
             a.imgsz // 32))
    print('[OK] %s -> %s (%.1f MB)'
          % (a.weights, a.out, os.path.getsize(a.out) / 1e6))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
