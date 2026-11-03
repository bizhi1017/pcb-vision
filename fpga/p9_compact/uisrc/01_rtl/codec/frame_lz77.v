// P9: original LZ77 modes 0/1/2 preserved. Mode 3 is explicit lossy packing:
// color RAW10 -> top 6 bits/pixel; processed gray8 -> top 5 bits/pixel.
// Every source byte, including row padding, is reconstructed and CRC checked.
// 4-bank streaming scratch RAM, NOT strip subsampling or cross-frame coverage.
// Input: legacy 16-byte packer beats, at most one per four RX clocks.
// CRC covers reconstructed (possibly explicitly quantized) bytes. Parameters
// latch only on an admitted SOF. Congestion produces an invalid terminated frame.
module frame_lz77 #(
  parameter [7:0] CAMERA=0, parameter RAW_SIZE=2432*1097, parameter GRAY_SIZE=1920*1080,
  parameter ROW_BEATS=152, parameter LINE_BYTES=2430, parameter HASH_BITS=11
)(
  input wire I_4k,
  input wire clk,rstn,input wire [15:0] config_word,input wire [2:0] pre_mode,
  input wire input_valid,input wire [143:0] input_word,input wire full,input wire [31:0] frame_id,
  output wire push,output wire [143:0] word
);
  function [31:0] crc_word;
    input [31:0] crc,data;reg[31:0] r;integer i;
    begin r=crc^data;for(i=0;i<32;i=i+1)r=r[0]?(r>>1)^32'hedb88320:r>>1;crc_word=r;end
  endfunction
  function [HASH_BITS-1:0] hash;
    input[31:0] d;begin hash=(d^(d>>11)^(d>>22));end
  endfunction
  function [127:0] quant;
    input[127:0] d;input[1:0] m;input[2:0] p;input[8:0] beat;input[2:0] phase;
    integer i;reg[127:0] v;reg[7:0] b;
    begin
      v=d;
      for(i=0;i<16;i=i+1)begin
        b=d[i*8+:8];
        if(m>=2)begin
          if(p!=0)b=b & (m==2?8'hfe:8'hf8);
          else if((beat*16+i)<(I_4k ? 4830 : LINE_BYTES))begin
            if(((phase+i)%5)==4)b=b & (m==2?8'haa:8'h00);
            else if(m==3)b=b & 8'hfc;
          end
        end
        v[i*8+:8]=b;
      end
      quant=v;
    end
  endfunction

  // LSB-first compact token. Up to four source bytes are accepted per clock.
  // Low RAW10 bit planes are omitted, NOT sent as zero-filled bytes.
  function [37:0] compact;
    input[31:0] d;input[2:0] p,phase;input[12:0] offset;
    integer i,n,k;reg[31:0] value;reg[7:0] b;
    begin
      value=0;n=0;
      for(i=0;i<4;i=i+1)begin
        b=d[i*8+:8];
        if(p!=0)begin b=b>>3;k=5;end
        else if(offset+i>=(I_4k ? 4830 : LINE_BYTES))k=8;
        else if((phase+i)%5==4)begin b=0;k=0;end
        else begin b=b>>2;k=6;end
        value=value | ({24'd0,b}<<n);n=n+k;
      end
      compact={n[5:0],value};
    end
  endfunction

  reg busy,capturing,input_done,failed;
  reg[1:0] mode;reg[2:0] pre;reg[13:0] epoch,tail_status;
  reg[31:0] frame_seq,raw_size,crc;
  reg[31:0] wire_crc[0:3];
  reg[8:0] row_beat;reg[2:0] row_phase;
  reg[127:0] serial;reg[2:0] remaining;reg serial_last;
  reg[12:0] serial_offset;reg[2:0] serial_phase;
  reg[37:0] pcompact,ccompact,scompact;
  reg[21:0] position;
  reg pvalid,plast;reg[31:0] pdata;reg[21:0] ppos;
  reg[HASH_BITS-1:0] phash;
  (* ram_style="block" *) reg[53:0] dictionary[0:(1<<HASH_BITS)-1];
  reg[(1<<HASH_BITS)-1:0] dict_valid;
  reg[53:0] lookup;
  reg prev_valid;reg[HASH_BITS-1:0] prev_hash;reg[53:0] prev_entry;
  wire[53:0] candidate=(prev_valid && prev_hash==phash)?prev_entry:lookup;
  reg cvalid,clast,centry_valid;reg[31:0] cdata;reg[21:0] cpos;reg[53:0] centry;
  wire[21:0] distance=cpos-centry[53:32];
  wire matched=mode!=0 && centry_valid && centry[31:0]==cdata && distance!=0 && distance<=65535;
  reg svalid,slast,smatched;reg[31:0] sdata;reg[15:0] sdistance;
  wire admitted=input_valid && input_word[142] && !busy;
  wire unexpected_sof=input_valid && input_word[142] && busy && capturing;
  wire accepted=input_valid && !unexpected_sof && ((capturing && !input_done) || admitted);
  wire overrun=accepted && remaining>1;
  wire writing=svalid && !failed;
  reg[1:0] wb,rb;
  reg[7:0] wi;reg[8:0] match_count;
  (* ram_style="block" *) reg[47:0] tokens[0:1023];
  reg[255:0] flags[0:3];
  reg[8:0] counts[0:3],bank_matches[0:3];
  reg[13:0] bank_bits[0:3],compact_count,stored_bits;
  reg[3:0] ready;
  wire chunk_finished=writing && (wi==255 || slast);

  localparam IDLE=0,HCRC=1,H0=2,H1=3,WAIT=4,CH=5,F0=6,F1=7,RD=8,PACK=9,DRAIN=10,FOOT=11,FOOT2=12;
  reg[3:0] state;
  reg[2:0] hi;reg[31:0] hcrc;
  reg[8:0] ri,nwords,nmatches;
  reg compressed;reg[31:0] chunk_index;
  reg[47:0] token;
  reg[255:0] bucket;
  reg[5:0] nbytes;
  reg[8:0] nbits;
  reg[31:0] hd;
  wire[7:0] version=mode==3 ? 8'd2 : 8'd1;
  wire[127:0] head0={frame_seq,raw_size,version,CAMERA,5'd0,pre,6'd0,mode,32'h31433756};
  wire[127:0] head1={~hcrc,32'd0,32'd1024,18'd0,epoch};
  wire[31:0] stored=mode==3 ? ((stored_bits+7)>>3) :
                         (nwords<<2)-(compressed?(nmatches<<1):0)+(compressed?32:0);
  wire output_valid=(state==H0 || state==H1 || state==CH || state==F0 || state==F1 || state==FOOT || state==FOOT2 ||
                    (state==PACK && (mode==3 ? nbits>=128 : nbytes>=16)) ||
                    (state==DRAIN && (mode==3 ? nbits!=0 : nbytes!=0)));
  reg[127:0] output_data;
  wire fire=output_valid && !full;
  wire release_bank=state==DRAIN && (mode==3 ? nbits==0 : nbytes==0);
  // One synchronous RAM token per clock. Output drains and token insertion
  // overlap, so an uninterrupted full 4K line cannot outrun the bank reader.
  wire take_token=state==PACK && (mode==3 ? (nbits<128 || fire) : (nbytes<16 || fire));
  wire [255:0] token_bucket=fire ? (bucket>>128) : bucket;
  wire [5:0] token_bytes=fire ? nbytes-6'd16 : nbytes;
  wire [8:0] token_bits=fire ? nbits-9'd128 : nbits;
  wire [7:0] next_ri=ri[7:0]+8'd1;
  wire[31:0] footer_crc=~crc;
  wire[13:0] status={tail_status[13:8],frame_seq[7:0]} | (failed ? 14'h1000 : 14'd0);
  wire[31:0] chunk_raw={21'd0,nwords,2'd0};
  assign push=fire;
  assign word={state==FOOT2,state==H0,status,output_data};
  always @* begin
    case(hi)
      0:hd=32'h31433756;
      1:hd={version,CAMERA,5'd0,pre,6'd0,mode};
      2:hd=raw_size;3:hd=frame_seq;4:hd={18'd0,epoch};5:hd=1024;default:hd=0;
    endcase
    case(state)
      H0:output_data=head0;
      H1:output_data=head1;
      CH:output_data={chunk_index,(mode==3 ? (32'hc0000000|stored) : {compressed,stored[30:0]}),chunk_raw,32'h4b4e4843};
      F0:output_data=flags[rb][127:0];
      F1:output_data=flags[rb][255:128];
      FOOT:output_data={{18'd0,epoch},raw_size,footer_crc,32'h31453756};
      FOOT2:output_data={~wire_crc[3],~wire_crc[2],~wire_crc[1],~wire_crc[0]};
      default:output_data=bucket[127:0];
    endcase
  end
  integer j;
  // RAM port processes deliberately have no reset on their data registers.
  // Reset/valid state qualifies their outputs; this infers synchronous ERAM.
  always @(posedge clk)begin
    if(rstn && remaining!=0 && !failed && !admitted)lookup<=dictionary[hash(serial[31:0])];
    if(rstn && pvalid && !failed)dictionary[phash]<={ppos,pdata};
    if(rstn && writing && !ready[wb])tokens[{wb,wi}]<=mode==3 ? {10'd0,scompact} : {sdistance,sdata};
    if(state==RD)token<=tokens[{rb,ri[7:0]}];
    else if(take_token && ri+1<nwords)token<=tokens[{rb,next_ri}];
  end
  always @(posedge clk)begin
    if(!rstn)begin
      busy<=0;capturing<=0;input_done<=0;failed<=0;frame_seq<=0;
      mode<=1;epoch<=0;pre<=0;raw_size<=RAW_SIZE;tail_status<=0;crc<=32'hffffffff;
      remaining<=0;serial<=0;serial_last<=0;position<=0;pvalid<=0;plast<=0;
      pdata<=0;ppos<=0;phash<=0;dict_valid<=0;prev_valid<=0;svalid<=0;slast<=0;smatched<=0;sdata<=0;sdistance<=0;
      cvalid<=0;clast<=0;centry_valid<=0;cdata<=0;cpos<=0;centry<=0;
      wb<=0;rb<=0;wi<=0;match_count<=0;ready<=0;row_beat<=0;row_phase<=0;
      state<=IDLE;hi<=0;hcrc<=32'hffffffff;ri<=0;nwords<=0;nmatches<=0;
      compressed<=0;chunk_index<=0;bucket<=0;nbytes<=0;
      nbits<=0;serial_offset<=0;serial_phase<=0;pcompact<=0;ccompact<=0;scompact<=0;
      compact_count<=0;stored_bits<=0;
      for(j=0;j<4;j=j+1)bank_bits[j]<=0;
      for(j=0;j<4;j=j+1)begin flags[j]<=0;counts[j]<=0;bank_matches[j]<=0;wire_crc[j]<=32'hffffffff;end
    end else begin
      if(fire && state!=FOOT && state!=FOOT2)for(j=0;j<4;j=j+1)wire_crc[j]<=crc_word(wire_crc[j],output_data[j*32+:32]);
      pvalid<=0;prev_valid<=pvalid && !failed;
      cvalid<=pvalid && !failed;clast<=plast;cdata<=pdata;cpos<=ppos;centry<=candidate;centry_valid<=dict_valid[phash];
      svalid<=cvalid && !failed;slast<=clast;sdata<=cdata;sdistance<=distance[15:0];smatched<=matched;
      ccompact<=pcompact;scompact<=ccompact;
      if(pvalid && !failed)begin dict_valid[phash]<=1;prev_hash<=phash;prev_entry<={ppos,pdata};end
      if(admitted)begin
        busy<=1;capturing<=1;input_done<=0;failed<=0;mode<=config_word[1:0];epoch<=config_word[15:2];pre<=pre_mode;
        frame_seq<=frame_id;raw_size<=pre_mode==0?(I_4k ? 4832*2192 : RAW_SIZE):GRAY_SIZE;
        crc<=32'hffffffff;position<=0;dict_valid<=0;prev_valid<=0;svalid<=0;cvalid<=0;
        wb<=0;rb<=0;wi<=0;match_count<=0;ready<=0;chunk_index<=0;
        compact_count<=0;nbits<=0;serial_offset<=0;serial_phase<=0;
        for(j=0;j<4;j=j+1)begin flags[j]<=0;wire_crc[j]<=32'hffffffff;end
        row_beat<=1;row_phase<=1;
        state<=HCRC;hi<=0;hcrc<=32'hffffffff;
        serial<=quant(input_word[127:0],config_word[1:0],pre_mode,0,0);remaining<=4;serial_last<=input_word[143];
      end else begin
        if(remaining!=0 && !failed)begin
          pvalid<=1;pdata<=serial[31:0];ppos<=position;phash<=hash(serial[31:0]);
          pcompact<=compact(serial[31:0],pre,serial_phase,serial_offset);
          serial_offset<=serial_offset+4;serial_phase<=serial_phase==0 ? 4 : serial_phase-1;
          plast<=serial_last && remaining==1;serial<=serial>>32;remaining<=remaining-1;position<=position+1;
        end
        if(accepted && !overrun)begin
          serial<=quant(input_word[127:0],mode,pre,row_beat,row_phase);remaining<=4;serial_last<=input_word[143];
          serial_offset<={row_beat,4'b0000};serial_phase<=row_phase;
          if(row_beat==(I_4k ? 302 : ROW_BEATS)-1)begin row_beat<=0;row_phase<=0;end
          else begin row_beat<=row_beat+1;row_phase<=row_phase==4?0:row_phase+1;end
        end
      end
      if(accepted && input_word[143])begin
        capturing<=0;tail_status<=input_word[141:128];
        if(input_word[141:139]!=3'b100)begin failed<=1;input_done<=1;remaining<=0;pvalid<=0;svalid<=0;cvalid<=0;end
      end
      if(unexpected_sof || overrun || (writing && ready[wb]))begin
        failed<=1;capturing<=0;input_done<=1;remaining<=0;pvalid<=0;svalid<=0;cvalid<=0;
      end
      if(writing && !ready[wb])begin
        flags[wb][wi]<=smatched;
        crc<=crc_word(crc,sdata);
        if(chunk_finished)begin
          counts[wb]<={1'b0,wi}+1;bank_matches[wb]<=match_count+smatched;ready[wb]<=1;
          bank_bits[wb]<=compact_count+scompact[37:32];compact_count<=0;
          wb<=wb+1;wi<=0;match_count<=0;
        end else begin wi<=wi+1;match_count<=match_count+smatched;compact_count<=compact_count+scompact[37:32];end
        if(slast)input_done<=1;
      end
      if(!admitted)case(state)
        IDLE:begin end
        HCRC:begin hcrc<=crc_word(hcrc,hd);if(hi==6)state<=H0;else hi<=hi+1;end
        H0:if(fire)state<=H1;
        H1:if(fire)state<=WAIT;
        WAIT:begin
          if(ready[rb])begin
            nwords<=counts[rb];nmatches<=bank_matches[rb];compressed<=bank_matches[rb]>16;
            stored_bits<=bank_bits[rb];state<=CH;ri<=0;bucket<=0;nbytes<=0;nbits<=0;
          end else if(input_done && ready==0 && !pvalid && !cvalid && !svalid && remaining==0)state<=FOOT;
        end
        CH:if(fire)state<=mode==3 ? RD : (compressed?F0:RD);
        F0:if(fire)state<=F1;
        F1:if(fire)state<=RD;
        RD:state<=PACK;
        PACK:if(take_token)begin
          if(mode==3)begin bucket<=token_bucket|({224'd0,token[31:0]}<<token_bits);nbits<=token_bits+token[37:32];end
          else if(compressed && flags[rb][ri[7:0]])begin bucket<=token_bucket|({240'd0,token[47:32]}<<(token_bytes*8));nbytes<=token_bytes+2;end
          else begin bucket<=token_bucket|({224'd0,token[31:0]}<<(token_bytes*8));nbytes<=token_bytes+4;end
          ri<=ri+1;
          if(ri+1==nwords)state<=DRAIN;
        end
        DRAIN:begin
          if(mode==3 ? nbits!=0 : nbytes!=0)begin
            if(fire)begin bucket<=bucket>>128;nbytes<=nbytes>16?nbytes-16:0;nbits<=nbits>128?nbits-128:0;end
          end
          else begin ready[rb]<=0;flags[rb]<=0;rb<=rb+1;chunk_index<=chunk_index+1;state<=WAIT;end
        end
        FOOT:if(fire)state<=FOOT2;
        FOOT2:if(fire)begin busy<=0;capturing<=0;state<=IDLE;end
        default:state<=IDLE;
      endcase
    end
  end
endmodule
