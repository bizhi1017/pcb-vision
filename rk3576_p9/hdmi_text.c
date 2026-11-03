/* CPU-only UTF-8 labels; no X11/GPU/camera/NPU initialization. */
#include <pango/pangocairo.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>

int pcb_station_label(const char *text,int size,int red,int green,int blue,
                      uint8_t *output,size_t capacity,int *out_width,int *out_height) {
    if(!text||!out_width||!out_height||size<8||size>48||strlen(text)>512||!g_utf8_validate(text,-1,NULL))return -1;
    cairo_surface_t *measure=cairo_image_surface_create(CAIRO_FORMAT_ARGB32,1,1);
    cairo_t *cr=cairo_create(measure);PangoLayout *layout=pango_cairo_create_layout(cr);
    PangoFontDescription *font=pango_font_description_new();pango_font_description_set_family(font,"WenQuanYi Zen Hei");
    pango_font_description_set_weight(font,PANGO_WEIGHT_BOLD);pango_font_description_set_absolute_size(font,size*PANGO_SCALE);
    pango_layout_set_font_description(layout,font);pango_font_description_free(font);pango_layout_set_text(layout,text,-1);
    int tw,th;pango_layout_get_pixel_size(layout,&tw,&th);int width=tw+10,height=th+4;
    int status=0;
    if(pango_layout_get_unknown_glyphs_count(layout))status=-2;
    if(width<=0||height<=0||width>2048||height>128)status=-1;
    *out_width=width;*out_height=height;size_t needed=(size_t)width*(size_t)height*4;
    if(status<0) { g_object_unref(layout);cairo_destroy(cr);cairo_surface_destroy(measure);return status; }
    if(output) {
        if(capacity<needed) { g_object_unref(layout);cairo_destroy(cr);cairo_surface_destroy(measure);return -1; }
        int stride=cairo_format_stride_for_width(CAIRO_FORMAT_ARGB32,width);
        if(stride!=width*4) { g_object_unref(layout);cairo_destroy(cr);cairo_surface_destroy(measure);return -1; }
        /* Use the same rounded 8-bit premultiplication as the workstation. */
        const uint32_t background=(225u<<24)|(8u<<16)|(21u<<8)|34u;
        for(size_t i=0;i<(size_t)width*(size_t)height;i++)memcpy(output+i*4,&background,4);
        cairo_surface_t *surface=cairo_image_surface_create_for_data(output,CAIRO_FORMAT_ARGB32,width,height,stride);
        cairo_t *paint=cairo_create(surface);
        cairo_set_operator(paint,CAIRO_OPERATOR_OVER);cairo_set_source_rgb(paint,red/255.,green/255.,blue/255.);
        cairo_move_to(paint,5,2);pango_cairo_update_layout(paint,layout);pango_cairo_show_layout(paint,layout);
        cairo_surface_flush(surface);
        status=cairo_status(paint)==CAIRO_STATUS_SUCCESS&&cairo_surface_status(surface)==CAIRO_STATUS_SUCCESS?0:-1;
        cairo_destroy(paint);cairo_surface_destroy(surface);
        if(!status)for(size_t i=0;i<(size_t)width*(size_t)height;i++) {
            uint32_t argb;memcpy(&argb,output+i*4,4);
            output[i*4]=(argb>>16)&255;output[i*4+1]=(argb>>8)&255;output[i*4+2]=argb&255;output[i*4+3]=(argb>>24)&255;
        }
    }
    g_object_unref(layout);cairo_destroy(cr);cairo_surface_destroy(measure);
    return status<0?status:(int)needed;
}
