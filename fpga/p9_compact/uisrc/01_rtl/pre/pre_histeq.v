`timescale 1ns/1ps
/*******************************************************************************
 * 文件名   : pre_histeq.v
 * 所属工程 : p1_preprocess
 * 功能     : 直方图均衡 (帧间统计 -> LUT -> 下一帧应用), 4 像素/时钟
 *
 * 架构 (不停流, 不需要同帧两遍扫描):
 *   帧 N   : 用 LUT(N-1) 输出, 同时统计帧 N 的直方图
 *   帧 N 末: 冻结统计, 用约 257 拍把 直方图 -> CDF -> LUT, 顺便把直方图清零
 *   帧 N+1 : 用新 LUT 输出, 同时统计帧 N+1
 *
 * 避免除法器的关键简化:
 *   LUT[k] = min(255, CDF[k] >> SHIFT)
 *   SHIFT 取 "总像素数 >> SHIFT ≈ 255":
 *     1920x1080 = 2,073,600 像素 -> SHIFT=13 (8192) -> 253, 与 255 差 0.8%
 *     小图/仿真请改小 (TB 用 64x8 图, SHIFT=1)
 *
 * 4 路并行:
 *   - P6四块直方图RAM (256 x 19bit)，每lane支持518400次同灰度计数。
 *     地址 = 本路像素值, 因此同一块内不存在同拍多地址冲突。
 *   - 4 块 LUT ERAM (256 x 8bit), 生成阶段四块同址并行写, 正常显示时 4 路各读各的。
 *   - 生成阶段停止统计, 所以直方图 RAM 的读写不会冲突。
 *
 * 帧同步: beat_cnt 在 tuser (帧首) 清零, 数到 FRAME_BEATS-1 时启动 LUT 生成,
 *         保证统计窗口正好是一帧, 不受上电时刻相位影响。
 * 首帧行为: LUT 未建立前直通灰度 (lut_ok=0), 从第二帧起才做均衡。
 * 时序: 固定延迟 2 拍 (LUT 读 1 拍 + 输出寄存 1 拍)。
 ******************************************************************************/
module pre_histeq #(
    parameter integer BEATS_PER_LINE = 480,      // 1920/4
    parameter integer FRAME_LINES    = 1080,
    parameter integer SHIFT          = 13
) (
    input  wire        I_clk,
    input  wire        I_rst_n,

    input  wire        I_tvalid,
    input  wire        I_tlast,
    input  wire        I_tuser,
    input  wire [31:0] I_tdata,        // 4 x 8bit 灰度, px0 = [7:0]

    output reg         O_tvalid,
    output reg         O_tlast,
    output reg         O_tuser,
    output reg  [31:0] O_tdata
);

    localparam integer FRAME_BEATS = BEATS_PER_LINE * FRAME_LINES;

    wire [7:0] g0 = I_tdata[ 7: 0];
    wire [7:0] g1 = I_tdata[15: 8];
    wire [7:0] g2 = I_tdata[23:16];
    wire [7:0] g3 = I_tdata[31:24];

    //==================================================================
    // 1. 直方图 RAM (4 块) / LUT RAM (4 块)
    //==================================================================
    // Each lane can contribute all 518400 pixels to one bin in a flat frame.
    reg [18:0] h0 [0:255];
    reg [18:0] h1 [0:255];
    reg [18:0] h2 [0:255];
    reg [18:0] h3 [0:255];

    reg [7:0]  l0 [0:255];
    reg [7:0]  l1 [0:255];
    reg [7:0]  l2 [0:255];
    reg [7:0]  l3 [0:255];

    //==================================================================
    // 2. 状态
    //==================================================================
    reg        gen;            // 1 = 正在生成 LUT (停止统计)
    reg [8:0]  gk;             // 0..256
    reg [31:0] beat_cnt;       // 帧内 beat 计数
    reg [21:0] cdf;            // 累积像素数
    reg        lut_ok;         // LUT 是否已建立
    reg        clr_done;       // 上电清零是否完成
    reg [8:0]  ck;             // 清零计数 0..256

    reg [18:0] s0, s1, s2, s3; // 直方图读数据 (1 拍延迟)

    wire       counting  = !gen && clr_done;
    wire       frame_end = (beat_cnt == FRAME_BEATS - 1);

    //==================================================================
    // 3. 统计 (仅在 counting 时递增; 每块每拍一个地址, 读改写安全)
    //==================================================================
    // 注意: 直方图/ LUT 的写端口必须**集中在一个 always 块**里。
    // 早期版本把"统计递增"和"生成阶段清零"分成两个 always 块, 同一个数组被
    // 两处驱动, 仿真里计数被清零覆盖(h 数组一直是 X), 综合也会报多驱动。
    always @(posedge I_clk) begin
        if (!clr_done) begin
            // 上电清零: 直方图 RAM 未初始化时读出来是 X, 而"从未出现过的灰度值"
            // 这一帧根本不会被写, 读到的仍是 X, 会把 CDF 全污染成 X。
            // (踩过这个坑: 测试图里恰好没有 0 灰度, h[0] 一直是 X)
            h0[ck[7:0]] <= 16'd0;
            h1[ck[7:0]] <= 16'd0;
            h2[ck[7:0]] <= 16'd0;
            h3[ck[7:0]] <= 16'd0;
        end else if (counting && I_tvalid) begin
            h0[g0] <= h0[g0] + 16'd1;
            h1[g1] <= h1[g1] + 16'd1;
            h2[g2] <= h2[g2] + 16'd1;
            h3[g3] <= h3[g3] + 16'd1;
        end else if (gen && (gk > 9'd0)) begin
            // 生成阶段: 写 LUT, 同时把直方图清零为下一帧做准备
            l0[waddr] <= lut_val;
            l1[waddr] <= lut_val;
            l2[waddr] <= lut_val;
            l3[waddr] <= lut_val;
            h0[waddr] <= 16'd0;
            h1[waddr] <= 16'd0;
            h2[waddr] <= 16'd0;
            h3[waddr] <= 16'd0;
        end
    end

    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            clr_done <= 1'b0;
            ck       <= 9'd0;
        end else if (!clr_done) begin
            if (ck == 9'd255) clr_done <= 1'b1;
            else              ck       <= ck + 9'd1;
        end
    end

    //==================================================================
    // 4. CDF / LUT 生成流水
    //    cycle gk=j      : 发读地址 j
    //    cycle gk=j+1    : s* = hist[j] -> cdf += s -> LUT[j] = min(255,cdf>>SHIFT)
    //                      同时把 hist[j] 清零
    //==================================================================
    wire [20:0] hsum    = {2'b00, s0} + {2'b00, s1} + {2'b00, s2} + {2'b00, s3};
    wire [21:0] cdf_new = cdf + {1'd0, hsum};
    wire [7:0]  lut_val = ((cdf_new >> SHIFT) > 22'd255) ? 8'd255
                                                         : (cdf_new >> SHIFT);
    wire [7:0]  waddr   = gk[7:0] - 8'd1;      // 写回上一项

    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            gen      <= 1'b0;
            gk       <= 9'd0;
            beat_cnt <= 32'd0;
            cdf      <= 22'd0;
            lut_ok   <= 1'b0;
            s0 <= 16'd0; s1 <= 16'd0; s2 <= 16'd0; s3 <= 16'd0;
        end else if (!gen) begin
            //---------------- 统计阶段 ----------------
            // 注意顺序: 先判 frame_end 再判 tuser。
            // beat_cnt 在帧首清零, 一帧 N 个 beat 后它正好等于 N-1,
            // 而下一帧首个 beat 同时也带 tuser; 若先判 tuser 就会把
            // frame_end 吃掉, 导致 LUT 永远不生成(踩过这个坑)。
            // frame_end 不依赖 tvalid: 一帧最后一个 beat 处理完的下一拍就启动
            // LUT 生成, 正好落在场消隐期间(真实视频有 ~3 万拍的空档)。
            // 若等"下一个有效 beat"再启动, 生成过程会和下一帧的有效像素抢时间。
            if (frame_end) begin
                beat_cnt <= 32'd0;
                gen      <= 1'b1;
                gk       <= 9'd0;
                cdf      <= 22'd0;
            end else if (I_tvalid) begin
                if (I_tuser) begin
                    beat_cnt <= 32'd0;                       // 帧首同步
                end else begin
                    beat_cnt <= beat_cnt + 32'd1;
                end
            end
        end else begin
            //---------------- 生成阶段 ----------------
            if (gk < 9'd256) begin
                s0 <= h0[gk[7:0]];
                s1 <= h1[gk[7:0]];
                s2 <= h2[gk[7:0]];
                s3 <= h3[gk[7:0]];
            end

            if (gk > 9'd0) begin
                cdf <= cdf_new;      // RAM 写在上面那个统一的 always 块里
            end

            if (gk == 9'd256) begin
                gen    <= 1'b0;
                gk     <= 9'd0;
                lut_ok <= 1'b1;
            end else begin
                gk <= gk + 9'd1;
            end
        end
    end

    //==================================================================
    // 5. 输出通路: LUT 读 (1 拍) + 输出寄存 (1 拍)
    //==================================================================
    reg [7:0] q0, q1, q2, q3;

    always @(posedge I_clk) begin
        q0 <= l0[g0];
        q1 <= l1[g1];
        q2 <= l2[g2];
        q3 <= l3[g3];
    end

    reg        vld_d1, lst_d1, usr_d1, ok_d1;
    reg [31:0] g_d1;

    always @(posedge I_clk) begin
        if (!I_rst_n) begin
            vld_d1 <= 1'b0;
            lst_d1 <= 1'b0;
            usr_d1 <= 1'b0;
            ok_d1  <= 1'b0;
            g_d1   <= 32'd0;
            O_tvalid <= 1'b0;
            O_tlast  <= 1'b0;
            O_tuser  <= 1'b0;
            O_tdata  <= 32'd0;
        end else begin
            vld_d1 <= I_tvalid;
            lst_d1 <= I_tlast;
            usr_d1 <= I_tuser;
            ok_d1  <= lut_ok;
            g_d1   <= I_tdata;

            O_tvalid <= vld_d1;
            O_tlast  <= lst_d1;
            O_tuser  <= usr_d1;
            O_tdata  <= ok_d1 ? {q3, q2, q1, q0} : g_d1;   // LUT 未建立时直通
        end
    end

`ifdef HEQ_DEBUG
    reg gen_d;
    always @(posedge I_clk) begin
        gen_d <= gen;
        if (I_rst_n && gen && !gen_d)
            $display("[gen START] beat_cnt=%0d", beat_cnt);
        if (I_rst_n && gen && (gk < 9'd3 || gk > 9'd253))
            $display("  gk=%0d s=%04x %04x %04x %04x cdf=%0d lut=%02x ok=%b",
                     gk, s0, s1, s2, s3, cdf, lut_val, lut_ok);
        if (I_rst_n && !gen && gen_d)
            $display("[gen END] ok=%b", lut_ok);
    end
`endif

endmodule
