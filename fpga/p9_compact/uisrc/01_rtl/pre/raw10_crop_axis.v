`timescale 1ns/1ps
// Unpack CSI payload bytes; crop the documented 1944x1097 RAW area to
// x=4..1923,y=16..1095. Four ten-bit samples per output beat.
module raw10_crop_axis(
 input wire clk,rstn,fs,valid,first,
 input wire [2:0] bytes,
 input wire [31:0] data,
 output reg v,last,user,
 output reg [39:0] pixels
);
 reg [95:0] acc;reg [3:0] count;
 reg [10:0] next_y,y;reg [9:0] group;
 reg [95:0] temp;reg [3:0] available;
 integer k;
 always @(posedge clk) begin
   if(!rstn || fs) begin
     acc<=0;count<=0;next_y<=0;y<=0;group<=0;
     v<=0;last<=0;user<=0;pixels<=0;
   end else begin
     v<=0;last<=0;user<=0;
     temp=acc;available=count;
     if(count>=5) begin
       if(y>=16 && y<1096 && group>=1 && group<=480) begin
         v<=1;last<=(group==480);user<=(group==1 && y==16);
         for(k=0;k<4;k=k+1) pixels[k*10+:10]<={acc[k*8+:8],acc[32+k*2+:2]};
       end
       temp=acc>>40;available=count-5;group<=group+1;
     end
     if(valid) begin
       if(first) begin
         temp=0;available=0;y<=next_y;next_y<=next_y+1;group<=0;
       end
       for(k=0;k<4;k=k+1) if(k<bytes) temp[(available+k)*8+:8]=data[k*8+:8];
       available=available+bytes;
     end
     acc<=temp;count<=available;
   end
 end
endmodule
