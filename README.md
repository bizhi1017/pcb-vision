# PCB VISION · 双路PCB检测与图像压缩

基于安路PH1A90 FPGA与RK3576的双IMX415图像采集、预处理、源端压缩、三类PCB缺陷检测和板框初测系统。当前整理基线为P9 BUILD `0x5630503B`、板端3.13.0-p9、Qt ui40-p9。

```text
IMX415 A/B → FPGA采集/预处理/源压缩 → PCIe → RK重建/推理/网络编码
                                             ├→ Qt上位机
                                             ├→ HDMI
                                             └→ 手机浏览器 /m
```

支持A/B独立预览、缺件/污渍/划痕检测框、告警、缺陷放大、HDMI路由、手机通道选择及远程控制、按日和通道归档截图、棋盘格平面标定后板框初测。采集档位为双1080P或单4K，实际吞吐取决于编码/检测/显示工况。

**两个压缩环节不同**：FPGA端是LZ77无损与RAW6/GRAY5有损位打包；RK到Qt/手机的JPEG有损/PNG或PCZ1完整无损属于网络编码。项目不将RK硬件JPEG称为FPGA JPEG。网络无损不能恢复源端有损已丢掉的低位。

| 目录 | 内容 |
|---|---|
| rk3576_p9/ | 板端服务、解码、推理调度、网络、HDMI、测量和存储 |
| pc_qt_p9/ | 当前Qt ui40及Zstd解码依赖 |
| fpga/p9_compact/uisrc/01_rtl/ | 自研预处理和完整帧编码RTL |
| ai/ | 类别表、训练/评估和ONNX→RKNN转换脚本 |
| test/、tools/ | 可在CPU运行的检查和编码RTL仿真入口 |
| verification/ | 已有P9离线仿真、资源与时序证据 |
| docs/ | 架构、使用、测试、指标范围和GitHub上传说明 |

公开树不包含厂商FPGA IP、模型权重、个人数据集或板端预编译库，因此不是开箱即烧写的完整厂商工程。需要硬件复现时先取得对应依赖；完整原项目材料在单独的比赛提交包中，不能直接把比赛包当MIT仓库公开。

Windows/Python 3.11示例：

```sh
python -m pip install -r requirements-pc.txt
python tools/run_offline_checks.py
python pc_qt_p9/dual_station.py --base http://192.168.10.50:8001
```

MIT适用范围与第三方说明见LICENSE_SCOPE.md、THIRD_PARTY_NOTICES.md。使用与构建见docs/BUILD_AND_RUN.md；已测/待测指标见docs/RELEASE_STATUS.md。
