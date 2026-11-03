`timescale 1ns/1ps
/*******************************************************************************
 * 文件名   : pre_adapt.v
 * 所属工程 : p3_link_rk   (D17: JPEG 质量四档手动可调 + 自适应码率)
 * 功能     : JPEG 码率闭环控制器
 *            每个"4 帧"统计窗口结束时比较一次 实际码率 vs 目标码率, 决定 qsel 加/减。
 *
 * ---- 为什么不用乘法器 / DSP ----
 *   预算 = 目标码率(B/s) x 窗口秒数(s)。若先求"窗口秒数"再相乘, 就要一个真乘法器。
 *   这里换成 DDA 式做法: 每来一个 **1ms 时基脉冲** 就把预算累加器加上
 *   "目标码率 / 1000 秒" 的定点值, 于是乘法退化成
 *        [ 常数移位/查表 + 一个加法器 ]
 *   全程只有比较器/加减法器, 不占一个 DSP。
 *
 * ---- 为什么时基要从 25MHz 晶振域取 ----
 *   统计窗口按"4 个视频帧"划分, 但帧率会随 AE/曝光变化, 不能写死 30fps;
 *   而像素时钟 S_hs_rx_clk 是 MIPI 恢复时钟(SDC 里没有 create_clock, 也不该写死)。
 *   所以用确定的 25MHz 系统时钟产生 1ms 时基脉冲(pre_ctrl.O_ms_tick, 展宽 64 拍),
 *   送进像素域后 2FF 同步 + 取上升沿。窗口时长 = "数了几个 1ms",
 *   与帧率、像素时钟频率都无关 —— 没有魔法频率常数。
 *
 * ---- 控制律 (每个窗口结束评估一次) ----
 *   budget = target_KBps x 1024 x 窗口秒数                  (累加器 acc, 1 LSB = 2^-10 字节)
 *   超预算 : win_bytes x 20 > budget x 23  即 码率 > 1.15 x 预算, 且 q>0 -> q-- (画质降档, 码率变小)
 *   低于预算: win_bytes x 20 < budget x 17  即 码率 < 0.85 x 预算, 且 q<3 -> q++ (画质升档, 码率变大)
 *   滞回   : 同一方向 **连续 2 个窗口** 才动作, 动作一次后计数清 0 -> 抖动输入不会频繁换档
 *   手动   : I_auto=0 时 O_qsel 直接跟随 I_qsel_man, 控制器完全不动
 *
 * ---- [D27] 目标码率量程: 1 单位 = 40 KB/s (原来是 5 KB/s) ----
 *   原来的坑: 量程 5/10/20/40/80 KB/s, 而实测 **Q30 就已经 ≈270 KB/s、Q90 ≈480 KB/s**
 *   (Q30 = 7.76 B/块 x 9960 块/窗口 / 0.286 s ≈ 269 KB/s)。也就是说量程上限 80 KB/s
 *   连最低档真实码率的 1/3 都不到 -> 每个窗口都判"超预算 1.15 倍以上" -> AUTO 一路降到
 *   最低档 Q30 就再也动不了(用户实测: AUTO 永远压在 Q30)。
 *   改法: 1 单位 = 40 KB/s, B 键循环 target5 = 7/8/9/10/11 -> 280/320/360/400/440 KB/s,
 *   复位默认 9 (360 KB/s)。控制律(±15% 门限 + 连续 2 窗口)一个字没动。
 *   注意: 水印 [62:58] 仍是 5 bit target5, 位宽/位置不变, 只是**单位语义**从 5 KB/s
 *   变成 40 KB/s -> 板卡端解码必须用 target5 x 40 (rk_pcb_view.py 的 TARGET_STEP_KBPS)。
 *
 * ---- qsel 语义 (见 jpeg_dct2d/jpeg_enc_full) ----
 *   0 = Q30(最糙, 码率最小)  1 = Q50  2 = Q75(复位默认)  3 = Q90(最细, 码率最大)
 ******************************************************************************/
module pre_adapt (
    input  wire        I_clk,          // 像素时钟 (S_hs_rx_clk, 与 JPEG 编码器同域)
    input  wire        I_rst_n,        // 低有效, 用 S_pll_lock

    // ---- 来自 pre_ctrl(25MHz 域) 的准静态配置 ----
    input  wire        I_auto,         // 1=自适应  0=手动
    input  wire [4:0]  I_target5,      // [D27] 目标码率 = I_target5 x 40 KB/s (360KB/s -> 9)
    input  wire [1:0]  I_qsel_man,     // 手动档位 ('Q' 循环出来的)

    // ---- 时基 + 统计窗口 ----
    input  wire        I_tick_ms,      // 1ms 时基脉冲(25MHz 域, 展宽)
    input  wire [23:0] I_win_bytes,    // 刚结束窗口内编码器输出的压缩字节数
    input  wire        I_win_end,      // 窗口结束: 单拍脉冲(与 I_win_bytes 同拍有效)

    // ---- 输出 ----
    output reg  [1:0]  O_qsel,         // 当前生效档位 -> jpeg_enc_full.I_qsel + 水印
    output wire        O_cfg_auto,     // 同步后的 auto  (给水印, 保证与控制器一致)
    output wire [4:0]  O_cfg_target5,  // 同步后的 target5(给水印)
    output reg         O_act_dn,       // 调试: 本窗口真的降了一档(单拍)
    output reg         O_act_up        // 调试: 本窗口真的升了一档(单拍)
);

    //==================================================================
    // 定点比例: acc 的 1 LSB = 2^-FRAC_BITS 字节
    //   [D27] target5=1 现在是 1 x 40 KB/s (原来 5 KB/s):
    //     40960 B/s x 1ms = 40.96 字节 -> 二进制 40.96 x 2^10 = 41943.04 -> 取 41943
    //     相对误差 1e-6, 可忽略; 只有"查表常数"变大, 仍然没有乘法器
    //   (原来是 (5120*2^10+500)/1000 = 5243 对应 5 KB/s)
    //==================================================================
    localparam integer FRAC_BITS = 10;
    localparam integer INC_UNIT  = (40960 * (1 << FRAC_BITS) + 500) / 1000;   // = 41943 (40 KB/s)

    //==================================================================
    // 1. 准静态配置采样 (2 级) + 时基 2FF 同步 / 上升沿检测
    //    说明: I_auto / I_target5 / I_qsel_man 只在收到串口字节时才变(毫秒级间隔),
    //          属于准静态多比特信号, 直接 2FF 采样即可(与工程里 O_mode/O_thresh 同做法)。
    //==================================================================
    reg        auto_r, auto_r2;
    reg [4:0]  tgt_r , tgt_r2 ;
    reg [1:0]  man_r , man_r2 ;
    reg [2:0]  tick_s;

    wire tick_edge = tick_s[2] & ~tick_s[1];              // 1ms 脉冲上升沿(已在 I_clk 域)
    wire restart   = (auto_r2 != auto_r) || (tgt_r2 != tgt_r);   // 模式/目标变了 -> 重开窗口

    assign O_cfg_auto    = auto_r2;
    assign O_cfg_target5 = tgt_r2;

    //==================================================================
    // 2. 每 1ms 累加的预算增量 (查表 = 常数移位, 无乘法器)
    //    [D27] 量程 1 单位 = 40 KB/s: 7/8/9/10/11 -> 280/320/360/400/440 KB/s
    //          default(0/非法) -> 9 = 360 KB/s, 与复位默认对齐, 避免一上电就降档
    //==================================================================
    reg [31:0] inc_v;

    always @(*) begin
        case (tgt_r)
            5'd7 :   inc_v = INC_UNIT * 7;      // 280 KB/s
            5'd8 :   inc_v = INC_UNIT * 8;      // 320 KB/s
            5'd9 :   inc_v = INC_UNIT * 9;      // 360 KB/s  (复位默认)
            5'd10:   inc_v = INC_UNIT * 10;     // 400 KB/s
            5'd11:   inc_v = INC_UNIT * 11;     // 440 KB/s
            default: inc_v = INC_UNIT * 9;      // 0 / 非法值 -> 按 360KB/s
        endcase
    end

    // 饱和加法: 预算累加器 32bit 封顶(2^32/2^10 = 4 MB 预算, 正常窗口远到不了)
    reg  [31:0] acc;
    wire [32:0] acc_nx = {1'b0, acc} + {1'b0, inc_v};

    //==================================================================
    // 3. 窗口预算 -> 字节, 与实测字节数比较 (±15% 门限, 全部用移位加减实现)
    //    over  : win_bytes x 20 > budget x 23        (1.15 倍)
    //    under : win_bytes x 20 < budget x 17        (0.85 倍)
    //==================================================================
    wire [31:0] budget_b = {10'd0, acc[31:FRAC_BITS]};                                 // 预算字节数
    wire [31:0] wb20     = ({8'd0, I_win_bytes} << 4) + ({8'd0, I_win_bytes} << 2);     // x20
    wire [31:0] b23      = (budget_b << 4) + (budget_b << 2) + (budget_b << 1) + budget_b; // x23
    wire [31:0] b17      = (budget_b << 4) + budget_b;                                  // x17

    wire over  = (wb20 > b23);
    wire under = (wb20 < b17);
    wire dec_en = (budget_b != 32'd0);   // 还没攒到时基(acc=0)时不做判断, 防止一上电就冲到 Q30

    //==================================================================
    // 4. 主状态: 滞回计数 + 档位调整
    //    注意: O_qsel / acc / up_s / dn_s 全部只在这一个 always 块里赋值(避开 HDL-8007)
    //==================================================================
    reg [1:0] up_s;      // 连续"低于预算"的窗口数 (0/1, 到 1 再同向一个就动作)
    reg [1:0] dn_s;      // 连续"超预算"的窗口数

    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            auto_r   <= 1'b1;
            auto_r2  <= 1'b1;
            tgt_r    <= 5'd9;          // [D27] 复位默认 360 KB/s (1 单位 = 40 KB/s)
            tgt_r2   <= 5'd9;
            man_r    <= 2'd2;          // 复位默认 Q75
            man_r2   <= 2'd2;
            tick_s   <= 3'd0;
            acc      <= 32'd0;
            up_s     <= 2'd0;
            dn_s     <= 2'd0;
            O_qsel   <= 2'd2;          // 复位默认 Q75
            O_act_dn <= 1'b0;
            O_act_up <= 1'b0;
        end else begin
            // ---- 准静态配置 + 时基同步 ----
            auto_r  <= I_auto;
            auto_r2 <= auto_r;
            tgt_r   <= I_target5;
            tgt_r2  <= tgt_r;
            man_r   <= I_qsel_man;
            man_r2  <= man_r;
            tick_s  <= {tick_s[1:0], I_tick_ms};

            O_act_dn <= 1'b0;
            O_act_up <= 1'b0;

            // ---- (a) 每 1ms 累加预算 ----
            if (tick_edge) begin
                acc <= acc_nx[32] ? 32'hFFFF_FFFF : acc_nx[31:0];
            end

            // ---- (b) 模式/目标变化: 预算与滞回计数重开 ----
            if (restart) begin
                acc  <= 32'd0;
                up_s <= 2'd0;
                dn_s <= 2'd0;
            end

            // ---- (c) 手动模式: 档位直接跟随, 控制器不动 ----
            if (!auto_r2) begin
                O_qsel <= man_r2;
                up_s   <= 2'd0;
                dn_s   <= 2'd0;
                if (I_win_end) acc <= 32'd0;
            end
            // ---- (d) 自适应模式: 窗口结束时评估一次 ----
            else if (I_win_end) begin
                acc <= 32'd0;                       // 新窗口从零攒预算
                if (dec_en) begin
                    if (over) begin
                        up_s <= 2'd0;
                        if (dn_s == 2'd1) begin     // 连续第 2 个超预算窗口 -> 降一档
                            dn_s <= 2'd0;
                            if (O_qsel != 2'd0) begin
                                O_qsel   <= O_qsel - 2'd1;
                                O_act_dn <= 1'b1;
                            end
                        end else begin
                            dn_s <= 2'd1;
                        end
                    end else if (under) begin
                        dn_s <= 2'd0;
                        if (up_s == 2'd1) begin     // 连续第 2 个低预算窗口 -> 升一档
                            up_s <= 2'd0;
                            if (O_qsel != 2'd3) begin
                                O_qsel   <= O_qsel + 2'd1;
                                O_act_up <= 1'b1;
                            end
                        end else begin
                            up_s <= 2'd1;
                        end
                    end else begin                  // 落在 +-15% 滞回带内: 两个方向都清
                        up_s <= 2'd0;
                        dn_s <= 2'd0;
                    end
                end
            end
        end
    end

endmodule
