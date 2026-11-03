/* Reversible planar prediction + Zstandard. No quantization or image resize.
 * Profile 2 bypasses LZ match search: zero sequences, Huffman-coded literals.
 * The advanced sequence API is linked statically to its matching system header.
 * libzstd symbols are hidden; the Qt decoder uses only the stable Zstd ABI.
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#define ZSTD_STATIC_LINKING_ONLY
#include <zstd.h>
#include <libdeflate.h>
#include <pthread.h>
#include <time.h>
#if defined(__ARM_NEON) || defined(__aarch64__)
#include <arm_neon.h>
#endif
#define MAX_FRAME (32u*1024u*1024u)
#define HEADER 32u
typedef struct {ZSTD_CCtx *cctx[3];uint8_t *filtered;size_t capacity;int level;double timings[3];} encoder;
static double now_ms(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec*1000.+t.tv_nsec/1e6;}
void pcb_lossless_profile(void *opaque,double *out){encoder *ctx=opaque;if(ctx && out)memcpy(out,ctx->timings,sizeof(ctx->timings));}
typedef struct {ZSTD_CCtx *ctx;const uint8_t *src;uint8_t *dst;size_t raw,capacity,written;int level;} job;
static void *compress_plane(void *opaque){
    job *j=opaque;
    if(j->level==2){
        ZSTD_CCtx_reset(j->ctx,ZSTD_reset_session_and_parameters);
        ZSTD_CCtx_setParameter(j->ctx,ZSTD_c_compressionLevel,1);
        j->written=ZSTD_compressSequences(j->ctx,j->dst,j->capacity,NULL,0,j->src,j->raw);
    }else j->written=ZSTD_compressCCtx(j->ctx,j->dst,j->capacity,j->src,j->raw,j->level);
    return NULL;
}
static void be32(uint8_t *p,uint32_t n){p[0]=(uint8_t)(n>>24);p[1]=(uint8_t)(n>>16);p[2]=(uint8_t)(n>>8);p[3]=(uint8_t)n;}
void *pcb_lossless_create(int level){
    if(level!=1 && level!=2)return NULL;
    encoder *ctx=calloc(1,sizeof(*ctx));if(!ctx)return NULL;
    ctx->level=level;
    for(unsigned i=0;i<3;i++){ctx->cctx[i]=ZSTD_createCCtx();if(!ctx->cctx[i]){for(unsigned j=0;j<i;j++)ZSTD_freeCCtx(ctx->cctx[j]);free(ctx);return NULL;}}
    return ctx;
}
void pcb_lossless_destroy(void *opaque){encoder *ctx=opaque;if(ctx){for(unsigned i=0;i<3;i++)ZSTD_freeCCtx(ctx->cctx[i]);free(ctx->filtered);free(ctx);}}
size_t pcb_lossless_bound(unsigned w,unsigned h){
    if(!w || !h || w>4096 || h>4096)return 0;
    size_t bound=HEADER+12;
    for(unsigned i=0;i<3;i++)bound+=ZSTD_compressBound((size_t)w*3*(h*(i+1)/3-h*i/3));
    return bound<=MAX_FRAME ? bound : 0;
}
static void predict(const uint8_t *restrict src,uint8_t *restrict green,uint8_t *restrict blue,
                    uint8_t *restrict red,unsigned w){
    unsigned x=0;uint8_t pg=0,pb=0,pr=0;
#if defined(__ARM_NEON) || defined(__aarch64__)
    for(;x+16<=w;x+=16){
        uint8x16x3_t bgr=vld3q_u8(src+3*x);
        uint8x16_t g=bgr.val[1],b=vsubq_u8(bgr.val[0],g),r=vsubq_u8(bgr.val[2],g);
        vst1q_u8(green+x,vsubq_u8(g,vextq_u8(vdupq_n_u8(pg),g,15)));
        vst1q_u8(blue+x,vsubq_u8(b,vextq_u8(vdupq_n_u8(pb),b,15)));
        vst1q_u8(red+x,vsubq_u8(r,vextq_u8(vdupq_n_u8(pr),r,15)));
        pg=vgetq_lane_u8(g,15);pb=vgetq_lane_u8(b,15);pr=vgetq_lane_u8(r,15);
    }
#endif
    for(;x<w;x++){
        uint8_t g=src[3*x+1],b=(uint8_t)(src[3*x]-g),r=(uint8_t)(src[3*x+2]-g);
        green[x]=(uint8_t)(g-pg);blue[x]=(uint8_t)(b-pb);red[x]=(uint8_t)(r-pr);pg=g;pb=b;pr=r;
    }
}
int pcb_lossless_encode(void *opaque,const uint8_t *src,size_t source_bytes,unsigned w,unsigned h,
                        size_t stride,uint8_t *out,size_t capacity,size_t *written){
    if(written)*written=0;
    encoder *ctx=opaque;size_t bound=pcb_lossless_bound(w,h);
    if(!ctx || !bound || !src || !out || !written || capacity<bound || stride<(size_t)w*3)return -1;
    if(stride>(SIZE_MAX-(size_t)w*3)/(h>1?h-1:1) || source_bytes<(h-1)*stride+(size_t)w*3)return -1;
    size_t plane=(size_t)w*h,raw=plane*3;
    if(ctx->capacity<raw){uint8_t *p=realloc(ctx->filtered,raw);if(!p)return -2;ctx->filtered=p;ctx->capacity=raw;}
    double t=now_ms();
    for(unsigned i=0;i<3;i++){
        unsigned start=h*i/3,end=h*(i+1)/3;size_t band_plane=(size_t)(end-start)*w,offset=(size_t)start*w*3;
        for(unsigned y=start;y<end;y++)predict(src+y*stride,ctx->filtered+offset+(size_t)(y-start)*w,ctx->filtered+offset+band_plane+(size_t)(y-start)*w,ctx->filtered+offset+2*band_plane+(size_t)(y-start)*w,w);
    }
    ctx->timings[0]=now_ms()-t;t=now_ms();
    job jobs[3];pthread_t threads[3];unsigned started=0;size_t offset=HEADER+12;
    for(unsigned i=0;i<3;i++){
        size_t start=(size_t)(h*i/3)*w*3,size=(size_t)(h*(i+1)/3-h*i/3)*w*3,slot=ZSTD_compressBound(size);
        jobs[i]=(job){ctx->cctx[i],ctx->filtered+start,out+offset,size,slot,0,ctx->level};offset+=slot;
    }
    for(unsigned i=0;i<3;i++){
        if(pthread_create(&threads[i],NULL,compress_plane,&jobs[i]))break;
        started++;
    }
    for(unsigned i=started;i<3;i++)compress_plane(&jobs[i]);
    for(unsigned i=0;i<started;i++)pthread_join(threads[i],NULL);
    ctx->timings[1]=now_ms()-t;t=now_ms();
    size_t n=12;
    for(unsigned i=0;i<3;i++){
        if(ZSTD_isError(jobs[i].written) || jobs[i].written>jobs[i].capacity)return -3;
        be32(out+HEADER+4*i,(uint32_t)jobs[i].written);
        memmove(out+HEADER+n,jobs[i].dst,jobs[i].written);n+=jobs[i].written;
    }
    memcpy(out,"PCZ1",4);be32(out+4,w);be32(out+8,h);be32(out+12,(uint32_t)raw);be32(out+16,(uint32_t)n);
    be32(out+20,libdeflate_crc32(0,ctx->filtered,raw));be32(out+24,3);be32(out+28,libdeflate_crc32(0,out,28));
    *written=n+HEADER;ctx->timings[2]=now_ms()-t;return 0;
}
