/* Full RGB8 PNG: reversible row prediction and the system libdeflate encoder. */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <libdeflate.h>
#include <zlib.h>
#include <pthread.h>
#if defined(__ARM_NEON) || defined(__aarch64__)
#include <arm_neon.h>
#endif

#define PNG_LIMIT (32u * 1024u * 1024u)
typedef struct {
    z_stream stream;
    uint8_t *output;
    size_t capacity;
    const uint8_t *input;
    size_t input_bytes,written;
    int last,status;
    pthread_t thread;
} png_job;
typedef struct {
    struct libdeflate_compressor *compressor;
    uint8_t *rows;
    size_t capacity;
    int filter;
    unsigned workers;
    png_job jobs[4];
} png_encoder;

static void be32(uint8_t *p, uint32_t value) {
    p[0]=(uint8_t)(value>>24);p[1]=(uint8_t)(value>>16);
    p[2]=(uint8_t)(value>>8);p[3]=(uint8_t)value;
}

void *pcb_png_create(int level,int filter) {
    if(level<1 || level>12 || filter<0 || filter>2)return NULL;
    png_encoder *ctx=calloc(1,sizeof(*ctx));
    if(!ctx)return NULL;
    ctx->compressor=libdeflate_alloc_compressor(level);ctx->filter=filter;
    if(!ctx->compressor){free(ctx);return NULL;}
    return ctx;
}

void pcb_png_destroy(void *opaque) {
    png_encoder *ctx=opaque;
    if(!ctx)return;
    if(ctx->compressor)libdeflate_free_compressor(ctx->compressor);
    for(unsigned i=0;i<ctx->workers;i++) {
        if(ctx->jobs[i].stream.state)deflateEnd(&ctx->jobs[i].stream);
        free(ctx->jobs[i].output);
    }
    free(ctx->rows);free(ctx);
}

void *pcb_png_create_parallel(int workers,int filter) {
    if(workers<2 || workers>4 || filter<0 || filter>2)return NULL;
    png_encoder *ctx=calloc(1,sizeof(*ctx));
    if(!ctx)return NULL;
    ctx->workers=(unsigned)workers;ctx->filter=filter;
    for(unsigned i=0;i<ctx->workers;i++) {
        if(deflateInit2(&ctx->jobs[i].stream,1,Z_DEFLATED,-15,8,Z_RLE)!=Z_OK) {
            pcb_png_destroy(ctx);return NULL;
        }
    }
    return ctx;
}

size_t pcb_png_bound(void *opaque,unsigned width,unsigned height) {
    png_encoder *ctx=opaque;
    if(!ctx || !width || !height || width>4096 || height>4096)return 0;
    size_t raw=((size_t)width*3+1)*height;
    size_t bound;
    if(ctx->workers) {
        unsigned workers=height<ctx->workers?height:ctx->workers;
        bound=6;
        for(unsigned i=0;i<workers;i++) {
            size_t rows=(size_t)height*(i+1)/workers-(size_t)height*i/workers;
            bound+=deflateBound(&ctx->jobs[i].stream,((size_t)width*3+1)*rows)+64;
        }
    } else bound=libdeflate_zlib_compress_bound(ctx->compressor,raw);
    return bound<=PNG_LIMIT-57 ? bound+57 : 0;
}

static void *compress_part(void *opaque) {
    png_job *job=opaque;job->status=-1;job->written=0;
    if(deflateReset(&job->stream)!=Z_OK)return NULL;
    job->stream.next_in=(Bytef *)job->input;job->stream.avail_in=(uInt)job->input_bytes;
    job->stream.next_out=job->output;job->stream.avail_out=(uInt)job->capacity;
    int code=deflate(&job->stream,job->last?Z_FINISH:Z_FULL_FLUSH);
    if(job->stream.avail_in || !job->stream.avail_out || code!=(job->last?Z_STREAM_END:Z_OK))return NULL;
    job->written=job->capacity-job->stream.avail_out;job->status=0;return NULL;
}

static size_t compress_parallel(png_encoder *ctx,unsigned width,unsigned height,
                               uint8_t *output,size_t capacity) {
    unsigned workers=height<ctx->workers?height:ctx->workers,created=0;
    size_t row_bytes=(size_t)width*3+1;
    for(unsigned i=0;i<workers;i++) {
        png_job *job=&ctx->jobs[i];size_t start=(size_t)height*i/workers,end=(size_t)height*(i+1)/workers;
        job->input=ctx->rows+start*row_bytes;job->input_bytes=(end-start)*row_bytes;job->last=i+1==workers;
        size_t bound=deflateBound(&job->stream,job->input_bytes)+64;
        if(job->capacity<bound) {
            uint8_t *replacement=realloc(job->output,bound);
            if(!replacement)return 0;
            job->output=replacement;job->capacity=bound;
        }
    }
    for(unsigned i=0;i<workers;i++) {
        if(pthread_create(&ctx->jobs[i].thread,NULL,compress_part,&ctx->jobs[i]))break;
        created++;
    }
    for(unsigned i=0;i<created;i++)pthread_join(ctx->jobs[i].thread,NULL);
    if(created!=workers)return 0;
    size_t length=2;output[0]=0x78;output[1]=0x01;
    for(unsigned i=0;i<workers;i++) {
        png_job *job=&ctx->jobs[i];
        if(job->status || job->written>capacity-length-4)return 0;
        memcpy(output+length,job->output,job->written);length+=job->written;
    }
    be32(output+length,libdeflate_adler32(1,ctx->rows,row_bytes*height));
    return length+4;
}

