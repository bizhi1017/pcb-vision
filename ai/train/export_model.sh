#!/bin/bash
# ============================================================================
# export_model.sh —— 训练好的 best.pt → ONNX → RKNN（FP16 + INT8），一条命令走完
#
# 用法（WSL2 里跑）：
#     bash ai/train/export_model.sh                      # 取 runs 里最新 best.pt
#     bash ai/train/export_model.sh /path/best.pt p01_baseline
#
# 产物：ai/export/runs/<NAME>/
#     <NAME>.pt           冻结的权重（fp32）
#     <NAME>.onnx         (1,3,640,640) -> (1,25200,5+nc)
#     <NAME>_fp16.rknn    无量化，板卡首选用这个（链路最简单）
#     <NAME>_int8.rknn    INT8 量化（用 images/train 做校准集），要验掉点再用
#     classes.txt         一起拷到板卡，脚本按行号=类别 id 读
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
RUN=$REPO/ai/train/runs
Y5=${YOLOV5_DIR:-$HOME/y5v7}
PY=${TRAIN_PYTHON:-$HOME/train-venv/bin/python}
RKNYPY=${RKNN_PYTHON:-$HOME/rknn-venv/bin/python}
W_PT=${1:-$(ls -t "$RUN"/*/weights/best.pt 2>/dev/null | head -1)}
NAME=${2:-$(basename "$(dirname "$(dirname "$W_PT")")")}
OUT=$REPO/ai/export/runs/$NAME
CALIB=$REPO/ai/dataset/pcb_v1/images/train

[ -f "$W_PT" ] || { echo "找不到权重: $W_PT" >&2; exit 2; }
mkdir -p "$OUT"
echo "== 权重: $W_PT"
echo "== 名称: $NAME"
echo "== 产物: $OUT"

cp "$W_PT" "$OUT/$NAME.pt"
cp "$REPO/ai/dataset/classes.txt" "$OUT/classes.txt"

echo
echo "== [1/3] pt -> onnx（opset 12, 640x640）"
cd "$Y5"
"$PY" export.py --weights "$OUT/$NAME.pt" --include onnx --opset 12 --imgsz 640 640 2>&1 \
  | grep -E "PyTorch:|ONNX:|export success|Export complete|Error" | tail -6
[ -f "$OUT/$NAME.onnx" ] || { echo "ONNX 没生成" >&2; exit 3; }

echo
echo "== [2/3] onnx -> rknn (FP16)"
cd "$REPO"
"$RKNYPY" ai/export/onnx2rknn.py --onnx "$OUT/$NAME.onnx" \
    --out "$OUT/${NAME}_fp16.rknn" --classes "$REPO/ai/dataset/classes.txt" \
    --no-quant --target rk3576 2>&1 | grep -E "\[RESULT\]|\[OK\]|\[ERR\]|Traceback" | tail -6

echo
echo "== [3/3] onnx -> rknn (INT8, calib=images/train)"
CAL_N=$(ls "$CALIB" 2>/dev/null | wc -l)
if [ "$CAL_N" -gt 0 ]; then
  "$RKNYPY" ai/export/onnx2rknn.py --onnx "$OUT/$NAME.onnx" \
      --out "$OUT/${NAME}_int8.rknn" --classes "$REPO/ai/dataset/classes.txt" \
      --calib "$CALIB" --calib-num "$CAL_N" --quant --target rk3576 2>&1 \
      | grep -E "\[RESULT\]|\[OK\]|\[ERR\]|Traceback" | tail -6
else
  echo "[warn] 没有校准集 $CALIB，跳过 INT8"
fi

echo
echo "== 产物清单 =="
ls -l "$OUT"

# ---------------------------------------------------------------------------
# ★ INT8 专用导出：sigmoid **之前**的 3 张原始特征图
#   v7.0 的单输出把「坐标 0~640」和「已 sigmoid 的 0~0.05 分数」混在一个张量里，
#   INT8 按整张量量化会**把分数全压成 0（板上一个框都不出）**。详见 test/d32 §8.3。
#   注意：上面那份单输出模型不要拿去做 INT8，只做 FP16。
# ---------------------------------------------------------------------------
echo
echo "== [附加] raw（3 张 logits 特征图）→ FP16 / INT8 =="
if "$PY" ai/export/export_raw.py --weights "$OUT/$NAME.pt" \
        --out "$OUT/${NAME}_raw.onnx" 2>&1 | grep -E "^\[|^      " | tail -6; then
  "$RKNYPY" ai/export/onnx2rknn.py --onnx "$OUT/${NAME}_raw.onnx" \
      --out "$OUT/${NAME}_raw_fp16.rknn" --classes "$REPO/ai/dataset/classes.txt" \
      --no-quant --target rk3576 2>&1 | grep -E "\[RESULT\]|\[ERR\]" | tail -3
  if [ "${CAL_N:-0}" -gt 0 ]; then
    "$RKNYPY" ai/export/onnx2rknn.py --onnx "$OUT/${NAME}_raw.onnx" \
        --out "$OUT/${NAME}_raw_int8.rknn" --classes "$REPO/ai/dataset/classes.txt" \
        --calib "$CALIB" --calib-num "$CAL_N" --quant --target rk3576 2>&1 \
        | grep -E "\[RESULT\]|\[ERR\]" | tail -3
  else
    echo "[warn] 没有校准集，跳过 raw INT8"
  fi
else
  echo "[warn] raw 导出失败（不影响上面已生成的单输出模型）"
fi
echo
echo "== 最终产物 =="
ls -l "$OUT"
