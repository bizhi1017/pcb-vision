#!/bin/bash
# ============================================================================
# train_baseline.sh —— 在 WSL2 里训一版 PCB 缺件检测基线（yolov5s / 640）
#
# 数据来自 Windows 侧仓库：ai/dataset/pcb_v1（tools/make_yolo_dataset.py 生成）
# 训练产物写到              ai/train/runs/<RUN_NAME>（已被 .gitignore 忽略）
#
# 用法：
#   bash ai/train/train_baseline.sh                  # 默认 p01_baseline、200 epochs
#   RUN_NAME=p02_baseline EPOCHS=300 bash ai/train/train_baseline.sh
#
# 坑记录（2026-10-24 真跑）：**别把变量叫 NAME** —— Windows 侧的
#   NAME=YOUR_COMPUTER_NAME 会被 WSL 继承进来，${NAME:-默认值} 拿到的是主机名，
#   yolov5 的 run 目录就变成 runs/YOUR_COMPUTER_NAME 了。
# ============================================================================
set -euo pipefail

REPO_WSL="$(cd "$(dirname "$0")/../.." && pwd)"
DATA_DIR=$REPO_WSL/ai/dataset/pcb_v1                # make_yolo_dataset 的产物
RUNS=$REPO_WSL/ai/train/runs
Y5=${YOLOV5_DIR:-$HOME/y5v7}
PY=${TRAIN_PYTHON:-$HOME/train-venv/bin/python}
WEIGHTS=${PRETRAINED_WEIGHTS:-$HOME/yolov5s.pt}
RUN_NAME=${RUN_NAME:-p01_baseline}
EPOCHS=${EPOCHS:-200}
BATCH=${BATCH:-8}
IMG=${IMG:-640}

[ -d "$DATA_DIR/images/train" ] || { echo "找不到数据集 $DATA_DIR（先跑 tools/make_yolo_dataset.py）" >&2; exit 2; }
[ -f "$WEIGHTS" ] || { echo "找不到预训练权重 $WEIGHTS" >&2; exit 2; }

# make_yolo_dataset 写的是 Windows 路径，WSL 里用不了 —— 这里现写一份 /mnt 版的。
# ★ nc / names 必须**从 classes.txt 现读**，不能写死 nc:1 ——
#   写死过一次：加类后训练一启动就 `AssertionError: Label class 2 exceeds nc=1`。
CLASSES=$REPO_WSL/ai/dataset/classes.txt
[ -f "$CLASSES" ] || { echo "找不到类别表 $CLASSES" >&2; exit 2; }
mapfile -t NAMES < <(grep -v '^[[:space:]]*#' "$CLASSES" | grep -v '^[[:space:]]*$' \
                     | tr -d '\r' | sed 's/[[:space:]]*$//')
NC=${#NAMES[@]}
[ "$NC" -gt 0 ] || { echo "classes.txt 里没有类名" >&2; exit 2; }
NAMES_YAML='['
for n in "${NAMES[@]}"; do NAMES_YAML="$NAMES_YAML'$n', "; done
NAMES_YAML="${NAMES_YAML%, }]"
echo "== 类别: nc=$NC  $NAMES_YAML"

YAML=$Y5/data/pcb_wsl.yaml
mkdir -p "$(dirname "$YAML")"
cat > "$YAML" <<EOF
# 由 ai/train/train_baseline.sh 自动生成（nc/names 来自 ai/dataset/classes.txt，勿手改）
path: $DATA_DIR
train: images/train
val: images/val
nc: $NC
names: $NAMES_YAML
EOF
echo "== data.yaml =="; cat "$YAML"

mkdir -p "$RUNS"
echo "== 开始训练 $RUN_NAME: ${EPOCHS} epochs, img $IMG, batch $BATCH =="
cd "$Y5"
exec "$PY" train.py \
  --img "$IMG" --batch "$BATCH" --epochs "$EPOCHS" \
  --data "$YAML" --weights "$WEIGHTS" \
  --project "$RUNS" --name "$RUN_NAME" --exist-ok \
  --device 0 --workers 4 --seed 0 --patience 150 \
  --label-smoothing 0.0 --cos-lr
