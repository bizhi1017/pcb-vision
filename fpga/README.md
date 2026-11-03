# FPGA工程范围

`p9_compact/uisrc/01_rtl/pre`为自研灰度、高斯、Sobel、直方图均衡等流水线；`codec/frame_lz77.v`实现完整帧封装、LZ77无损与RAW6/GRAY5有损路径。
`.al`是P9工程入口元数据。公开版没有配齐厂商工程；缺失项逐项记录在external_files.json，不能把公开版称为可单独完成整机bitgen。
已有合法厂商集成工程的用户，使用完整提交副本或按同路径补齐依赖。不要随意用不同版本IP替换，恢复后应先核对SHA再构建。独立编码RTL仿真不需要这些IP。
