/* Stateless CPU kernel: RAW10 -> exact 16-bit EA arithmetic -> original LUT.
 * No device access. Equivalence reference: OpenCV 5.x COLOR_BayerGR2BGR_EA,
 * modules/imgproc/src/demosaicing.cpp (three-row directional interpolation).
 * Sliding rows avoid full RAW16 and BGR16 intermediate images. */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdlib.h>
#include <pthread.h>
#if defined(__aarch64__)
#include <arm_neon.h>
#endif
#define W 1920
#define H 1080
#define STRIDE 2432
static void unpack_row(const uint8_t *src,int y,uint16_t *dst,int width,int stride,int crop_x,int crop_y) {
    const uint8_t *s=src+(size_t)(y+crop_y)*stride+(crop_x/4)*5;unsigned x=0;
#if defined(__aarch64__)
    const uint8_t hi_idx[16]={0,1,2,3,5,6,7,8,10,11,12,13,15,16,17,18};
    const uint8_t lo_idx[16]={4,4,4,4,9,9,9,9,14,14,14,14,19,19,19,19};
    const int8_t shifts[16]={0,-2,-4,-6,0,-2,-4,-6,0,-2,-4,-6,0,-2,-4,-6};
    for (;x<(unsigned)width/4;x+=4,s+=20,dst+=16) {
        uint8x16x2_t table={{vld1q_u8(s),vld1q_u8(s+16)}};
        uint8x16_t hi=vqtbl2q_u8(table,vld1q_u8(hi_idx));
        uint8x16_t low=vandq_u8(vshlq_u8(vqtbl2q_u8(table,vld1q_u8(lo_idx)),vld1q_s8(shifts)),vdupq_n_u8(3));
        vst1q_u16(dst,vorrq_u16(vshll_n_u8(vget_low_u8(hi),2),vmovl_u8(vget_low_u8(low))));
        vst1q_u16(dst+8,vorrq_u16(vshll_n_u8(vget_high_u8(hi),2),vmovl_u8(vget_high_u8(low))));
    }
#else
    for (;x<(unsigned)width/4;x++,s+=5,dst+=4) {
        unsigned low=s[4];
        for (unsigned i=0;i<4;i++) dst[i]=((unsigned)s[i]<<2)|((low>>(2*i))&3);
    }
#endif
}
static inline void pixel(const uint16_t *a,const uint16_t *m,const uint16_t *z,int x,int y,const uint8_t *lut,uint8_t *dst) {
    unsigned h=(m[x-1]+m[x+1]+1)>>1,v=(a[x]+z[x]+1)>>1;
    unsigned diagonal=(a[x-1]+a[x+1]+z[x-1]+z[x+1]+2)>>2;
    unsigned b,g,r;
    if ((x&1)==(y&1)) {
        g=m[x];b=(y&1)?v:h;r=(y&1)?h:v;
    } else {
        g=abs((int)m[x-1]-m[x+1])>abs((int)a[x]-z[x])?v:h;
        b=(y&1)?diagonal:m[x];r=(y&1)?m[x]:diagonal;
    }
    dst[0]=lut[b];dst[1]=lut[1024+g];dst[2]=lut[2048+r];
}
int hq_fused_ea_geometry(const uint8_t *src,size_t length,const uint8_t *lut,uint8_t *output,int width,int height,int stride,int crop_x,int crop_y,int y0,int y1) {
    if (!src || !lut || !output || width<16 || width>3840 || (width&15) || height<4 || height>2160 ||
        crop_x<0 || (crop_x&3) || crop_x>64 || crop_y<0 || crop_y>64 || stride<(crop_x+width)/4*5+16 ||
        length<(size_t)(crop_y+height)*stride || y0<0 || y1>height || y1-y0<2) return -1;
    uint16_t rows[3][width];int first=y0<1?1:y0,last=y1>height-1?height-1:y1;
    uint16_t *a=rows[0],*m=rows[1],*z=rows[2];
    unpack_row(src,first-1,a,width,stride,crop_x,crop_y);unpack_row(src,first,m,width,stride,crop_x,crop_y);unpack_row(src,first+1,z,width,stride,crop_x,crop_y);
    for (int y=first;y<last;y++) {
        uint8_t *dst=output+(size_t)y*width*3;int x=1;
#if defined(__aarch64__)
        const uint16_t even_mask[8]={0,65535,0,65535,0,65535,0,65535};
        uint16x8_t green_mask=vld1q_u16(even_mask);
        if (y&1) green_mask=vmvnq_u16(green_mask);
        for (;x+8<=width-1;x+=8) {
            uint16x8_t left=vld1q_u16(m+x-1),right=vld1q_u16(m+x+1);
            uint16x8_t up=vld1q_u16(a+x),down=vld1q_u16(z+x),center=vld1q_u16(m+x);
            uint16x8_t h=vrhaddq_u16(left,right),v=vrhaddq_u16(up,down);
            uint16x8_t diagonal=vshrq_n_u16(vaddq_u16(vaddq_u16(vaddq_u16(vld1q_u16(a+x-1),vld1q_u16(a+x+1)),vaddq_u16(vld1q_u16(z+x-1),vld1q_u16(z+x+1))),vdupq_n_u16(2)),2);
            uint16x8_t g=vbslq_u16(vcgtq_u16(vabdq_u16(left,right),vabdq_u16(up,down)),v,h);
            uint16x8_t b=(y&1)?vbslq_u16(green_mask,v,diagonal):vbslq_u16(green_mask,h,center);
            uint16x8_t r=(y&1)?vbslq_u16(green_mask,h,center):vbslq_u16(green_mask,v,diagonal);
            g=vbslq_u16(green_mask,center,g);
            uint16_t blue[8],green[8],red[8];vst1q_u16(blue,b);vst1q_u16(green,g);vst1q_u16(red,r);
            for (unsigned i=0;i<8;i++) {
                dst[3*(x+i)]=lut[blue[i]];dst[3*(x+i)+1]=lut[1024+green[i]];dst[3*(x+i)+2]=lut[2048+red[i]];
            }
        }
#endif
        for (;x<width-1;x++) pixel(a,m,z,x,y,lut,dst+3*x);
        memcpy(dst,dst+3,3);memcpy(dst+3*(width-1),dst+3*(width-2),3);
        if (y+1<last) {uint16_t *old=a;a=m;m=z;z=old;unpack_row(src,y+2,z,width,stride,crop_x,crop_y);}
    }
    if (y0==0) memcpy(output,output+width*3,width*3);
    if (y1==height) memcpy(output+(size_t)(height-1)*width*3,output+(size_t)(height-2)*width*3,width*3);
    return 0;
}
int hq_fused_ea(const uint8_t *src,size_t length,const uint8_t *lut,uint8_t *output,int y0,int y1) {
    return hq_fused_ea_geometry(src,length,lut,output,W,H,STRIDE,4,16,y0,y1);
}

