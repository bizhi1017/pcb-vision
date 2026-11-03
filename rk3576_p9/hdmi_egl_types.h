#ifndef PCB_HDMI_EGL_TYPES_H
#define PCB_HDMI_EGL_TYPES_H
#include <stdint.h>
typedef struct {
    const uint8_t *pixels;
    int width,height,upload,visible;
    int x,y,w,h;
} PcbHdmiImage;
typedef struct {
    const uint8_t *pixels;
    int width,height,upload;
} PcbHdmiAtlas;
typedef struct {
    int camera;
    float x,y,w,h,u0,v0,u1,v1;
} PcbHdmiSprite;
#endif
