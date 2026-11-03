`timescale 1ns/1ps
/*******************************************************************************
 * 文件名   : pre_gauss3x3.v
 * 所属工程 : p1_preprocess
 * 功能     : 3x3 高斯平滑, 4 像素/时钟并行
 *            核 = 1/16 * [[1,2,1],[2,4,2],[1,2,1]]
 *
 * 数据格式 : I_tdata[31:0] = 4 个 8bit 灰度像素, pixel0 = [7:0]
 *
 * 架构 (三行缓存 ping-pong + 6 像素滑窗):
 *   - 3 块 ERAM (每块 1 写 1 读), LINE_WORDS x 32bit, 存一整行 (32bit=4 像素)
 *   - 写: 当前行 (row n)   -> bank[n%3]
 *   - 读: 同地址从 bank[(n-1)%3] / bank[(n-2)%3] 读, 得到上一行 / 上上行的同一 beat
 *   - 6 像素窗 = {上一拍的后 2 像素, 本拍的 4 像素}, 覆盖列 (4k-2 .. 4k+3)
 *     可产生 4 个输出, 中心列 = (4k-1 .. 4k+2)
 *     ==> 图像整体右移 1 像素 (左边沿复制, 末列丢弃), 视觉不可见
 *   - 先做 3 行各 4 个水平 3 抽头 (a + 2b + c), 再垂直 (上 + 2*中 + 下) >> 4
 *   - P6稳态II=1，水平和/垂直和分级，固定延迟3拍。
 *
 * 边界:
 *   - 每帧前 2 行 (row < 2): 垂直窗不完整 -> 灰度直通
 *   - 每行第 0 个 beat: 水平左边界用 pixel0 复制
 ******************************************************************************/
module pre_gauss3x3 #(
    parameter integer LINE_WORDS = 512,      // 每行 >= ceil(宽/4); 1920/4=480 -> 512
    parameter integer AW         = 9         // log2(LINE_WORDS)
) (
    input  wire        I_clk,
    input  wire        I_rst_n,

    input  wire        I_tvalid,
    input  wire        I_tlast,       // 一行最后一个 beat
    input  wire        I_tuser,       // 一帧第一个 beat
    input  wire [31:0] I_tdata,       // 4 x 8bit 灰度, px0 = [7:0]

    output reg         O_tvalid,
    output reg         O_tlast,
    output reg         O_tuser,
    output reg  [31:0] O_tdata
);

    localparam integer DW = 32;

    //==================================================================
    // 1. 三块行缓存
    //==================================================================
    reg [DW-1:0] m0 [0:LINE_WORDS-1];
    reg [DW-1:0] m1 [0:LINE_WORDS-1];
    reg [DW-1:0] m2 [0:LINE_WORDS-1];

    //==================================================================
    // 2. 行 / 列计数 + bank 轮转
    //    row n:   wr_sel = n%3,  rd1_sel = (n-1)%3,  rd2_sel = (n-2)%3
    //    行结束:  wr' = wr+1,  rd1' = wr,  rd2' = rd1
    //==================================================================
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

    //==================================================================
    // 3. 写端口 (按 wr_sel 选 bank)
    //==================================================================
    always @(posedge I_clk) begin
        if (I_tvalid) begin
            case (I_tuser ? 2'd0 : wr_sel)
                2'd0    : m0[I_tuser ? {AW{1'b0}} : beat_idx] <= I_tdata;
                2'd1    : m1[I_tuser ? {AW{1'b0}} : beat_idx] <= I_tdata;
                default : m2[I_tuser ? {AW{1'b0}} : beat_idx] <= I_tdata;
            endcase
        end
    end

    //==================================================================
    // 4. 读端口: 三块同地址各读一次, 再用 rd1_sel/rd2_sel 选
    //    q* 在 t+1 有效, 内容 = 各 bank 第 k 个 beat
    //==================================================================
    reg [DW-1:0] q0, q1, q2;
    reg [1:0]    rd1_sel_d, rd2_sel_d;

    always @(posedge I_clk) begin
        q0 <= m0[I_tuser ? {AW{1'b0}} : beat_idx];
        q1 <= m1[I_tuser ? {AW{1'b0}} : beat_idx];
        q2 <= m2[I_tuser ? {AW{1'b0}} : beat_idx];

        // 读数据与"发起读时的 bank 选择"必须同拍对齐:
        // 行末 beat 时 bank 选择会轮转, 而 q 里还是上一拍的读数据,
        // 因此选择信号也要延迟 1 拍再用, 否则行末像素会取错行。
        rd1_sel_d <= I_tuser ? 2'd2 : rd1_sel;
        rd2_sel_d <= I_tuser ? 2'd1 : rd2_sel;
    end

    wire [DW-1:0] d_row1 = (rd1_sel_d == 2'd0) ? q0 : (rd1_sel_d == 2'd1) ? q1 : q2;
    wire [DW-1:0] d_row2 = (rd2_sel_d == 2'd0) ? q0 : (rd2_sel_d == 2'd1) ? q1 : q2;

    //==================================================================
    // 5. t+1 拍对齐: cur / d_row1 / d_row2 都是第 k 个 beat;
    //    *_p23 是第 k-1 个 beat 的 px2,px3 (用于左侧第 6/5 列)
    //==================================================================
    reg [DW-1:0] cur;          // 行 n   第 k 拍
    reg [15:0]   cur_p23;
    reg [15:0]   r1_p23;
    reg [15:0]   r2_p23;
    reg [11:0]   row_d1;       // 与 cur 同拍的所在行号
    reg          beat0_d1;     // 与 cur 同拍: 是否为行内第 0 拍

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

            // 始终寄存"上一拍"的 px2,px3 (行首时会被下面的 mux 覆盖, 值不重要)
            // 注意字节序: 存成 {px2, px3}, 使 p23[15:8]=px2(列 4k-2), p23[7:0]=px3(列 4k-1)
            if(vld_d1)begin
            cur_p23 <= {cur[23:16],    cur[31:24]   };
            r1_p23  <= {d_row1[23:16], d_row1[31:24]};
            r2_p23  <= {d_row2[23:16], d_row2[31:24]};
            end
        end
    end

    // 行内第 0 拍: 左边沿用本拍 px0 复制 (必须用 t+1 拍刚读到的数据)
    wire [15:0] w0_p23 = beat0_d1 ? {cur[7:0],    cur[7:0]   } : cur_p23;
    wire [15:0] w1_p23 = beat0_d1 ? {d_row1[7:0], d_row1[7:0]} : r1_p23;
    wire [15:0] w2_p23 = beat0_d1 ? {d_row2[7:0], d_row2[7:0]} : r2_p23;

    //==================================================================
    // 6. 6 像素窗 (w0 = 列 4k-2 ... w5 = 列 4k+3)
    //==================================================================
    wire [47:0] win0 = {cur[31:24],    cur[23:16],    cur[15: 8],    cur[ 7: 0],    w0_p23[ 7: 0], w0_p23[15: 8]};
    wire [47:0] win1 = {d_row1[31:24], d_row1[23:16], d_row1[15: 8], d_row1[ 7: 0], w1_p23[ 7: 0], w1_p23[15: 8]};
    wire [47:0] win2 = {d_row2[31:24], d_row2[23:16], d_row2[15: 8], d_row2[ 7: 0], w2_p23[ 7: 0], w2_p23[15: 8]};

    //==================================================================
    // 7. 水平 3 抽头: H[c] = w[c-1] + 2*w[c] + w[c+1], c = 1..4
    //==================================================================
    function [9:0] f_h3;
        input [7:0] a;      // 左
        input [7:0] b;      // 中
        input [7:0] c;      // 右
        reg   [9:0] t;
        begin
            t    = {1'b0, a} + {b, 1'b0} + {1'b0, c};   // a + 2b + c
            f_h3 = t;
        end
    endfunction

    wire [9:0] h0_1 = f_h3(win0[ 7: 0], win0[15: 8], win0[23:16]);
    wire [9:0] h0_2 = f_h3(win0[15: 8], win0[23:16], win0[31:24]);
    wire [9:0] h0_3 = f_h3(win0[23:16], win0[31:24], win0[39:32]);
    wire [9:0] h0_4 = f_h3(win0[31:24], win0[39:32], win0[47:40]);

    wire [9:0] h1_1 = f_h3(win1[ 7: 0], win1[15: 8], win1[23:16]);
    wire [9:0] h1_2 = f_h3(win1[15: 8], win1[23:16], win1[31:24]);
    wire [9:0] h1_3 = f_h3(win1[23:16], win1[31:24], win1[39:32]);
    wire [9:0] h1_4 = f_h3(win1[31:24], win1[39:32], win1[47:40]);

    wire [9:0] h2_1 = f_h3(win2[ 7: 0], win2[15: 8], win2[23:16]);
    wire [9:0] h2_2 = f_h3(win2[15: 8], win2[23:16], win2[31:24]);
    wire [9:0] h2_3 = f_h3(win2[23:16], win2[31:24], win2[39:32]);
    wire [9:0] h2_4 = f_h3(win2[31:24], win2[39:32], win2[47:40]);

    //==================================================================
    // 8. 垂直: out = (H_top + 2*H_mid + H_bot) >> 4
    //    win2 = 行 n-2 (上), win1 = 行 n-1 (中), win0 = 行 n (下)
    //==================================================================
    function [7:0] f_v3;
        input [9:0] top;
        input [9:0] mid;
        input [9:0] bot;
        reg   [11:0] s;
        begin
            s    = {2'b00, top} + {mid, 1'b0} + {2'b00, bot};
            f_v3 = s[11:4];
        end
    endfunction


    // Split RAM/mux -> horizontal sums -> vertical sums for 150 MHz.
    reg [9:0] h0r[1:4],h1r[1:4],h2r[1:4];
    reg [31:0] cur_d2;
    reg warm_d2,vld_d2,lst_d2,usr_d2;
    always @(posedge I_clk)begin
      if(!I_rst_n)begin
        vld_d2<=0;lst_d2<=0;usr_d2<=0;warm_d2<=0;cur_d2<=0;
      end else begin
        vld_d2<=vld_d1;lst_d2<=lst_d1;usr_d2<=usr_d1;
        warm_d2<=row_d1<2;cur_d2<=cur;
      end
      h0r[1]<=h0_1;h1r[1]<=h1_1;h2r[1]<=h2_1;
      h0r[2]<=h0_2;h1r[2]<=h1_2;h2r[2]<=h2_2;
      h0r[3]<=h0_3;h1r[3]<=h1_3;h2r[3]<=h2_3;
      h0r[4]<=h0_4;h1r[4]<=h1_4;h2r[4]<=h2_4;
    end
    wire [7:0] g0 = f_v3(h2r[1], h1r[1], h0r[1]);
    wire [7:0] g1 = f_v3(h2r[2], h1r[2], h0r[2]);
    wire [7:0] g2 = f_v3(h2r[3], h1r[3], h0r[3]);
    wire [7:0] g3 = f_v3(h2r[4], h1r[4], h0r[4]);

    //==================================================================
    // 9. 输出级 (第 2 拍): 控制信号与数据同时对齐
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

            O_tvalid <= vld_d2;
            O_tlast  <= lst_d2;
            O_tuser  <= usr_d2;
            // 行 < 2 没有完整垂直窗 -> 灰度直通
            O_tdata  <= warm_d2 ? cur_d2 : {g3, g2, g1, g0};
        end
    end

`ifdef PRE_DEBUG
    integer fd_dbg;
    initial fd_dbg = $fopen("vectors/dbg_gauss.txt", "w");
    always @(posedge I_clk) begin
        if (I_rst_n && vld_d1) begin
            $fdisplay(fd_dbg, "%0d %0d %08x %08x %08x %04x %04x %04x",
                row_d1, beat_idx, cur, d_row1, d_row2, w0_p23, w1_p23, w2_p23);
        end
    end
`endif

endmodule
