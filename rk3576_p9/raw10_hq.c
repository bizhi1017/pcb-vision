#include <stdint.h>
#include <stddef.h>
#if defined(__aarch64__)
#include <arm_neon.h>
#endif
/* Stateless CPU kernels. No device access; exact RAW10 and LUT arithmetic. */
int hq_unpack(const uint8_t *src, size_t length, uint16_t *dst) {
    if (!src || !dst || length < 1096u*2432u) return -1;
    for (unsigned y=0;y<1080;y++) {
        const uint8_t *s=src+(y+16u)*2432u+5u;
        uint16_t *d=dst+y*1920u;
        unsigned x=0;
#if defined(__aarch64__)
        /* Sixteen exact RAW10 samples per iteration. The last 32-byte load
         * stays inside the source row's 2432-byte stride (including padding). */
        const uint8_t hi_idx[16]={0,1,2,3,5,6,7,8,10,11,12,13,15,16,17,18};
        const uint8_t lo_idx[16]={4,4,4,4,9,9,9,9,14,14,14,14,19,19,19,19};
        const int8_t shifts[16]={0,-2,-4,-6,0,-2,-4,-6,0,-2,-4,-6,0,-2,-4,-6};
        for (;x+4<=480;x+=4,s+=20,d+=16) {
            uint8x16x2_t table={{vld1q_u8(s),vld1q_u8(s+16)}};
            uint8x16_t high=vqtbl2q_u8(table,vld1q_u8(hi_idx));
            uint8x16_t low=vandq_u8(vshlq_u8(vqtbl2q_u8(table,vld1q_u8(lo_idx)),vld1q_s8(shifts)),vdupq_n_u8(3));
            vst1q_u16(d,vorrq_u16(vshll_n_u8(vget_low_u8(high),2),vmovl_u8(vget_low_u8(low))));
            vst1q_u16(d+8,vorrq_u16(vshll_n_u8(vget_high_u8(high),2),vmovl_u8(vget_high_u8(low))));
        }
#endif
        for (;x<480;x++,s+=5,d+=4) {
            unsigned low=s[4];
            d[0]=((unsigned)s[0]<<2)|(low&3u);
            d[1]=((unsigned)s[1]<<2)|((low>>2)&3u);
            d[2]=((unsigned)s[2]<<2)|((low>>4)&3u);
            d[3]=((unsigned)s[3]<<2)|((low>>6)&3u);
        }
    }
    return 0;
}
int hq_tone(const uint16_t *src,const uint8_t *lut,uint8_t *dst,size_t pixels) {
    if (!src || !lut || !dst) return -1;
    for (size_t i=0;i<pixels;i++,src+=3,dst+=3) {
        dst[0]=lut[src[0]>1023?1023:src[0]];
        dst[1]=lut[1024+(src[1]>1023?1023:src[1])];
        dst[2]=lut[2048+(src[2]>1023?1023:src[2])];
    }
    return 0;
}
/* Exact phase-2 AWB statistics. Integer sums equal the old float64 reductions
 * (all sums are small exact integers); smoothing stays in the Python caller. */
int hq_awb_sums(const uint8_t *src,size_t length,uint64_t *out) {
    if (!src || !out || length<1096u*2432u) return -1;
    uint64_t count=0,red=0,green=0,blue=0;
    for (unsigned y=0;y<135;y++) {
        const uint8_t *s=src+(16u+8u*y)*2432u+5u;
        for (unsigned x=0;x<240;x++,s+=10) {
            unsigned b=s[1],r=s[2432],g=((unsigned)s[0]+s[2433]+1)>>1;
            if (r>16 && r<235 && b>16 && b<235 && g>16 && g<235) {
                count++;red+=r;green+=g;blue+=b;
            }
        }
    }
    out[0]=count;out[1]=red;out[2]=green;out[3]=blue;
    return 0;
}
