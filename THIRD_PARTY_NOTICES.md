# 第三方组件与来源

| 组件 | 本包处理 | 说明 |
|---|---|---|
| Zstandard 1.5.7 | Windows解码DLL与原BSD许可保留 | pc_qt_p9/native/LICENSE.zstd.txt、zstd_provenance.json |
| Anlogic / MiLianKe FPGA采集、MIPI/PCIe/ISP IP | 未放公开树 | 原文件存在All Rights Reserved/厂商版权；外部文件清单含路径及SHA，不能由本项目MIT覆盖 |
| Anlogic PCIe Linux驱动 | 比赛包附源码和编译模块；公开树不含 | 源文件声明GPL version 2，比赛包保留原声明及COPYING；模块只适配原内核 |
| TP-Link/AIC USB Wi-Fi驱动和固件 | 比赛包提供原发布压缩包 | 厂商许可与固件权限未在本次整理中确认，未纳入MIT公开树 |
| YOLOv5 v7训练代码/预训练权重 | 外部依赖，不复刻上游仓库 | 自研数据准备/导出脚本保留；模型、训练图像不自动授予MIT，比赛包仅供原项目验收 |
| RKNN Toolkit Lite2、MPP/RGA、PyQt5、OpenCV、libdeflate、libpng | 运行/编译依赖 | 使用各上游发行版及原许可；不包含SDK二进制和板卡镜像 |

公开树可验证算法与UI；独立生成整机位流需要另行取得匹配的厂商工程/IP。比赛包的完整工程仅是原项目提交副本，不表示获得第三方再许可权。
