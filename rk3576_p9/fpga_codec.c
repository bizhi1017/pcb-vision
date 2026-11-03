/* Bounded P9 LZ77 + RAW6/GRAY5 decoder. No device access or HW contexts. */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#ifdef __aarch64__
#include <arm_acle.h>
#include <sys/auxv.h>
#include <asm/hwcap.h>
#endif
#define RAW (2432u*1097u)
#define GRAY (1920u*1080u)
#define RAW4K (4832u*2192u)
#define GRAY4K (3840u*2160u)
#define MAX (RAW4K+((RAW4K+1023u)/1024u)*16u+80u)
static uint32_t u32(const uint8_t *p){return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);}
static void put32(uint8_t *p,uint32_t v){for(unsigned i=0;i<4;i++)p[i]=(uint8_t)(v>>(8*i));}
int fpga_codec_protocol(void){return 2;}
/* table generated at compile time; no shared mutable lazy initialization */
#include "fpga_crc_table.h"
static uint32_t crc32(const uint8_t *p,size_t n){uint32_t c=~0u;
#if defined(__aarch64__) && defined(__ARM_FEATURE_CRC32)
 if(getauxval(AT_HWCAP)&HWCAP_CRC32){while(n>=8){uint64_t v;memcpy(&v,p,8);c=__crc32d(c,v);p+=8;n-=8;}while(n--)c=__crc32b(c,*p++);return ~c;}
#endif
 while(n--){c=crc_table[(c^*p++)&255]^(c>>8);}
 return ~c;}
