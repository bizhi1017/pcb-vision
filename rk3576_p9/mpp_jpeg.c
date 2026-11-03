#define _POSIX_C_SOURCE 200809L
/* RK MPP MJPEG bridge. Per-channel contexts; never touches capture or NPU. */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#ifdef PCB_USE_RGA
#include "im2d.h"
#include "rga.h"
#endif
#include "rk_mpi.h"
#include "rk_venc_cfg.h"
#include "rk_venc_rc.h"
#include "mpp_buffer.h"
#include "mpp_frame.h"
#include "mpp_packet.h"
#include "mpp_meta.h"

typedef struct {
    MppCtx ctx; MppApi *api; MppEncCfg cfg;
    MppBufferGroup group; MppBuffer input,output;
    int w,h,hs,vs,q,planar,cached;
    double copy_ms,sync_ms,rga_ms,output_ms;
#ifdef PCB_USE_RGA
    MppBuffer bgr;
    rga_buffer_handle_t rga_src,rga_dst;
#endif
} PcbJpeg;

void pcb_jpeg_free(PcbJpeg *s) {
    if (!s) return;
    if (s->ctx) mpp_destroy(s->ctx);
#ifdef PCB_USE_RGA
    if(s->rga_src)releasebuffer_handle(s->rga_src);
    if(s->rga_dst)releasebuffer_handle(s->rga_dst);
    if(s->bgr)mpp_buffer_put(s->bgr);
#endif
    if (s->input) mpp_buffer_put(s->input);
    if (s->output) mpp_buffer_put(s->output);
    if (s->group) mpp_buffer_group_put(s->group);
    if (s->cfg) mpp_enc_cfg_deinit(s->cfg);
    free(s);
}

static int set_quality(PcbJpeg *s,int q) {
    if (q<1 || q>99) return -1;
    if (mpp_enc_cfg_set_s32(s->cfg,"jpeg:q_factor",q) ||
        mpp_enc_cfg_set_s32(s->cfg,"jpeg:qf_min",q) ||
        mpp_enc_cfg_set_s32(s->cfg,"jpeg:qf_max",q)) return -2;
    if (s->api->control(s->ctx,MPP_ENC_SET_CFG,s->cfg)) return -3;
    s->q=q;return 0;
}

static PcbJpeg *create_policy(int width,int height,int quality,int planar,int cached) {
    if (width<16 || height<16 || width>3840 || height>2160 || (width&1) || (height&1)) return NULL;
    PcbJpeg *s=calloc(1,sizeof(*s));if(!s)return NULL;
    s->w=width;s->h=height;s->hs=(width+15)&~15;s->vs=(height+15)&~15;s->planar=planar;
    RK_S64 timeout=2000;
    if(mpp_create(&s->ctx,&s->api) || mpp_init(s->ctx,MPP_CTX_ENC,MPP_VIDEO_CodingMJPEG))goto fail;
    if(s->api->control(s->ctx,MPP_SET_OUTPUT_TIMEOUT,&timeout) || mpp_enc_cfg_init(&s->cfg) ||
       s->api->control(s->ctx,MPP_ENC_GET_CFG,s->cfg))goto fail;
#define CFG(name,value) if(mpp_enc_cfg_set_s32(s->cfg,name,value))goto fail
    CFG("prep:width",s->w);CFG("prep:height",s->h);CFG("prep:hor_stride",s->hs);CFG("prep:ver_stride",s->vs);
    CFG("prep:format",planar?MPP_FMT_YUV420P:MPP_FMT_YUV420SP);CFG("rc:mode",MPP_ENC_RC_MODE_FIXQP);
    CFG("prep:range",MPP_FRAME_RANGE_MPEG);
    CFG("rc:fps_in_num",60);CFG("rc:fps_in_denorm",1);CFG("rc:fps_out_num",60);CFG("rc:fps_out_denorm",1);
    CFG("codec:type",MPP_VIDEO_CodingMJPEG);
#undef CFG
    unsigned buffer_type=MPP_BUFFER_TYPE_DRM;
#ifdef MPP_BUFFER_FLAGS_CACHABLE
    if(cached){buffer_type|=MPP_BUFFER_FLAGS_CACHABLE;s->cached=1;}
#else
    (void)cached;
#endif
    if(set_quality(s,quality) || mpp_buffer_group_get_internal(&s->group,buffer_type))goto fail;
    if(mpp_buffer_get(s->group,&s->input,(size_t)s->hs*s->vs*3/2) ||
       mpp_buffer_get(s->group,&s->output,(size_t)s->hs*s->vs*3+4096))goto fail;
    if(mpp_buffer_sync_begin(s->input))goto fail;
    memset(mpp_buffer_get_ptr(s->input),128,(size_t)s->hs*s->vs*3/2);
    if(mpp_buffer_sync_end(s->input))goto fail;
    return s;
fail:pcb_jpeg_free(s);return NULL;
}