static void filter_row(const uint8_t *restrict src,const uint8_t *restrict up,
                       uint8_t *restrict dst,unsigned width,int filter) {
    unsigned x=0;uint8_t red=0,green=0,blue=0;
    *dst++=(uint8_t)filter;
#if defined(__ARM_NEON) || defined(__aarch64__)
    for(;x+16<=width;x+=16) {
        uint8x16x3_t bgr=vld3q_u8(src+3*x),rgb;
        if(filter==1) {
            rgb.val[0]=vsubq_u8(bgr.val[2],vextq_u8(vdupq_n_u8(red),bgr.val[2],15));
            rgb.val[1]=vsubq_u8(bgr.val[1],vextq_u8(vdupq_n_u8(green),bgr.val[1],15));
            rgb.val[2]=vsubq_u8(bgr.val[0],vextq_u8(vdupq_n_u8(blue),bgr.val[0],15));
            red=vgetq_lane_u8(bgr.val[2],15);green=vgetq_lane_u8(bgr.val[1],15);blue=vgetq_lane_u8(bgr.val[0],15);
        } else if(filter==2) {
            uint8x16x3_t previous=vld3q_u8(up+3*x);
            rgb.val[0]=vsubq_u8(bgr.val[2],previous.val[2]);
            rgb.val[1]=vsubq_u8(bgr.val[1],previous.val[1]);
            rgb.val[2]=vsubq_u8(bgr.val[0],previous.val[0]);
        } else {
            rgb.val[0]=bgr.val[2];rgb.val[1]=bgr.val[1];rgb.val[2]=bgr.val[0];
        }
        vst3q_u8(dst+3*x,rgb);
    }
#endif
    for(;x<width;x++) {
        uint8_t r=src[3*x+2],g=src[3*x+1],b=src[3*x];
        if(filter==1) {
            dst[3*x]=(uint8_t)(r-red);dst[3*x+1]=(uint8_t)(g-green);dst[3*x+2]=(uint8_t)(b-blue);
            red=r;green=g;blue=b;
        } else if(filter==2) {
            dst[3*x]=(uint8_t)(r-up[3*x+2]);dst[3*x+1]=(uint8_t)(g-up[3*x+1]);dst[3*x+2]=(uint8_t)(b-up[3*x]);
        } else {dst[3*x]=r;dst[3*x+1]=g;dst[3*x+2]=b;}
    }
}

int pcb_png_encode(void *opaque,const uint8_t *bgr,size_t source_bytes,
                   unsigned width,unsigned height,size_t stride,
                   uint8_t *output,size_t output_capacity,size_t *written) {
    png_encoder *ctx=opaque;
    if(written)*written=0;
    size_t bound=pcb_png_bound(ctx,width,height);
    if(!bound || !bgr || !output || !written || stride<(size_t)width*3 || output_capacity<bound)return -1;
    if(stride>(SIZE_MAX-(size_t)width*3)/(height>1?height-1:1))return -1;
    if(source_bytes<(height-1)*stride+(size_t)width*3)return -1;
    size_t row_bytes=(size_t)width*3+1,raw_bytes=row_bytes*height;
    if(ctx->capacity<raw_bytes) {
        uint8_t *replacement=realloc(ctx->rows,raw_bytes);
        if(!replacement)return -2;
        ctx->rows=replacement;ctx->capacity=raw_bytes;
    }
    for(unsigned y=0;y<height;y++) {
        int filter=ctx->filter==2 && y==0 ? 0 : ctx->filter;
        filter_row(bgr+y*stride,y?bgr+(y-1)*stride:NULL,ctx->rows+y*row_bytes,width,filter);
    }
    size_t compressed=ctx->workers?compress_parallel(ctx,width,height,output+41,output_capacity-57):
        libdeflate_zlib_compress(ctx->compressor,ctx->rows,raw_bytes,output+41,output_capacity-57);
    if(!compressed || compressed>UINT32_MAX)return -3;
    memcpy(output,"\x89PNG\r\n\x1a\n",8);
    be32(output+8,13);memcpy(output+12,"IHDR",4);
    be32(output+16,width);be32(output+20,height);
    output[24]=8;output[25]=2;output[26]=0;output[27]=0;output[28]=0;
    be32(output+29,libdeflate_crc32(0,output+12,17));
    be32(output+33,(uint32_t)compressed);memcpy(output+37,"IDAT",4);
    be32(output+41+compressed,libdeflate_crc32(0,output+37,compressed+4));
    be32(output+45+compressed,0);memcpy(output+49+compressed,"IEND",4);
    be32(output+53+compressed,0xae426082u);
    *written=compressed+57;return 0;
}