static uint32_t crcword(uint32_t c,uint32_t v,int hardware){
#if defined(__aarch64__) && defined(__ARM_FEATURE_CRC32)
 if(hardware)return __crc32w(c,v);
#else
 (void)hardware;
#endif
 for(unsigned j=0;j<4;j++){c=crc_table[(c^v)&255]^(c>>8);v>>=8;}
 return c;
}
int fpga_decode_profile(const void *input,size_t length,unsigned camera,void *output,size_t capacity,unsigned raw_bytes,unsigned gray_bytes){
 const uint8_t *p=input;uint8_t *out=output;size_t at=32,used=0;unsigned index=0;
 if(!p||!out||camera>1||length<96||length>MAX||(length&15)||!((raw_bytes==RAW&&gray_bytes==GRAY)||(raw_bytes==RAW4K&&gray_bytes==GRAY4K)))return -1;
 uint32_t info=u32(p+4),size=u32(p+8),seq=u32(p+12),epoch=u32(p+16),pre=(info>>8)&255,version=info>>24;
 if(u32(p)!=0x31433756||(version!=1&&version!=2)||(version==2&&(info&255)!=3)||((info>>16)&255)!=camera||(info&255)>3||pre>4||u32(p+20)!=1024||u32(p+24)||crc32(p,28)!=u32(p+28))return -2;
 if(size!=(pre?gray_bytes:raw_bytes)||capacity<size+16)return -3;
 uint32_t tag=u32(p+length-8),body=(uint32_t)length-16;
 if(u32(p+length-16)!=0x54373056||u32(p+length-12)!=body||u32(p+length-4)!=(body^~0u)||tag>>24!=1||((tag>>16)&255)!=camera||(tag&0xc000)||((tag>>11)&7)!=4||(tag&2047)!=((pre<<8)|(seq&255)))return -4;
 unsigned stride=raw_bytes==RAW4K?4832:2432,line_bytes=stride-2;
 while(used<size){
  if(at>length-64)return -5;
  uint32_t raw=u32(p+at+4),stored=u32(p+at+8),compressed=stored>>31,compact=version==2;
  if(compact){if(stored>>30!=3)return -6;stored&=0x3fffffff;compressed=0;}else stored&=0x7fffffff;
  uint32_t expected=size-used<1024?size-used:1024;
  if(u32(p+at)!=0x4b4e4843||u32(p+at+12)!=index||raw!=expected||stored>raw+(compressed?32:0)||(!compact&&!compressed&&stored!=raw))return -6;
  at+=16;size_t end=at+stored;if(end>length-48)return -7;
  if(compact){
   /* Gray chunks are multiples of 16 source bytes: eight pixels / five bytes.
      RAW groups are four six-bit high samples / three bytes; the fifth RAW10
      low-plane byte is reconstructed as zero. Only chunk/row edges need the
      scalar reader. This avoids a variable bit-reader branch per source byte. */
   if(pre){
    if((raw&7u)||stored!=raw/8u*5u)return -17;
    for(size_t pos=at;pos<end;pos+=5){
     uint64_t v=(uint64_t)u32(p+pos)|((uint64_t)p[pos+4]<<32);
     uint64_t expanded=((v&0x1full)<<3)|((v&0x3e0ull)<<6)|((v&0x7c00ull)<<9)|((v&0xf8000ull)<<12)|
                       ((v&0x1f00000ull)<<15)|((v&0x3e000000ull)<<18)|((v&0x7c0000000ull)<<21)|((v&0xf800000000ull)<<24);
     memcpy(out+used,&expanded,8);used+=8;
    }
   }else{
   uint64_t bucket=0;unsigned bits=0,row_offset=(unsigned)(used%stride),phase=row_offset%5;size_t pos=at,stop=used+raw;
   while(used<stop){
    if(phase==0&&row_offset+5<=line_bytes&&used+5<=stop){
     if(pos+3>end)return -17;
     uint32_t next=(uint32_t)p[pos]|((uint32_t)p[pos+1]<<8)|((uint32_t)p[pos+2]<<16);pos+=3;
     bucket|=(uint64_t)next<<bits;
     uint32_t v=(uint32_t)bucket;
     uint32_t expanded=((v&0x3fu)<<2)|((v&0xfc0u)<<4)|((v&0x3f000u)<<6)|((v&0xfc0000u)<<8);
     put32(out+used,expanded);out[used+4]=0;used+=5;row_offset+=5;bucket>>=24;
     continue;
    }
    unsigned n,shift;
    if(row_offset>=line_bytes){n=8;shift=0;}
    else if(phase==4){n=0;shift=0;}
    else{n=6;shift=2;}
    while(bits<n){if(pos>=end)return -17;bucket|=(uint64_t)p[pos++]<<bits;bits+=8;}
    out[used++]=(uint8_t)((bucket&((1u<<n)-1u))<<shift);bucket>>=n;bits-=n;
    if(++phase==5)phase=0;
    if(++row_offset==stride){row_offset=0;phase=0;}
   }
   if(pos!=end||bucket)return -18;
   }
  }else if(compressed){
   if(stored<32)return -8;
   const uint8_t *flags=p+at;size_t pos=at+32;
   for(unsigned i=raw/4;i<256;i++)if((flags[i/8]>>(i%8))&1)return -9;
   for(unsigned i=0;i<raw/4;i++){
    unsigned match=(flags[i/8]>>(i%8))&1;size_t n=match?2:4;
    if(pos+n>end)return -10;
    if(match){unsigned d=((unsigned)p[pos]|((unsigned)p[pos+1]<<8))*4;if(!d||d>used)return -11;memcpy(out+used,out+used-d,4);}
    else memcpy(out+used,p+pos,4);
    used+=4;pos+=n;
   }
   if(pos!=end)return -12;
  }else{memcpy(out+used,p+at,raw);used+=raw;}
  at+=((stored+15u)/16u)*16u;
  if(at>length-48)return -13;
  for(size_t i=end;i<at;i++)if(p[i])return -14;
  index++;
 }
 if(at+48!=length||u32(p+at)!=0x31453756||u32(p+at+8)!=size||u32(p+at+12)!=epoch||u32(p+at+4)!=crc32(out,size))return -15;
 uint32_t wire[4]={~0u,~0u,~0u,~0u};int hardware=0;
#if defined(__aarch64__) && defined(__ARM_FEATURE_CRC32)
 hardware=(getauxval(AT_HWCAP)&HWCAP_CRC32)!=0;
#endif
 for(size_t i=0;i<at;i+=16)for(unsigned lane=0;lane<4;lane++)wire[lane]=crcword(wire[lane],u32(p+i+lane*4),hardware);
 for(unsigned lane=0;lane<4;lane++)if(u32(p+at+16+lane*4)!=~wire[lane])return -16;
 put32(out+size,0x54373056);put32(out+size+4,size);put32(out+size+8,tag);put32(out+size+12,size^~0u);
 return (int)size+16;
}
int fpga_decode(const void *input,size_t length,unsigned camera,void *output,size_t capacity){return fpga_decode_profile(input,length,camera,output,capacity,RAW,GRAY);}