static PcbJpeg *create(int width,int height,int quality,int planar) {
    /* On RK3576 this workload is faster with the original uncached buffers.
     * Cached DMA is an explicit diagnostic opt-in, never the default. */
    const char *setting=getenv("PCB_JPEG_CACHED_DMA");
    int cached=setting && !strcmp(setting,"1");
    PcbJpeg *s=create_policy(width,height,quality,planar,cached);
    /* Older allocators may reject cached buffers; retain the working path. */
    return s?s:(cached?create_policy(width,height,quality,planar,0):NULL);
}

PcbJpeg *pcb_jpeg_create(int width,int height,int quality) {return create(width,height,quality,0);}
PcbJpeg *pcb_jpeg_create_i420(int width,int height,int quality) {return create(width,height,quality,1);}
int pcb_jpeg_buffer_cached(PcbJpeg *s) {return s?s->cached:0;}
int pcb_jpeg_pipeline_stats(PcbJpeg *s,double *values,size_t count) {
    if(!s||!values||count<4)return -1;
    values[0]=s->copy_ms;values[1]=s->sync_ms;values[2]=s->rga_ms;values[3]=s->output_ms;return 0;
}

static double milliseconds(void) {
    struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec*1000.0+t.tv_nsec/1000000.0;
}

static int encode_ready(PcbJpeg *s,int quality,uint8_t *dst,size_t capacity,size_t *length) {
    MppFrame frame=NULL;MppPacket packet=NULL;int result=-1;
    if(!s||!dst||!length)return -1;
    *length=0;
    if(quality!=s->q && set_quality(s,quality))return -2;
    if(mpp_frame_init(&frame) || mpp_packet_init_with_buffer(&packet,s->output))goto done;
    mpp_frame_set_width(frame,s->w);mpp_frame_set_height(frame,s->h);mpp_frame_set_hor_stride(frame,s->hs);
    mpp_frame_set_ver_stride(frame,s->vs);mpp_frame_set_fmt(frame,s->planar?MPP_FMT_YUV420P:MPP_FMT_YUV420SP);mpp_frame_set_buffer(frame,s->input);
    mpp_frame_set_color_range(frame,MPP_FRAME_RANGE_MPEG);
    mpp_packet_set_length(packet,0);mpp_meta_set_packet(mpp_frame_get_meta(frame),KEY_OUTPUT_PACKET,packet);
    if(s->api->encode_put_frame(s->ctx,frame))goto done;
    MppPacket encoded=NULL;
    if(s->api->encode_get_packet(s->ctx,&encoded) || !encoded)goto done;
    if(encoded!=packet){mpp_packet_deinit(&packet);packet=encoded;}
    size_t bytes=mpp_packet_get_length(packet);
    if(!bytes || bytes>capacity){result=-5;goto done;}
    MppBuffer actual=mpp_packet_get_buffer(packet);
    double copying=milliseconds();
    if(actual && mpp_buffer_sync_ro_begin(actual)){result=-6;goto done;}
    memcpy(dst,mpp_packet_get_pos(packet),bytes);*length=bytes;
    if(actual && mpp_buffer_sync_ro_end(actual)){*length=0;result=-7;goto done;}
    s->output_ms=milliseconds()-copying;
    result=0;
done:
    if(packet)mpp_packet_deinit(&packet);
    if(frame)mpp_frame_deinit(&frame);
    return result;
}

