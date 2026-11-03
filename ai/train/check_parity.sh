#!/bin/bash
# ============================================================================
# check_parity.sh —— 验证"板上解码器"和"训练框架的解码"是同一个东西
#
# 为什么需要：板卡端 rk_pcb_view.py 自己解码模型输出（conf=obj*cls -> NMS ->
#   去 letterbox）。这套代码一旦和训练框架的後处理不一致，板上就会出现
#   "框整体偏移 / 分数偏低 / 少框" 这种很难查的现象 —— 而且看起来像模型没训好。
#   本脚本拿**同一份权重**两条路各跑一遍，逐框对比：
#       参考 = PyTorch + yolov5 官方 non_max_suppression
#       被测 = ONNX + rk3576/rk_pcb_view.py 里的 postprocess_auto
#
# 用法（WSL2 里跑）：
#     bash ai/train/check_parity.sh                       # 用 run 里最新 best.pt
#     bash ai/train/check_parity.sh /path/to/best.pt
#
# 判定：框坐标差 <= 2 px、分数差 <= 0.02、类别相同、框数相同 -> PASS
# 坑：yolov5 v7.0 的 letterbox 返回的 ratio 是 **元组 (r, r)**；喂模型前必须
#     BGR->RGB（两边都要，否则坐标能差 10 px，看着像"转换坏了"）。
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
RUN=$REPO/ai/train/runs
Y5=${YOLOV5_DIR:-$HOME/y5v7}
PY=${TRAIN_PYTHON:-$HOME/train-venv/bin/python}
OUT=$REPO/ai/export/parity
W_PT=${1:-$(ls -t "$RUN"/*/weights/best.pt 2>/dev/null | head -1)}

[ -f "$W_PT" ] || { echo "找不到权重: $W_PT" >&2; exit 2; }
mkdir -p "$OUT"
echo "== 参战权重: $W_PT =="

# 冻结一份再导出，保证两条路用的是**同一份**参数
#   注意：训练还在跑的时候 best.pt 可能正写到一半 -> 复制出来可能是坏文件。
#   所以直接拿 export.py 当"体检"（它能处理 yolov5 的自定义类），失败就重试。
#   （别用裸 torch.load 验证：yolov5 的 ckpt 里 pickle 了 models.yolo，离开仓库 cwd 会
#    报 ModuleNotFoundError: No module named 'models'，那是假故障，踩过一次。）
cd "$Y5"
ok_export=0
for i in 1 2 3; do
  cp "$W_PT" "$OUT/parity.pt"
  if "$PY" export.py --weights "$OUT/parity.pt" --include onnx --opset 12 \
        --imgsz 640 640 >/tmp/export_parity.log 2>&1; then
    ok_export=1
    break
  fi
  echo "[warn] 导出失败（第 $i 次，训练可能正在写权重），2s 后重试"
  sleep 2
done
[ "$ok_export" = 1 ] || { echo "onnx 导出失败，最后 5 行日志：" >&2
  tail -5 /tmp/export_parity.log >&2; exit 3; }

PARITY_PT="$OUT/parity.pt" PARITY_ONNX="$OUT/parity.onnx" REPO="$REPO" "$PY" - <<'PY'
import os, sys, glob
import numpy as np
import cv2
import torch
import onnxruntime as ort

REPO = os.environ['REPO']
sys.path.insert(0, os.path.join(REPO, 'rk3576_p9'))
import rk_pcb_view as R                      # 板上那份解码器
from models.common import DetectMultiBackend
from utils.augmentations import letterbox as y_letterbox
from utils.general import non_max_suppression

PT, ONNX = os.environ['PARITY_PT'], os.environ['PARITY_ONNX']
imgs = sorted(glob.glob(os.path.join(REPO, 'ai/dataset/pcb_v1/images/val/*.jpg')))
if not imgs:
    print('没有 val 图（先跑 tools/make_yolo_dataset.py）')
    sys.exit(2)

m = DetectMultiBackend(PT, device=torch.device('cpu'), fuse=True)
sess = ort.InferenceSession(ONNX, providers=['CPUExecutionProvider'])
fmt = '   %-5s x1=%7.1f y1=%7.1f x2=%7.1f y2=%7.1f conf=%.3f cls=%d'

ok_all, n_cmp = True, 0
for p in imgs:
    img0 = cv2.imread(p)
    H0, W0 = img0.shape[:2]
    lb, ratio_t, (dw, dh) = y_letterbox(img0, 640, stride=32, auto=False)
    r = float(ratio_t[0])                    # v7.0 的 ratio 是 (r, r) 元组
    rgb = np.ascontiguousarray(lb[:, :, ::-1])       # cv2 读的是 BGR，模型吃 RGB
    t = torch.from_numpy(rgb.transpose(2, 0, 1)[None].copy()).float() / 255.0
    det = non_max_suppression(m(t)[0], 0.35, 0.45, max_det=50)[0]
    ref = []
    if det is not None and len(det):
        for row in det.numpy():
            x1, y1, x2, y2, cf, cl = [float(v) for v in row[:6]]
            ref.append(((x1 - dw) / r, (y1 - dh) / r,
                        (x2 - dw) / r, (y2 - dh) / r, cf, int(cl)))
    x = rgb.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    out = sess.run(None, {sess.get_inputs()[0].name: x})[0]
    b, s, c = R.postprocess_auto([out], r, dw, dh, W0, H0)
    mine = sorted([tuple([float(v) for v in bb]) + (float(ss), int(cc))
                   for bb, ss, cc in zip(b, s, c)], key=lambda z: -z[4])
    ref = sorted(ref, key=lambda z: -z[4])
    print('\n%s  参考 %d 框 / 被测 %d 框' % (os.path.basename(p), len(ref), len(mine)))
    for z in ref[:3]:
        print(fmt % (('ref',) + z))
    for z in mine[:3]:
        print(fmt % (('mine',) + z))
    if len(ref) != len(mine):
        ok_all = False
        continue
    for a, bb in zip(ref, mine):
        n_cmp += 1
        if (max(abs(a[k] - bb[k]) for k in range(4)) > 2.0
                or abs(a[4] - bb[4]) > 0.02 or a[5] != bb[5]):
            ok_all = False
            print('   [DIFF] ref=%s mine=%s'
                  % ([round(v, 1) for v in a], [round(v, 1) for v in bb]))
print('\n比较了 %d 个框; 一致性: %s' % (n_cmp, 'PASS' if ok_all else 'FAIL'))
sys.exit(0 if ok_all else 1)
PY
