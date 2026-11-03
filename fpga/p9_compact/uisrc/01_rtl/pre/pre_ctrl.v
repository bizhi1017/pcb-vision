`timescale 1ns/1ps
/*******************************************************************************
 * 文件名   : pre_ctrl.v
 * 所属工程 : p1_preprocess
 * 功能     : 串口命令控制器 —— 在线切换预处理模式 / 调 Sobel 阈值 / 上电握手
 *
 * 为什么用串口: 本板 **所有用户按键都在 RK3576 侧**（KEY1/RESET_L/PWRON_L/
 *               SARADC_VIN0_BOOT 均为 RK 引脚），FPGA 侧没有任何按键,
 *               只有一路 UART0（RX=V11, TX=U11，连接器 J30，CH9102F）到 Type-C。
 *
 * 串口参数: 115200 8N1 无流控 (与上位机串口助手默认一致)
 *
 * 命令 (单字符, 不需要回车):
 *     '0' 原图 (RGB 直通)
 *     '1' 灰度
 *     '2' 灰度 + 3x3 高斯
 *     '3' 灰度 + Sobel 边缘(阈值二值化)
 *     '+' Sobel 阈值 +32   (上限 2040)
 *     '-' Sobel 阈值 -32   (下限 8)
 *     'J' JPEG 码流是否从同一个串口输出 (开关, 默认关)
 *
 * ---- D17 新增: JPEG 质量 / 码率 (反馈走视频水印, 这两个命令**不回串口消息**) ----
 *     'Q' (0x51) 切到 **手动** 模式, 并循环四档 0->1->2->3->0  (Q30/Q50/Q75/Q90)
 *     'A' (0x41) 切到 **自适应** 模式 (按目标码率自动调档)
 *     'B' (0x42) 循环"目标码率"预设 280/320/360/400/440 KB/s ([D27] 1 单位 = 40 KB/s)
 *     复位默认: AUTO 模式, 目标 360 KB/s (target5=9), qsel=2 (Q75)
 *     注意: '+'/'-' 已被 Sobel 阈值占用, 不用于码率。
 *
 * 回传 (ASCII, 串口助手直接可读):
 *     上电约 10ms 后: "PRE-OK\r\n"      <-- 握手: 看到它就说明串口和波特率都对
 *     每次切模式  :   "MODE=n\r\n"
 *     每次改阈值  :   "TH=nnn\r\n"
 ******************************************************************************/
module pre_ctrl #(
    parameter integer CLK_HZ   = 25_000_000,
    parameter integer BAUD     = 115200,
    parameter [10:0]  TH_DEF   = 11'd200
) (
    input  wire        I_clk,          // 25MHz (S_clk_25m 网络)
    input  wire        I_rst_n,

    input  wire        I_frame_tog,    // 每帧翻转一次 (来自 S_hs_rx_clk 域)
    input  wire        I_uart_rx,      // 来自 Type-C 串口 (J30, V11)
    output reg         O_uart_tx,      // 回传 (U11)

    output reg  [2:0]  O_mode,
    output reg  [10:0] O_thresh,
    output reg         O_mode_changed,  // 单拍脉冲
    output reg         O_dump_en,       // 'J' 切换: JPEG 码流是否从同一个串口输出(默认关)

    //---- D17: JPEG 质量/码率 (全部为准静态, 可安全地给像素时钟域直接采样) ----
    output reg  [1:0]  O_qsel,          // 手动档位 (0=Q30 1=Q50 2=Q75 3=Q90), 复位 2
    output reg         O_auto,          // 1=自适应 0=手动, 复位 1
    output reg  [4:0]  O_target5,       // [D27] 目标码率 = O_target5 x 40 KB/s, 复位 9 (=360KB/s)
    output reg         O_ms_tick        // 1ms 时基脉冲(展宽 64 拍), 给像素域算码率用
);

    localparam integer DIV = CLK_HZ / BAUD;      // 25e6/115200 = 217
    localparam integer MS_DIV = CLK_HZ / 1000;   // 1ms 时基分频 = 25000 (25MHz)

    localparam [1:0] KIND_MODE   = 2'd0;
    localparam [1:0] KIND_THRESH = 2'd1;
    localparam [1:0] KIND_BANNER = 2'd2;
    localparam [1:0] KIND_FPS    = 2'd3;

    //==================================================================
    // 1. UART 接收 (115200 8N1)
    //==================================================================
    reg [1:0] rx_sync;
    always @(posedge I_clk) rx_sync <= {rx_sync[0], I_uart_rx};
    wire rx_line = rx_sync[1];

    reg [15:0] rx_cnt;
    reg [3:0]  rx_bit;
    reg [7:0]  rx_shift;
    reg        rx_busy;
    reg        rx_done;
    reg [7:0]  rx_data;

    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            rx_busy  <= 1'b0;
            rx_cnt   <= 16'd0;
            rx_bit   <= 4'd0;
            rx_shift <= 8'd0;
            rx_done  <= 1'b0;
            rx_data  <= 8'd0;
        end else begin
            rx_done <= 1'b0;

            if (!rx_busy) begin
                if (!rx_line) begin              // 检测起始位下降沿
                    rx_busy <= 1'b1;
                    rx_cnt  <= DIV / 2 - 1;      // 对到起始位中点
                    rx_bit  <= 4'd0;
                end
            end else begin
                if (rx_cnt == 16'd0) begin
                    rx_cnt <= DIV - 1;
                    if (rx_bit == 4'd0) begin
                        rx_bit <= 4'd1;          // 起始位确认, 开始收数据
                    end else if (rx_bit <= 4'd8) begin
                        rx_shift <= {rx_line, rx_shift[7:1]};   // LSB first
                        rx_bit   <= rx_bit + 4'd1;
                    end else begin
                        rx_busy <= 1'b0;         // 停止位
                        rx_done <= 1'b1;
                        rx_data <= rx_shift;
                    end
                end else begin
                    rx_cnt <= rx_cnt - 16'd1;
                end
            end
        end
    end

    //==================================================================
    // 1b. 帧率统计: 帧翻转信号 2FF 同步 + 边沿计数, 每 1 秒上报一次
    //==================================================================
    reg [2:0]  tg_sync;
    reg        tg_d;
    reg [7:0]  fps_cnt;
    reg [7:0]  fps_val;
    reg [24:0] sec_cnt;

    wire tg_edge = tg_sync[2] ^ tg_d;

    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            tg_sync <= 3'd0;
            tg_d    <= 1'b0;
            fps_cnt <= 8'd0;
            fps_val <= 8'd0;
            sec_cnt <= 25'd0;
        end else begin
            tg_sync <= {tg_sync[1:0], I_frame_tog};
            tg_d    <= tg_sync[2];
            if (tg_edge && (fps_cnt != 8'd255)) fps_cnt <= fps_cnt + 8'd1;

            if (sec_cnt == 25'd25_000_000 - 1) begin
                sec_cnt <= 25'd0;
                fps_val <= fps_cnt;
                fps_cnt <= 8'd0;
            end else begin
                sec_cnt <= sec_cnt + 25'd1;
            end
        end
    end

    //==================================================================
    // 1c. [D17] 1ms 时基脉冲 —— 给像素域算"码率"用
    //     为什么要有它: 自适应码率的统计窗口是"4 个视频帧", 但帧率随 AE/曝光变化,
    //     像素时钟 S_hs_rx_clk 又是 MIPI 恢复时钟(频率不该写死)。而本模块的
    //     I_clk = 25MHz 晶振是确定的, 所以在这里产生 1ms 脉冲, 展宽 64 拍(2.56us)
    //     后送进像素域做 2FF 同步 + 取上升沿 —— 展宽是为了保证慢时钟域也不会漏掉。
    //==================================================================
    reg [15:0] ms_cnt;
    reg [6:0]  tick_w;

    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            ms_cnt    <= 16'd0;
            tick_w    <= 7'd0;
            O_ms_tick <= 1'b0;
        end else begin
            if (ms_cnt == MS_DIV - 1) begin         // 到 1ms: 起一个 64 拍宽的脉冲
                ms_cnt    <= 16'd0;
                tick_w    <= 7'd63;
                O_ms_tick <= 1'b1;
            end else begin
                ms_cnt <= ms_cnt + 16'd1;
                if (tick_w != 7'd0) begin
                    tick_w    <= tick_w - 7'd1;
                    O_ms_tick <= 1'b1;
                end else begin
                    O_ms_tick <= 1'b0;
                end
            end
        end
    end

    //==================================================================
    // 2. 命令解析 + 上电握手 + 发送请求
    //==================================================================
    reg [25:0] boot_cnt;      // 25MHz x 2s = 50,000,000, 需 26bit
    reg        boot_done;
    reg        tx_req;
    reg [1:0]  tx_kind;

    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            O_mode         <= 3'd0;
            O_thresh       <= TH_DEF;
            O_mode_changed <= 1'b0;
            O_qsel         <= 2'd2;      // 复位默认 Q75
            O_auto         <= 1'b1;      // 复位默认 自适应
            O_target5      <= 5'd9;      // [D27] 复位默认 360 KB/s (1 单位 = 40 KB/s)
            tx_req         <= 1'b0;
            tx_kind        <= KIND_MODE;
            boot_cnt       <= 26'd0;
            boot_done      <= 1'b0;
        end else begin
            O_mode_changed <= 1'b0;
            tx_req         <= 1'b0;

            // ---- 上电握手: 10ms 后先发一次, 之后每 2 秒发一次心跳 ----
            //      这样上位机任何时候打开串口, 最多 2 秒就能看到 "PRE-OK",
            //      用来判断 (a) FPGA 是否加载了本设计 (b) 串口/波特率是否正确。
            if (!boot_done) begin
                if (boot_cnt == 26'd250_000) begin     // 10ms
                    boot_done <= 1'b1;
                    boot_cnt  <= 26'd0;
                    tx_req    <= 1'b1;
                    tx_kind   <= KIND_BANNER;
                end else begin
                    boot_cnt <= boot_cnt + 26'd1;
                end
            end else begin
                if (boot_cnt == 26'd50_000_000) begin     // 2s @25MHz
                    boot_cnt <= 26'd0;
                    tx_req   <= 1'b1;
                    tx_kind  <= KIND_BANNER;
                end else begin
                    boot_cnt <= boot_cnt + 26'd1;
                end
            end

            // ---- 每秒上报一次帧率 (优先级低于命令与握手) ----
            if ((sec_cnt == 25'd25_000_000 - 1) && boot_done && (rx_done == 1'b0)) begin
                tx_req  <= 1'b1;
                tx_kind <= KIND_FPS;
            end

            // ---- 命令 ----
            if (rx_done) begin
                case (rx_data)
                    8'h30, 8'h31, 8'h32, 8'h33, 8'h34 : begin   // '0'..'4'
                        O_mode         <= {1'b0, rx_data[2:0]};
                        O_mode_changed <= 1'b1;
                        tx_req         <= 1'b1;
                        tx_kind        <= KIND_MODE;
                    end
                    8'h2B : begin                              // '+'
                        O_thresh <= (O_thresh > 11'd2000) ? 11'd2040
                                 : (O_thresh + 11'd32 > 11'd2040) ? 11'd2040
                                 : O_thresh + 11'd32;
                        tx_req  <= 1'b1;
                        tx_kind <= KIND_THRESH;
                    end
                    8'h4A : begin                              // 'J' 切换码流输出
                        O_dump_en <= ~O_dump_en;               // 不回消息: 看终端是否有二进制刷屏即可
                    end
                    //---- D17 新增: JPEG 质量 / 目标码率 (反馈只走视频水印, 不回串口) ----
                    8'h51 : begin                              // 'Q' 手动模式 + 循环四档
                        O_auto <= 1'b0;
                        O_qsel <= (O_qsel == 2'd3) ? 2'd0 : (O_qsel + 2'd1);
                    end
                    8'h41 : begin                              // 'A' 自适应模式
                        O_auto <= 1'b1;
                    end
                    8'h42 : begin                              // 'B' 循环目标码率 280/320/360/400/440 KB/s
                        // [D27] 1 单位 = 40 KB/s (原 5 KB/s)。原来上限 80 KB/s 远低于
                        // 实测 Q30≈270 / Q90≈480 KB/s, AUTO 永远判超预算 -> 死压在 Q30。
                        case (O_target5)
                            5'd7 :   O_target5 <= 5'd8;      // 280 -> 320 KB/s
                            5'd8 :   O_target5 <= 5'd9;      // 320 -> 360
                            5'd9 :   O_target5 <= 5'd10;     // 360 -> 400
                            5'd10:   O_target5 <= 5'd11;     // 400 -> 440
                            default: O_target5 <= 5'd7;      // 440 -> 280 (回绕); 0/非法 -> 7
                        endcase
                    end
                    8'h2D : begin                              // '-'
                        O_thresh <= (O_thresh < 11'd40) ? 11'd8
                                 : O_thresh - 11'd32;
                        tx_req  <= 1'b1;
                        tx_kind <= KIND_THRESH;
                    end
                    default : ;                                // 其它字符(含回车)忽略
                endcase
            end
        end
    end

    //==================================================================
    // 3. 消息字节生成 (每条固定 8 字节, CRLF 结尾)
    //==================================================================
    wire [3:0] th_h = O_thresh / 11'd100;
    wire [3:0] th_t = (O_thresh % 11'd100) / 11'd10;
    wire [3:0] th_o = O_thresh % 11'd10;

    function [7:0] f_msg;
        input [1:0] kind;
        input [2:0] idx;
        input [2:0] mode;
        input [3:0] hh, tt, oo;
        input [7:0] fps_in;
        begin
            case (kind)
                KIND_MODE : begin
                    case (idx)
                        3'd0: f_msg = "M";
                        3'd1: f_msg = "O";
                        3'd2: f_msg = "D";
                        3'd3: f_msg = "E";
                        3'd4: f_msg = "=";
                        3'd5: f_msg = {5'b00110, mode};    // '0'(0x30) + mode
                        3'd6: f_msg = 8'h0D;               // CR
                        default: f_msg = 8'h0A;            // LF
                    endcase
                end
                KIND_THRESH : begin
                    case (idx)
                        3'd0: f_msg = "T";
                        3'd1: f_msg = "H";
                        3'd2: f_msg = "=";
                        3'd3: f_msg = {4'b0011, hh};       // '0' + 百位
                        3'd4: f_msg = {4'b0011, tt};
                        3'd5: f_msg = {4'b0011, oo};
                        3'd6: f_msg = 8'h0D;
                        default: f_msg = 8'h0A;
                    endcase
                end
                KIND_BANNER : begin                       // "PRE-OK\r\n"
                    case (idx)
                        3'd0: f_msg = "P";
                        3'd1: f_msg = "R";
                        3'd2: f_msg = "E";
                        3'd3: f_msg = "-";
                        3'd4: f_msg = "O";
                        3'd5: f_msg = "K";
                        3'd6: f_msg = 8'h0D;
                        default: f_msg = 8'h0A;
                    endcase
                end
                default : begin                            // FPS: "F=nn\r\n"
                    case (idx)
                        3'd0: f_msg = "F";
                        3'd1: f_msg = "=";
                        3'd2: f_msg = {4'h3, fps_in[7:4]};  // 十位
                        3'd3: f_msg = {4'h3, fps_in[3:0]};  // 个位
                        3'd4: f_msg = 8'h0D;
                        default: f_msg = 8'h0A;
                    endcase
                end
            endcase
        end
    endfunction

    //==================================================================
    // 4. UART 发送 (115200 8N1)
    //==================================================================
    reg [15:0] tx_cnt;
    reg        tx_tick;

    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            tx_cnt  <= 16'd0;
            tx_tick <= 1'b0;
        end else begin
            if (tx_cnt == 16'd0) begin
                tx_cnt  <= DIV - 1;
                tx_tick <= 1'b1;
            end else begin
                tx_cnt  <= tx_cnt - 16'd1;
                tx_tick <= 1'b0;
            end
        end
    end

    reg [3:0] tx_state;      // 0=idle, 1=start, 2..9=data, 10=stop
    reg [2:0] tx_idx;
    reg [1:0] tx_kind_l;
    reg [7:0] tx_shift;
    reg [3:0] tx_len;

    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            tx_state  <= 4'd0;
            tx_idx    <= 3'd0;
            tx_kind_l <= 2'd0;
            tx_shift  <= 8'd0;
            tx_len    <= 4'd8;
            O_uart_tx <= 1'b1;
        end else begin
            case (tx_state)
                4'd0 : begin                       // idle: 等发送请求
                    O_uart_tx <= 1'b1;
                    if (tx_req) begin
                        tx_kind_l <= tx_kind;
                        tx_idx    <= 3'd0;
                        tx_len    <= (tx_kind == KIND_FPS) ? 4'd6 : 4'd8;
                        tx_shift  <= f_msg(tx_kind, 3'd0, O_mode, th_h, th_t, th_o, fps_val);
                        tx_state  <= 4'd1;
                    end
                end
                4'd1 : begin                       // 起始位
                    if (tx_tick) begin
                        O_uart_tx <= 1'b0;
                        tx_state  <= 4'd2;
                    end
                end
                4'd2, 4'd3, 4'd4, 4'd5, 4'd6, 4'd7, 4'd8, 4'd9 : begin
                    if (tx_tick) begin             // 8 个数据位, LSB first
                        O_uart_tx <= tx_shift[0];
                        tx_shift  <= {1'b0, tx_shift[7:1]};
                        tx_state  <= tx_state + 4'd1;
                    end
                end
                4'd10 : begin                      // 停止位
                    if (tx_tick) begin
                        O_uart_tx <= 1'b1;
                        if ((tx_idx + 3'd1) >= tx_len) begin
                            tx_state <= 4'd0;
                        end else begin
                            tx_idx   <= tx_idx + 3'd1;
                            tx_shift <= f_msg(tx_kind_l, tx_idx + 3'd1,
                                              O_mode, th_h, th_t, th_o, fps_val);
                            tx_state <= 4'd1;
                        end
                    end
                end
                default : tx_state <= 4'd0;
            endcase
        end
    end

endmodule
