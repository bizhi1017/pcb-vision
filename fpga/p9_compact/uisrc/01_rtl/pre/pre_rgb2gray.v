`timescale 1ns/1ps
/*******************************************************************************
 * 文件名   : pre_rgb2gray.v
 * 所属工程 : p1_preprocess (PCB 智能缺陷检测系统 - FPGA 预处理流水线)
 * 功能     : RGB888 -> 灰度, 4 像素/时钟并行
 *
 * 输入数据格式 (与 ui_isp_top.O_tdata 一致):
 *   I_tdata[127:0] = 4 个像素, 每像素 32bit = {8'd0, R[7:0], G[7:0], B[7:0]}
 *     pixel0 = I_tdata[23:0]   (R=[23:16], G=[15:8], B=[7:0])
 *     pixel1 = I_tdata[55:32]
 *     pixel2 = I_tdata[87:64]
 *     pixel3 = I_tdata[119:96]
 *
 * 灰度公式 (定点, 权重和=256, 无浮点):
 *   Y = (77*R + 150*G + 29*B) >> 8
 *
 * P6时序: 乘积寄存 + 加权求和寄存，固定2拍，II=1。
 * RGB直通同步延迟2拍，灰度pixel0 = O_gray[7:0]。
 ******************************************************************************/
module pre_rgb2gray (
    input  wire         I_clk,
    input  wire         I_rst_n,

    input  wire [127:0] I_tdata,     // 4 x {8'd0,R,G,B}

    output reg  [127:0] O_tdata,     // RGB 直通 (延迟 1)
    output reg  [ 31:0] O_gray       // 4 x 8bit 灰度 (延迟 1), px0 = [7:0]
);

    //------------------------------------------------------------------
    // 定点灰度: 77R + 150G + 29B = 8bit*8bit 乘法, 用移位加法实现
    //   77  = 64 + 8 + 4 + 1
    //   150 = 128 + 16 + 4 + 2
    //   29  = 16 + 8 + 4 + 1
    // 直接写乘法让综合器自己挑 DSP/进位链 (与 Python/OpenCV 完全一致)
    //------------------------------------------------------------------
    function [7:0] f_rgb2y;
        input [15:0] r;
        input [15:0] g;
        input [15:0] b;
        reg   [15:0] sum;
        begin
            sum    = r + g + b;   // Products registered separately for 150 MHz.
            f_rgb2y = sum[15:8];
        end
    endfunction

    (* keep="true" *) reg [15:0] pr[0:3],pg[0:3],pb[0:3];
    reg [127:0] rgb_delay;
    integer p;
    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            rgb_delay<=0;
            for(p=0;p<4;p=p+1)begin pr[p]<=0;pg[p]<=0;pb[p]<=0;end
            O_tdata <= 128'd0;
            O_gray  <= 32'd0;
        end else begin
            rgb_delay<=I_tdata;O_tdata<=rgb_delay;
            for(p=0;p<4;p=p+1)begin
                pr[p]<=I_tdata[p*32+16+:8]*16'd77;
                pg[p]<=I_tdata[p*32+8+:8]*16'd150;
                pb[p]<=I_tdata[p*32+:8]*16'd29;
            end

            O_gray[ 7: 0] <= f_rgb2y(pr[0],pg[0],pb[0]);
            O_gray[15: 8] <= f_rgb2y(pr[1],pg[1],pb[1]);
            O_gray[23:16] <= f_rgb2y(pr[2],pg[2],pb[2]);
            O_gray[31:24] <= f_rgb2y(pr[3],pg[3],pb[3]);
        end
    end

endmodule