int pcb_jpeg_encode(PcbJpeg *s,const uint8_t *nv12,size_t input_size,int quality,uint8_t *dst,size_t capacity,size_t *length) {
    if(!s||!nv12||!dst||!length||input_size!=(size_t)s->w*s->h*3/2)return -1;
    *length=0;
    s->copy_ms=s->sync_ms=s->rga_ms=s->output_ms=0;
    if(mpp_buffer_sync_begin(s->input))return -3;
    uint8_t *ptr=mpp_buffer_get_ptr(s->input);
    if(s->hs==s->w)memcpy(ptr,nv12,(size_t)s->w*s->h);
    else for(int y=0;y<s->h;y++)memcpy(ptr+(size_t)y*s->hs,nv12+(size_t)y*s->w,s->w);
    if(s->planar) {
        size_t padded=(size_t)s->hs*s->vs,active=(size_t)s->w*s->h;
        for(int c=0;c<2;c++) {
            uint8_t *out=ptr+padded+c*padded/4;const uint8_t *in=nv12+active+c*active/4;
            if(s->hs==s->w)memcpy(out,in,active/4);
            else for(int y=0;y<s->h/2;y++)memcpy(out+(size_t)y*s->hs/2,in+(size_t)y*s->w/2,s->w/2);
        }
    } else {
        uint8_t *out=ptr+(size_t)s->hs*s->vs;const uint8_t *in=nv12+(size_t)s->w*s->h;
        if(s->hs==s->w)memcpy(out,in,(size_t)s->w*s->h/2);
        else for(int y=0;y<s->h/2;y++)memcpy(out+(size_t)y*s->hs,in+(size_t)y*s->w,s->w);
    }
    if(mpp_buffer_sync_end(s->input))return -4;
    return encode_ready(s,quality,dst,capacity,length);
}

#ifdef PCB_USE_RGA
/* Import only stable, leased HQ storage. The Python owner retains the mapping
 * until release; synchronous RGA use finishes before an image lease is freed. */
