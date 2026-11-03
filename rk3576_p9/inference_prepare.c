/* CPU-only exact specializations of OpenCV INTER_LINEAR for 1080P.
 * 3:1 samples integer pixel centers; 6:1 samples half-integer centers.
 * Full model RGB/padding and tracking BGR are validated by the Python owner. */
#include <stddef.h>
#include <stdint.h>
#include <string.h>
int pcb_prepare_1080(const uint8_t *src,size_t length,uint8_t *rgb,size_t rgb_capacity,uint8_t *small,size_t small_capacity){
    if(!src||!rgb||!small||length<1920u*1080u*3u||rgb_capacity<640u*640u*3u||small_capacity<320u*180u*3u)return -1;
    memset(rgb,0,140u*640u*3u);memset(rgb+500u*640u*3u,0,140u*640u*3u);
    for(unsigned y=0;y<360;y++){
        const uint8_t *s=src+((size_t)(3*y+1)*1920+1)*3;uint8_t *d=rgb+(size_t)(y+140)*640*3;
        for(unsigned x=0;x<640;x++,s+=9,d+=3){d[0]=s[2];d[1]=s[1];d[2]=s[0];}
    }
    for(unsigned y=0;y<180;y++){
        const uint8_t *a=src+((size_t)(6*y+2)*1920+2)*3,*b=a+1920*3;uint8_t *d=small+(size_t)y*320*3;
        for(unsigned x=0;x<320;x++,a+=18,b+=18,d+=3)
            for(unsigned c=0;c<3;c++)d[c]=(uint8_t)((a[c]+a[c+3]+b[c]+b[c+3]+2u)>>2);
    }
    return 0;
}
