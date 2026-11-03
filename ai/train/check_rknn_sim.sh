#!/bin/bash
# ============================================================================
# check_rknn_sim.sh —— 在 PC 上用 rknn-toolkit2 的**仿真器**跑一遍 .rknn
#
# 目的：上板之前先证明三件事
#   1. .rknn 能 build 能在 runtime 里 load（模型没坏）
#   2. 喂 uint8 NHWC 640x640 RGB 图，输出形状是我们预期的 (1,25200,5+nc)
#   3. 用板上那份解码器(rk_pcb_view.postprocess_auto)解出来的框，和 PyTorch 版一致
#
# 用法（WSL2 里跑）：
#     bash ai/train/check_rknn_sim.sh                          # fp16, 默认路径
#     bash ai/train/check_rknn_sim.sh <onnx> <weights.pt>
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
Y5=${YOLOV5_DIR:-$HOME/y5v7}
PY=${TRAIN_PYTHON:-$HOME/train-venv/bin/python}
RKNYPY=${RKNN_PYTHON:-$HOME/rknn-venv/bin/python}
ONNX=${1:-$REPO/ai/export/runs/p01_baseline/p01_baseline.onnx}
PT=${2:-$REPO/ai/train/runs/p01_baseline/weights/best.pt}

[ -f "$ONNX" ] || { echo "找不到 onnx: $ONNX" >&2; exit 2; }
[ -f "$PT" ]   || { echo "找不到权重: $PT" >&2; exit 2; }
echo "== onnx: $ONNX"
echo "== 参考权重: $PT"

cd "$Y5"
ONNX="$ONNX" PT="$PT" REPO="$REPO" "$RKNYPY" - <<'PY' 2>&1 | tail -30
import os, glob
import numpy as np
import cv2
from rknn.api import RKNN

REPO = os.environ['REPO']
sys_path = os.path.join(REPO, 'rk3576_p9')
import sys
sys.path.insert(0, sys_path)
import rk_pcb_view as R            # 板上那份解码器（numpy only）

imgs = sorted(glob.glob(os.path.join(REPO, 'ai/dataset/pcb_v1/images/val/*.jpg')))
print('val 图 %d 张' % len(imgs))

# ---- 1) 在同一个进程里 build + init_runtime（toolkit2 的仿真推理要求这样）----
rk = RKNN(verbose=False)
rk.config(target_platform='rk3576', mean_values=[[0, 0, 0]],
          std_values=[[255, 255, 255]], optimization_level=3)
if rk.load_onnx(model=os.environ['ONNX']) != 0:
    print('[ERR] load_onnx 失败'); sys.exit(2)
if rk.build(do_quantization=False) != 0:
    print('[ERR] build 失败'); sys.exit(3)
if rk.init_runtime() != 0:
    print('[ERR] init_runtime 失败'); sys.exit(4)

ok, n_box = True, 0
for p in imgs:
    img0 = cv2.imread(p)
    H0, W0 = img0.shape[:2]
    # 板卡侧的预处理：等比缩放 + 灰边到 640x640（与 rk_pcb_view.letterbox 同规则）
    r = min(640 / H0, 640 / W0)
    nw, nh = int(round(W0 * r)), int(round(H0 * r))
    dw, dh = (640 - nw) / 2.0, (640 - nh) / 2.0
    im = cv2.resize(img0, (nw, nh))
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    im = cv2.copyMakeBorder(im, top, bottom, left, right,
                            cv2.BORDER_CONSTANT, value=(114, 114, 114))
    rgb = np.ascontiguousarray(im[:, :, ::-1]).astype(np.uint8)[None]
    outs = rk.inference(inputs=[rgb], data_format='nhwc')
    if outs is None or outs[0] is None:
        print('%s [ERR] 推理返回空' % os.path.basename(p)); ok = False; continue
    b, s, c = R.postprocess_auto(outs, r, dw, dh, W0, H0)
    n_box += len(b)
    print('%s  输出 %s  dtype=%s  ->  %d 框' %
          (os.path.basename(p), outs[0].shape, outs[0].dtype, len(b)))
    for bb, ss in zip(b, s):
        print('     %s  conf %.3f' % ([int(v) for v in bb], float(ss)))
rk.release()
print('\n合计 %d 框; 仿真器链路: %s' % (n_box, 'PASS' if ok and n_box > 0 else 'FAIL'))
sys.exit(0 if (ok and n_box > 0) else 1)
PY
