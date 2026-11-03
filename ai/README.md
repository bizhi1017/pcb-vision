# 模型训练与转换

类别固定为missing、stain、scratch（缺件/污渍/划痕），640×640模型输入。
本仓库包含训练、评估与raw-logit导出脚本，不包含YOLOv5上游仓库、预训练权重、个人图像或训练产物。
原项目使用YOLOv5 v7训练，RKNN Toolkit2 2.3.0转换为RK3576 raw INT8三输出模型。运行端需要相配的Toolkit Lite2和驱动。
公开依赖说明见THIRD_PARTY_NOTICES.md。比赛副本提供当前模型、best.pt、raw ONNX、149训练/26验证样本及类别表。
训练数据YAML使用相对路径，运行训练时先从自己的YOLOv5环境指定配置；训练脚本的YOLOV5_DIR、TRAIN_PYTHON、PRETRAINED_WEIGHTS可覆盖。

```sh
python ai/export/export_raw.py --help
python ai/export/onnx2rknn.py --help
```

97.7% mAP50来自p07_near校正后26张/73框的CPU验证，不代表P9整机或RKNN量化后的准确率；本次没有重训或重新评测。
