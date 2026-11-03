# 测试与测量

python tools/run_offline_checks.py执行CPU生成数据协议往返、所有Python语法、模块本地依赖、P9 UI40离屏检查、隔离安装测试及只用板端模块的测量HTTP回归，不连接板卡、不加载相机/NPU。离屏测试的localhost模拟HTTP与实际网络控制不同。

RTL小帧/完整帧：安装Icarus Verilog，python tools/check_p9_codec.py（或--full）；随后python tools/check_p9_transitions.py。工具可用IVERILOG/VVP环境变量指定。完整帧仿真需要较长时间，生成文件位于test/inspection，不纳入Git。
native C测试需要先在Linux/WSL编译x86测试库，再执行test/test_p9_native.py；不得把ARM64.so作为x86测试库。历史证据verification/p9供复核，不代表本次重新实测硬件。

标定入口使用6×6格纸（5×5内角点）、横纵连续5格各47.0mm，即单格9.4mm；PCB厚度历史1.2mm。每路独立保存平面映射，保持机位/焦距/顶面高度。不附原机位个人标定文件，现场需按实际设置。
不要使用旧单路标定替代两路标定；当前测量修复包含board_outline.py，测试明确只从板端路径加载。
