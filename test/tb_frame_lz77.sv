`timescale 1ns/1ps
module tb_frame_lz77;
  reg clk=0;always #3.333 clk=~clk;
  reg rstn=0,iv=0,full=0;reg[143:0] iw=0;reg[15:0] cfg=1;
  wire ov;wire[143:0] ow;
  parameter SIZE=2432*1097;
  parameter CAM=0,PRE=0;
  frame_lz77 #(.CAMERA(CAM),.RAW_SIZE(SIZE),.GRAY_SIZE(SIZE)) dut(.clk(clk),.rstn(rstn),.config_word(cfg),.pre_mode(3'(PRE)),.frame_id(32'(f+1)),.input_valid(iv),.input_word(iw),.full(full),.push(ov),.word(ow));
  integer file,src,i,b,counter=0,frame=0,stall_seed=1121,initial_cfg=1,frames=1,f,stalls=0,tick=0;
  reg[127:0] data;
  reg[1023:0] infile,outfile;
  always @(negedge clk)if(ov)begin
    for(b=0;b<16;b=b+1)$fwrite(file,"%c",ow[b*8+:8]);
    counter=counter+16;
    if(ow[143])begin
      // Same legacy trailer as vdma_c2h_reader (little endian).
      for(b=0;b<4;b=b+1)$fwrite(file,"%c",(32'h54373056>>(b*8))&255);
      for(b=0;b<4;b=b+1)$fwrite(file,"%c",(counter>>(b*8))&255);
      for(b=0;b<4;b=b+1)$fwrite(file,"%c",((32'h01000000|(CAM<<16)|ow[141:128])>>(b*8))&255);
      for(b=0;b<4;b=b+1)$fwrite(file,"%c",((~counter)>>(b*8))&255);
      $display("FRAME_BYTES %d",counter+16);frame=frame+1;counter=0;
    end
  end
  initial begin
    if(!$value$plusargs("input=%s",infile))$fatal(1,"input path");
    if(!$value$plusargs("output=%s",outfile))$fatal(1,"output path");
    if($value$plusargs("config=%d",initial_cfg))begin end
    if($value$plusargs("frames=%d",frames))begin end
    if($value$plusargs("stalls=%d",stalls))begin end
    cfg=initial_cfg;file=$fopen(outfile,"wb");
    repeat(5)@(negedge clk);rstn=1;
    for(f=0;f<frames;f=f+1)begin
    src=$fopen(infile,"rb");
    for(i=0;i<SIZE/16;i=i+1)begin
      for(b=0;b<16;b=b+1)begin data[b*8+:8]=$fgetc(src);end
      @(negedge clk);iv=1;iw={i==SIZE/16-1,i==0,3'b100,3'(PRE),8'h01,data};
      // Config changes in the middle MUST NOT affect the current frame.
      if(i==SIZE/32)cfg=16'h0023;
      @(negedge clk);iv=0;
      repeat(2)@(negedge clk);
      // Camera line bursts, plus realistic blanking.
      if(i%152==151)repeat(1600)@(negedge clk);
    end
    $fclose(src);
    wait(frame==f+1);repeat(16)@(negedge clk);
    end
    $fclose(file);
    $display("LZ77 FULL FRAME PASS cycles=%0t",$time);$finish;
  end
  always @(posedge clk)begin
    tick<=tick+1;
    if(stalls==2)full<=f==0 && i<SIZE/16;
    else full<=stalls!=0 && (tick%17<3);
  end
  initial begin #200000000;$fatal(1,"timeout state=%d remaining=%d ready=%h",dut.state,dut.remaining,dut.ready);end
endmodule
