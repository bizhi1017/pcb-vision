`timescale 1ns/1ps
/*******************************************************************************
 * 文件名   : pre_sobel3x3.v
 * 所属工程 : p1_preprocess
 * 功能     : Sobel 边缘检测 (4 像素/时钟并行) + 可调阈值二值化
 *
 *   Gx = (上右 + 2*中右 + 下右) - (上左 + 2*中左 + 下左)
 *   Gy = (上左 + 2*上中 + 上右) - (下左 + 2*下中 + 下右)
 *   Mag = |Gx| + |Gy|            (梯度幅值近似, 省去平方/开方)
 *   Out = (Mag > TH) ? 255 : 0   (阈值分割, TH 由端口给出)
 *
 * 数据格式 / 三行缓存 / 6 像素窗 / 边界处理 / 时序 与 pre_gauss3x3 完全一致
 *   (同一套已验证的架构, 见 pre_gauss3x3.v 头注释)
 *
 *   窗口列: w0 = 4k-2 ... w5 = 4k+3, 输出中心列 = 4k-1 .. 4k+2 (整体右移 1 像素)
 *   前 2 行: 无完整垂直窗 -> 输出 0 (黑边, 不参与后续)
 *   P6加权和、绝对差、幅值阈值分级，固定延迟4拍，II=1。
 ******************************************************************************/
module pre_sobel3x3 #(
    parameter integer LINE_WORDS = 512,
    parameter integer AW         = 9
) (
    input  wire        I_clk,
    input  wire        I_rst_n,

    input  wire        I_tvalid,
    input  wire        I_tlast,
    input  wire        I_tuser,
    input  wire [31:0] I_tdata,        // 4 x 8bit 灰度, px0 = [7:0]

    input  wire [10:0] I_thresh,       // 0..2040, 越大边缘越少

    output reg         O_tvalid,
    output reg         O_tlast,
    output reg         O_tuser,
    output reg  [31:0] O_tdata         // 4 个 8bit 二值像素 (0x00 / 0xFF)
);

    localparam integer DW = 32;

    //==================================================================
    // 1~4. 三行缓存 (与 gauss 同一套结构)
    //==================================================================
    reg [DW-1:0] m0 [0:LINE_WORDS-1];
    reg [DW-1:0] m1 [0:LINE_WORDS-1];
    reg [DW-1:0] m2 [0:LINE_WORDS-1];

    reg [11:0]   row_idx;
    reg [AW-1:0] beat_idx;
    reg [1:0]    wr_sel, rd1_sel, rd2_sel;

    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            row_idx   <= 12'd0;
            beat_idx  <= {AW{1'b0}};
            wr_sel    <= 2'd0;
            rd1_sel   <= 2'd2;
            rd2_sel   <= 2'd1;
        end else if (I_tvalid) begin
            if (I_tuser) begin
                beat_idx<=1;row_idx<=0;wr_sel<=0;rd1_sel<=2;rd2_sel<=1;
            end else if (I_tlast) begin
                beat_idx <= {AW{1'b0}};
                row_idx  <= row_idx + 12'd1;
                wr_sel   <= (wr_sel  == 2'd2) ? 2'd0 : wr_sel  + 2'd1;
                rd1_sel  <= wr_sel;
                rd2_sel  <= rd1_sel;
            end else begin
                beat_idx <= beat_idx + {{(AW-1){1'b0}}, 1'b1};
            end
        end
    end

    always @(posedge I_clk) begin
        if (I_tvalid) begin
            case (I_tuser ? 2'd0 : wr_sel)
                2'd0    : m0[I_tuser ? {AW{1'b0}} : beat_idx] <= I_tdata;
                2'd1    : m1[I_tuser ? {AW{1'b0}} : beat_idx] <= I_tdata;
                default : m2[I_tuser ? {AW{1'b0}} : beat_idx] <= I_tdata;
            endcase
        end
    end

    reg [DW-1:0] q0, q1, q2;
    reg [1:0]    rd1_sel_d, rd2_sel_d;

    always @(posedge I_clk) begin
        q0 <= m0[I_tuser ? {AW{1'b0}} : beat_idx];
        q1 <= m1[I_tuser ? {AW{1'b0}} : beat_idx];
        q2 <= m2[I_tuser ? {AW{1'b0}} : beat_idx];

        rd1_sel_d <= I_tuser ? 2'd2 : rd1_sel;
        rd2_sel_d <= I_tuser ? 2'd1 : rd2_sel;
    end

    wire [DW-1:0] d_row1 = (rd1_sel_d == 2'd0) ? q0 : (rd1_sel_d == 2'd1) ? q1 : q2;
    wire [DW-1:0] d_row2 = (rd2_sel_d == 2'd0) ? q0 : (rd2_sel_d == 2'd1) ? q1 : q2;

    //==================================================================
    // 5. 拍对齐 + 前 2 像素补齐
    //==================================================================
    reg [DW-1:0] cur;
    reg [15:0]   cur_p23;
    reg [15:0]   r1_p23;
    reg [15:0]   r2_p23;
    reg [11:0]   row_d1;
    reg          beat0_d1;

    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            cur      <= 32'd0;
            cur_p23  <= 16'd0;
            r1_p23   <= 16'd0;
            r2_p23   <= 16'd0;
            row_d1   <= 12'd0;
            beat0_d1 <= 1'b0;
        end else begin
            if(I_tvalid)cur <= I_tdata;
            row_d1 <= I_tuser ? 12'd0 : row_idx;
            beat0_d1 <= I_tvalid && (I_tuser || beat_idx == 0);

            if(vld_d1)begin
            cur_p23 <= {cur[23:16],    cur[31:24]   };
            r1_p23  <= {d_row1[23:16], d_row1[31:24]};
            r2_p23  <= {d_row2[23:16], d_row2[31:24]};
            end
        end
    end

    wire [15:0] w0_p23 = beat0_d1 ? {cur[7:0],    cur[7:0]   } : cur_p23;
    wire [15:0] w1_p23 = beat0_d1 ? {d_row1[7:0], d_row1[7:0]} : r1_p23;
    wire [15:0] w2_p23 = beat0_d1 ? {d_row2[7:0], d_row2[7:0]} : r2_p23;

    //==================================================================
    // 6. 6 像素窗: win0=下行(n) win1=中行(n-1) win2=上行(n-2)
    //==================================================================
    wire [47:0] win0 = {cur[31:24],    cur[23:16],    cur[15: 8],    cur[ 7: 0],    w0_p23[ 7: 0], w0_p23[15: 8]};
    wire [47:0] win1 = {d_row1[31:24], d_row1[23:16], d_row1[15: 8], d_row1[ 7: 0], w1_p23[ 7: 0], w1_p23[15: 8]};
    wire [47:0] win2 = {d_row2[31:24], d_row2[23:16], d_row2[15: 8], d_row2[ 7: 0], w2_p23[ 7: 0], w2_p23[15: 8]};

    //==================================================================
    // 7. 提取像素 (p[行][列], 列 0..5; 行 0=上,1=中,2=下)
    //==================================================================
    wire [7:0] t0 = win2[ 7: 0], t1 = win2[15: 8], t2 = win2[23:16],
               t3 = win2[31:24], t4 = win2[39:32], t5 = win2[47:40];
    wire [7:0] m_0= win1[ 7: 0], m_1= win1[15: 8], m_2= win1[23:16],
               m_3= win1[31:24], m_4= win1[39:32], m_5= win1[47:40];
    wire [7:0] b0 = win0[ 7: 0], b1 = win0[15: 8], b2 = win0[23:16],
               b3 = win0[31:24], b4 = win0[39:32], b5 = win0[47:40];

    // 垂直 3 抽头 (列加权): V0..V5 = 上 + 2*中 + 下
    function [10:0] f_vcol;
        input [7:0] t, m_, b;
        reg   [10:0] s;
        begin
            s = {3'b000, t} + {2'b00, m_, 1'b0} + {3'b000, b};   // max 1020
            f_vcol = s;
        end
    endfunction

    wire [10:0] V0 = f_vcol(t0, m_0, b0);
    wire [10:0] V1 = f_vcol(t1, m_1, b1);
    wire [10:0] V2 = f_vcol(t2, m_2, b2);
    wire [10:0] V3 = f_vcol(t3, m_3, b3);
    wire [10:0] V4 = f_vcol(t4, m_4, b4);
    wire [10:0] V5 = f_vcol(t5, m_5, b5);

    // 水平 3 抽头 (行加权): H0..H5 = 左 + 2*中 + 右
    function [10:0] f_hrow;
        input [7:0] l, c, r;
        reg   [10:0] s;
        begin
            s = {3'b000, l} + {2'b00, c, 1'b0} + {3'b000, r};
            f_hrow = s;
        end
    endfunction

    wire [10:0] U0 = f_hrow(t0, t1, t2);   // 上行 列 0..2
    wire [10:0] U1 = f_hrow(t1, t2, t3);
    wire [10:0] U2 = f_hrow(t2, t3, t4);
    wire [10:0] U3 = f_hrow(t3, t4, t5);
    wire [10:0] B0 = f_hrow(b0, b1, b2);   // 下行 列 0..2
    wire [10:0] B1 = f_hrow(b1, b2, b3);
    wire [10:0] B2 = f_hrow(b2, b3, b4);
    wire [10:0] B3 = f_hrow(b3, b4, b5);

    //==================================================================
    // 8. 四路并行求 |Gx|+|Gy| 并阈值化
    //    j=1..4 (中心列), 左侧列 = j-1, 右侧列 = j+1
    //==================================================================
    function [10:0] f_absdiff;
        input [10:0] a;
        input [10:0] b;
        reg   [10:0] d;
        begin
            d = (a >= b) ? (a - b) : (b - a);
            f_absdiff = d;
        end
    endfunction

    function [7:0] f_thresh;
        input [10:0] mag;
        input [10:0] th;
        begin
            f_thresh = (mag > th) ? 8'hFF : 8'h00;
        end
    endfunction


    reg [10:0] vr[0:5],ur[0:3],br[0:3];
    reg [10:0] gxr[1:4],gyr[1:4],th2,th3;
    reg warm2,warm3,v2,l2,u2,v3,l3,u3;
    always @(posedge I_clk)begin
      if(!I_rst_n)begin
        v2<=0;l2<=0;u2<=0;v3<=0;l3<=0;u3<=0;warm2<=0;warm3<=0;th2<=200;th3<=200;
      end else begin
        v2<=vld_d1;l2<=lst_d1;u2<=usr_d1;warm2<=row_d1<2;th2<=I_thresh;
        v3<=v2;l3<=l2;u3<=u2;warm3<=warm2;th3<=th2;
      end
      vr[0]<=V0;
      vr[1]<=V1;
      vr[2]<=V2;
      vr[3]<=V3;
      vr[4]<=V4;
      vr[5]<=V5;
      ur[0]<=U0;br[0]<=B0;
      ur[1]<=U1;br[1]<=B1;
      ur[2]<=U2;br[2]<=B2;
      ur[3]<=U3;br[3]<=B3;
      gxr[1]<=gx1;gyr[1]<=gy1;
      gxr[2]<=gx2;gyr[2]<=gy2;
      gxr[3]<=gx3;gyr[3]<=gy3;
      gxr[4]<=gx4;gyr[4]<=gy4;
    end
    // Gx[j] = V[j+1] - V[j-1]  (垂直加权列和之差)
    wire [10:0] gx1 = f_absdiff(vr[2], vr[0]);
    wire [10:0] gx2 = f_absdiff(vr[3], vr[1]);
    wire [10:0] gx3 = f_absdiff(vr[4], vr[2]);
    wire [10:0] gx4 = f_absdiff(vr[5], vr[3]);

    // Gy[j] = U[j-1] - B[j-1]  (水平加权行和之差)
    wire [10:0] gy1 = f_absdiff(ur[0], br[0]);
    wire [10:0] gy2 = f_absdiff(ur[1], br[1]);
    wire [10:0] gy3 = f_absdiff(ur[2], br[2]);
    wire [10:0] gy4 = f_absdiff(ur[3], br[3]);

    wire [10:0] mag1 = gxr[1] + gyr[1];
    wire [10:0] mag2 = gxr[2] + gyr[2];
    wire [10:0] mag3 = gxr[3] + gyr[3];
    wire [10:0] mag4 = gxr[4] + gyr[4];

    wire [7:0]  e1 = f_thresh(mag1, th3);
    wire [7:0]  e2 = f_thresh(mag2, th3);
    wire [7:0]  e3 = f_thresh(mag3, th3);
    wire [7:0]  e4 = f_thresh(mag4, th3);

    //==================================================================
    // 9. 输出级
    //==================================================================
    reg vld_d1, lst_d1, usr_d1;

    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            vld_d1   <= 1'b0;
            lst_d1   <= 1'b0;
            usr_d1   <= 1'b0;
            O_tvalid <= 1'b0;
            O_tlast  <= 1'b0;
            O_tuser  <= 1'b0;
            O_tdata  <= 32'd0;
        end else begin
            vld_d1 <= I_tvalid;
            lst_d1 <= I_tlast;
            usr_d1 <= I_tuser;

            O_tvalid <= v3;
            O_tlast  <= l3;
            O_tuser  <= u3;
            // 前 2 行无完整垂直窗 -> 输出 0 (黑)
            O_tdata  <= warm3 ? 32'd0 : {e4, e3, e2, e1};
        end
    end

endmodule
