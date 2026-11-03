`timescale 1ns/1ps
// Four grayscale pixels/beat -> 16-byte DMA words. Keep frame admission
// and overflow handling bounded; never report an incomplete frame as good.
module gray_dma_packer(
 input wire clk,rstn,enable,bad,valid,user,last,
 input wire [127:0] rgb,
 input wire [2:0] mode,
 input wire [7:0] seq,
 input wire full,
 input wire [12:0] level,
 output reg push,
 output reg [143:0] word
);
 reg active,aborting,error;
 reg [1:0] slot;
 reg [19:0] beats;
 reg [8:0] column;
 reg [127:0] pack,tmp;
 reg [2:0] held_mode;reg [7:0] held_seq;
 wire [31:0] gray={rgb[103:96],rgb[71:64],rgb[39:32],rgb[7:0]};
 always @(posedge clk) begin
   push<=0;
   if(!rstn) begin
     active<=0;aborting<=0;error<=0;slot<=0;beats<=0;column<=0;pack<=0;word<=0;held_mode<=0;held_seq<=0;
   end else begin
     if(bad && active) error<=1;
     if(aborting && !full) begin
       push<=1;word<={1'b1,1'b0,3'b010,held_mode,held_seq,128'd0};aborting<=0;
     end else if(valid) begin
       if(user) begin
         if(active)begin active<=0;aborting<=1;end
         else begin
           active<=enable && level<1024 && !aborting;
           slot<=1;beats<=1;column<=1;pack<={96'd0,gray};error<=bad|last;
           held_mode<=mode;held_seq<=seq;
         end
       end else if(active) begin
         column<=(column==479)?0:column+1;
         if(last!=(column==479))error<=1;
         tmp=pack;tmp[slot*32+:32]=gray;pack<=tmp;slot<=slot+1;beats<=beats+1;
         if(slot==3) begin
           if(full || level>=4080) begin active<=0;aborting<=1;end
           else begin
             push<=1;
             word<={(beats==518399),(beats==3),
                    ((beats==518399)?{!(error|bad|!last),(error|bad|!last),1'b0,held_mode,held_seq}:14'd0),tmp};
           end
         end
         if(beats==518399)active<=0;
       end
     end
   end
 end
endmodule
