# 构建与使用

## Qt

Python 3.11，按requirements-pc.txt安装依赖。双击pc_qt_p9/start_dual_station.bat，或传--base http://BOARD_IP:8001。默认192.168.10.50只是开发网段示例，可自行覆盖。只打开一个操控窗口。

## RK

原验证平台为ARM64 RK3576、Linux 6.1.99，Toolkit Lite2 2.3.0。安装板卡镜像匹配的cv2/Qt与RKNN wheel；编译依赖包括C编译器、pkg-config、libpng、zlib、libdeflate、zstd、EGL/GLES、FreeType，以及Rockchip MPP/RGA开发文件。
在rk3576_p9依次运行build_fpga_codec.sh、build_hq_native.sh、build_inference_cpu.sh、build_fast_png.sh、build_fast_lossless.sh；需要硬件JPEG/HDMI时另编build_mpp_jpeg.sh、build_hdmi_egl.sh、build_hdmi_text.sh。脚本只编译，实际相机/驱动/HDMI启动需用户另行操作。
运行模型用PCB_MODEL指定p07_near_raw_int8.rknn。相机服务支持--calibration-dir；实际完整部署还需匹配的PCIe模块及厂商集成工程。比赛包提供原预编译.so/.ko、位流及明确手动上电流程。

## FPGA

公开版可独立仿真frame_lz77.v；完整综合依赖fpga/external_files.json列出的合法厂商集成文件。原TD版本6.2.200067（2026.1 SP2），器件PH1A90SEG324-2。完整比赛副本提供.al和所有引用，不含_Runs数据库。

## 无线

只使用有线Qt无需Wi-Fi。实际网卡驱动安装、热点连接由用户在板端执行；nmcli --ask输入密码。手机打开http://板卡无线IP:8001/m或扫描Qt二维码。当前方案是同热点/局域网，未实现公网云服务与认证。