struct hq_band_job {
    const uint8_t *src,*lut;
    size_t length;
    uint8_t *output;
    int rc;
    int width,height,stride,crop_x,crop_y;
};
static void *hq_second_band(void *opaque) {
    struct hq_band_job *job=opaque;
    job->rc=hq_fused_ea_geometry(job->src,job->length,job->lut,job->output,job->width,job->height,job->stride,job->crop_x,job->crop_y,job->height/2,job->height);
    return NULL;
}
/* One Python/native boundary, same two disjoint bands and exact EA arithmetic.
 * Each invocation owns its job/rows/thread; both writes finish before return. */
int hq_fused_ea_profile(const uint8_t *src,size_t length,const uint8_t *lut,uint8_t *output,int width,int height,int stride,int crop_x,int crop_y) {
    if (!src || !lut || !output || width<16 || width>3840 || (width&15) || height<4 || height>2160 || (height&1) ||
        crop_x<0 || crop_x>64 || (crop_x&3) || crop_y<0 || crop_y>64 || stride<(crop_x+width)/4*5+16 || length<(size_t)(crop_y+height)*stride) return -1;
    struct hq_band_job job={src,lut,length,output,0,width,height,stride,crop_x,crop_y};
    pthread_t thread;
    int created=pthread_create(&thread,NULL,hq_second_band,&job)==0;
    int first=hq_fused_ea_geometry(src,length,lut,output,width,height,stride,crop_x,crop_y,0,height/2);
    if (created) pthread_join(thread,NULL);
    else job.rc=hq_fused_ea_geometry(src,length,lut,output,width,height,stride,crop_x,crop_y,height/2,height);
    return first?first:job.rc;
}
int hq_fused_ea_parallel(const uint8_t *src,size_t length,const uint8_t *lut,uint8_t *output) {
    return hq_fused_ea_profile(src,length,lut,output,W,H,STRIDE,4,16);
}
