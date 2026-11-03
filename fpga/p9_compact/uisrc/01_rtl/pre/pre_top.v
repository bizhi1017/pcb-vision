`timescale 1ns/1ps
/*******************************************************************************
 * 文件名   : pre_top.v
 * 所属工程 : p1_preprocess
 * 功能     : FPGA 硬件预处理流水线顶层 (插在 ui_isp_top 与 ui_dbuf 之间)
 *
 *   ui_isp_top ──128bit──► pre_top ──128bit──► ui_dbuf (写通道)
 *
 *   模式 (I_mode):
 *     0 = RGB直通；1 = 灰度；2 = 灰度+3x3高斯；
 *     3 = 灰度+Sobel二值边缘（I_thresh）；4 = 灰度+帧间直方图均衡。
 *     P6为150MHz分别流水，模式间延迟不同，II均为1。
 *
 *   数据格式: 4 像素/拍, 每像素 32bit = {8'd0, R, G, B}
 *
 *   说明: 各模式内部 tvalid/tlast/tuser 与数据严格同延迟, 帧结构 (节拍数,
 *         tlast 位置, tuser 位置) 不因处理而改变, 因此 ui_dbuf 的帧缓存
 *         逻辑无需改动。P6由上层在源FS更新模式，活动图像前留有16行空档。
 ******************************************************************************/
module pre_top #(
    parameter integer LINE_WORDS = 512,     // 行缓存深度 (>= ceil(宽/4))
    parameter integer AW         = 9
) (
    input  wire         I_clk,
    input  wire         I_rst_n,

    input  wire [2:0]   I_mode,
    input  wire [10:0]  I_thresh,       // Sobel 阈值 0..2040

    input  wire         I_tvalid,
    input  wire         I_tlast,
    input  wire         I_tuser,
    input  wire [127:0] I_tdata,

    output reg          O_tvalid,
    output reg          O_tlast,
    output reg          O_tuser,
    output reg  [127:0] O_tdata
);

    //==================================================================
    // 0. 跨时钟域同步: I_mode / I_thresh 来自 25MHz 域 (pre_ctrl),
    //    两级触发器同步到本模块所在时钟域 (S_hs_clk)。两者都是准静态信号,
    //    切换瞬间可能出现一拍混合模式, 对显示无影响。
    //==================================================================
    reg [2:0]  mode_s1, mode_s2;
    reg [10:0] th_s1,   th_s2;

    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            mode_s1 <= 3'd0;
            mode_s2 <= 3'd0;
            th_s1   <= 11'd200;
            th_s2   <= 11'd200;
        end else begin
            mode_s1 <= I_mode;
            mode_s2 <= mode_s1;
            th_s1   <= I_thresh;
            th_s2   <= th_s1;
        end
    end

    //==================================================================
    // RGB -> Gray：P6乘积/求和两级，与同步信号一致延迟。
    //==================================================================
    wire [127:0] s_rgb;        // RGB 直通 (延迟 1)
    wire [ 31:0] s_gray;       // 灰度     (延迟 1)

    reg          s_vld1, s_lst1, s_usr1;
    reg          s_vld0, s_lst0, s_usr0;

    pre_rgb2gray u_pre_rgb2gray (
        .I_clk   (I_clk      ),
        .I_rst_n (I_rst_n    ),
        .I_tdata (I_tdata    ),
        .O_tdata (s_rgb      ),
        .O_gray  (s_gray     )
    );

    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            s_vld1 <= 1'b0;
            s_vld0 <= 1'b0;s_lst0 <= 1'b0;s_usr0 <= 1'b0;
            s_lst1 <= 1'b0;
            s_usr1 <= 1'b0;
        end else begin
            s_vld0 <= I_tvalid;s_lst0 <= I_tlast;s_usr0 <= I_tuser;
            s_vld1 <= s_vld0;
            s_lst1 <= s_lst0;
            s_usr1 <= s_usr0;
        end
    end

    //==================================================================
    // 第 2 级 (并行): 3x3 高斯
    //==================================================================
    wire         s_gv, s_gl, s_gu;
    wire [31:0]  s_gauss;

    pre_gauss3x3 #(
        .LINE_WORDS (LINE_WORDS),
        .AW         (AW        )
    ) u_pre_gauss3x3 (
        .I_clk    (I_clk   ),
        .I_rst_n  (I_rst_n ),
        .I_tvalid (s_vld1  ),
        .I_tlast  (s_lst1  ),
        .I_tuser  (s_usr1  ),
        .I_tdata  (s_gray  ),
        .O_tvalid (s_gv    ),
        .O_tlast  (s_gl    ),
        .O_tuser  (s_gu    ),
        .O_tdata  (s_gauss )
    );

    //==================================================================
    // 第 2 级 (并行): 3x3 Sobel 边缘 + 阈值二值化
    //==================================================================
    wire         s_ev, s_el, s_eu;
    wire [31:0]  s_edge;

    pre_sobel3x3 #(
        .LINE_WORDS (LINE_WORDS),
        .AW         (AW        )
    ) u_pre_sobel3x3 (
        .I_clk    (I_clk    ),
        .I_rst_n  (I_rst_n  ),
        .I_tvalid (s_vld1   ),
        .I_tlast  (s_lst1   ),
        .I_tuser  (s_usr1   ),
        .I_tdata  (s_gray   ),
        .I_thresh (th_s2    ),
        .O_tvalid (s_ev     ),
        .O_tlast  (s_el     ),
        .O_tuser  (s_eu     ),
        .O_tdata  (s_edge   )
    );

    //==================================================================
    // 第 2 级 (并行): 直方图均衡 (帧间统计 -> LUT)
    //   注意: FRAME_LINES 必须与实际输出行数一致 (ISP 输出 1920x1080)
    //==================================================================
    wire         s_hv, s_hl, s_hu;
    wire [31:0]  s_heq;

    pre_histeq #(
        .BEATS_PER_LINE (480 ),
        .FRAME_LINES    (1080),
        .SHIFT          (13  )
    ) u_pre_histeq (
        .I_clk    (I_clk   ),
        .I_rst_n  (I_rst_n ),
        .I_tvalid (s_vld1  ),
        .I_tlast  (s_lst1  ),
        .I_tuser  (s_usr1  ),
        .I_tdata  (s_gray  ),
        .O_tvalid (s_hv    ),
        .O_tlast  (s_hl    ),
        .O_tuser  (s_hu    ),
        .O_tdata  (s_heq   )
    );

    //==================================================================
    // 灰度 -> 128bit RGB (R=G=B=Y), 供显示/后续模块统一格式
    //==================================================================
    function [127:0] f_gray2rgb;
        input [31:0] y;              // 4 x 8bit, px0 = [7:0]
        begin
            f_gray2rgb = {
                8'd0, y[31:24], y[31:24], y[31:24],
                8'd0, y[23:16], y[23:16], y[23:16],
                8'd0, y[15: 8], y[15: 8], y[15: 8],
                8'd0, y[ 7: 0], y[ 7: 0], y[ 7: 0]
            };
        end
    endfunction

    //==================================================================
    // 模式选择 (按模式选各自的"数据 + 同步延迟"通路)
    //==================================================================
    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            O_tvalid <= 1'b0;
            O_tlast  <= 1'b0;
            O_tuser  <= 1'b0;
            O_tdata  <= 128'd0;
        end else begin
            case (mode_s2)
                // ---------------- 0: 原图直通 ----------------
                3'd0: begin
                    O_tdata  <= s_rgb;
                    O_tvalid <= s_vld1;
                    O_tlast  <= s_lst1;
                    O_tuser  <= s_usr1;
                end
                // ---------------- 1: 灰度 ----------------
                3'd1: begin
                    O_tdata  <= f_gray2rgb(s_gray);
                    O_tvalid <= s_vld1;
                    O_tlast  <= s_lst1;
                    O_tuser  <= s_usr1;
                end
                // ---------------- 2: 灰度 + 高斯 ----------------
                3'd2: begin
                    O_tdata  <= f_gray2rgb(s_gauss);
                    O_tvalid <= s_gv;
                    O_tlast  <= s_gl;
                    O_tuser  <= s_gu;
                end
                // ---------------- 3: 灰度 + Sobel 二值边缘 ------
                3'd3: begin
                    O_tdata  <= f_gray2rgb(s_edge);
                    O_tvalid <= s_ev;
                    O_tlast  <= s_el;
                    O_tuser  <= s_eu;
                end
                // ---------------- 4: 灰度 + 直方图均衡 ----------
                default: begin
                    O_tdata  <= f_gray2rgb(s_heq);
                    O_tvalid <= s_hv;
                    O_tlast  <= s_hl;
                    O_tuser  <= s_hu;
                end
            endcase
        end
    end

endmodule