uint64_t pcb_jpeg_import_source(PcbJpeg *s,void *pixels,size_t size) {
    if(!s||!s->planar||!pixels||size!=(size_t)s->w*s->h*3)return 0;
    im_handle_param_t a={0};a.width=s->w;a.height=s->h;a.format=RK_FORMAT_BGR_888;
    return (uint64_t)importbuffer_virtualaddr(pixels,&a);
}
void pcb_jpeg_release_source(uint64_t handle) {if(handle)releasebuffer_handle((rga_buffer_handle_t)handle);}
int pcb_jpeg_encode_source(PcbJpeg *s,uint64_t handle,int quality,uint8_t *dst,size_t capacity,size_t *length,double *color_ms,double *encode_ms) {
    if(!s||!s->planar||!handle||!dst||!length||!color_ms||!encode_ms)return -100;
    double started=milliseconds();*length=0;s->copy_ms=s->sync_ms=s->rga_ms=s->output_ms=0;
    im_handle_param_t a={0};a.width=s->hs;a.height=s->vs;a.format=RK_FORMAT_YCbCr_420_P;
    if(!s->rga_dst)s->rga_dst=importbuffer_fd(mpp_buffer_get_fd(s->input),&a);
    if(!s->rga_dst)return -102;
    rga_buffer_t src=wrapbuffer_handle_t((rga_buffer_handle_t)handle,s->w,s->h,s->w,s->h,RK_FORMAT_BGR_888);
    rga_buffer_t out=wrapbuffer_handle_t(s->rga_dst,s->w,s->h,s->hs,s->vs,RK_FORMAT_YCbCr_420_P);
    rga_buffer_t unused={0};im_rect rect={0};IM_STATUS status=imcheck_t(src,out,unused,rect,rect,rect,0);
    if(status!=IM_STATUS_NOERROR && status!=IM_STATUS_SUCCESS)return -2000+(int)status;
    double converted_started=milliseconds();
    /* Virtual imports receive the driver's per-job cache synchronization. */
    status=imcvtcolor_t(src,out,RK_FORMAT_BGR_888,RK_FORMAT_YCbCr_420_P,IM_RGB_TO_YUV_BT601_LIMIT,1);
    if(status!=IM_STATUS_SUCCESS)return -3000+(int)status;
    s->rga_ms=milliseconds()-converted_started;*color_ms=milliseconds()-started;double converted=milliseconds();
    int result=encode_ready(s,quality,dst,capacity,length);*encode_ms=milliseconds()-converted;return result;
}
int pcb_jpeg_encode_bgr(PcbJpeg *s,const uint8_t *bgr,size_t input_size,int quality,uint8_t *dst,size_t capacity,size_t *length,double *color_ms,double *encode_ms) {
    if(!s||!s->planar||!bgr||!dst||!length||!color_ms||!encode_ms||input_size!=(size_t)s->w*s->h*3)return -100;
    double started=milliseconds();*length=0;s->copy_ms=s->sync_ms=s->rga_ms=s->output_ms=0;
    if(!s->bgr && mpp_buffer_get(s->group,&s->bgr,(size_t)s->hs*s->vs*3))return -101;
    im_handle_param_t a={0};a.width=s->hs;a.height=s->vs;a.format=RK_FORMAT_BGR_888;
    if(!s->rga_src)s->rga_src=importbuffer_fd(mpp_buffer_get_fd(s->bgr),&a);
    a.format=RK_FORMAT_YCbCr_420_P;
    if(!s->rga_dst)s->rga_dst=importbuffer_fd(mpp_buffer_get_fd(s->input),&a);
    if(!s->rga_src||!s->rga_dst)return -102;
    rga_buffer_t src=wrapbuffer_handle_t(s->rga_src,s->w,s->h,s->hs,s->vs,RK_FORMAT_BGR_888);
    rga_buffer_t out=wrapbuffer_handle_t(s->rga_dst,s->w,s->h,s->hs,s->vs,RK_FORMAT_YCbCr_420_P);
    rga_buffer_t unused={0};im_rect rect={0};
    IM_STATUS status=imcheck_t(src,out,unused,rect,rect,rect,0);
    if(status!=IM_STATUS_NOERROR && status!=IM_STATUS_SUCCESS)return -2000+(int)status;
    double sync=milliseconds();
    if(mpp_buffer_sync_begin(s->bgr))return -103;
    s->sync_ms=milliseconds()-sync;
    double copying=milliseconds();
    uint8_t *ptr=mpp_buffer_get_ptr(s->bgr);
    if(s->hs==s->w)memcpy(ptr,bgr,(size_t)s->w*s->h*3);
    else for(int y=0;y<s->h;y++)memcpy(ptr+(size_t)y*s->hs*3,bgr+(size_t)y*s->w*3,(size_t)s->w*3);
    s->copy_ms=milliseconds()-copying;sync=milliseconds();
    if(mpp_buffer_sync_end(s->bgr))return -104;
    s->sync_ms+=milliseconds()-sync;double converted_started=milliseconds();
    status=imcvtcolor_t(src,out,RK_FORMAT_BGR_888,RK_FORMAT_YCbCr_420_P,IM_RGB_TO_YUV_BT601_LIMIT,1);
    if(status!=IM_STATUS_SUCCESS)return -3000+(int)status;
    s->rga_ms=milliseconds()-converted_started;
    *color_ms=milliseconds()-started;double converted=milliseconds();
    int result=encode_ready(s,quality,dst,capacity,length);*encode_ms=milliseconds()-converted;
    return result;
}
#endif
