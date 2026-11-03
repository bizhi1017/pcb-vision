# RK3576 P9板端软件

主入口：dual_camera_server.py；实际硬件启动入口start_capture_modes.sh。
BUILD=0x5630503B、板端3.13.0-p9、配套Qt ui40。示例和离线测试不会加载硬件。
公开版省略.so/.ko，需在ARM64目标环境按docs/BUILD_AND_RUN.md编译原生模块；不建议把x86生成库复制到板端。
模型从PCB_MODEL指定路径读取，标定通过--calibration-dir指定。每路标定与实际相机安装有关，未附个人标定数据。
