/* One main-thread HDMI surface. BGR input is swizzled in the GPU shader. */
#define _POSIX_C_SOURCE 200809L
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/keysym.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include "hdmi_egl_types.h"

static Display *xd;
static Window window;
static Colormap colormap;
static Atom delete_window;
static EGLDisplay ed=EGL_NO_DISPLAY;
static EGLContext context=EGL_NO_CONTEXT;
static EGLSurface surface=EGL_NO_SURFACE;
static int initialized,current,width,height,tw[2],th[2],clip[4];
static GLuint image_program,color_program,overlay_program,textures[2],overlay_textures[2];
static int atlas_width[2],atlas_height[2];
static GLint color_location;
static char error_text[512];
static GLfloat glyph_vertices[40*35*12];

const char *pcb_hdmi_error(void) { return error_text; }
void pcb_hdmi_close(void) {
    if(current) {
        if(image_program)glDeleteProgram(image_program);
        if(color_program)glDeleteProgram(color_program);
        if(overlay_program)glDeleteProgram(overlay_program);
        if(textures[0]||textures[1])glDeleteTextures(2,textures);
        if(overlay_textures[0]||overlay_textures[1])glDeleteTextures(2,overlay_textures);
        eglMakeCurrent(ed,EGL_NO_SURFACE,EGL_NO_SURFACE,EGL_NO_CONTEXT);
    }
    if(surface!=EGL_NO_SURFACE)eglDestroySurface(ed,surface);
    if(context!=EGL_NO_CONTEXT)eglDestroyContext(ed,context);
    if(initialized)eglTerminate(ed);
    if(xd) {
        if(window)XDestroyWindow(xd,window);
        if(colormap)XFreeColormap(xd,colormap);
        XCloseDisplay(xd);
    }
    xd=NULL;window=0;colormap=0;current=0;initialized=0;
    ed=EGL_NO_DISPLAY;context=EGL_NO_CONTEXT;surface=EGL_NO_SURFACE;
    image_program=0;color_program=0;overlay_program=0;memset(textures,0,sizeof textures);memset(overlay_textures,0,sizeof overlay_textures);
    memset(tw,0,sizeof tw);memset(th,0,sizeof th);
    memset(atlas_width,0,sizeof atlas_width);memset(atlas_height,0,sizeof atlas_height);
}
static int fail(const char *message) {
    if(message!=error_text)snprintf(error_text,sizeof error_text,"%s",message);
    pcb_hdmi_close();return -1;
}
static GLuint shader(GLenum type,const char *source) {
    GLuint value=glCreateShader(type);GLint ok=0;
    glShaderSource(value,1,&source,NULL);glCompileShader(value);glGetShaderiv(value,GL_COMPILE_STATUS,&ok);
    if(!ok) { glGetShaderInfoLog(value,sizeof error_text,NULL,error_text);glDeleteShader(value);return 0; }
    return value;
}
static GLuint program(const char *vertex,const char *fragment) {
    GLuint vs=shader(GL_VERTEX_SHADER,vertex),fs=shader(GL_FRAGMENT_SHADER,fragment);
    if(!vs||!fs) { if(vs)glDeleteShader(vs);if(fs)glDeleteShader(fs);return 0; }
    GLuint value=glCreateProgram();GLint ok=0;
    glAttachShader(value,vs);glAttachShader(value,fs);glBindAttribLocation(value,0,"position");glBindAttribLocation(value,1,"texcoord");
    glLinkProgram(value);glDeleteShader(vs);glDeleteShader(fs);glGetProgramiv(value,GL_LINK_STATUS,&ok);
    if(!ok) { glGetProgramInfoLog(value,sizeof error_text,NULL,error_text);glDeleteProgram(value);return 0; }
    return value;
}
int pcb_hdmi_open(int w,int h,int x,int y) {
    if(xd) { snprintf(error_text,sizeof error_text,"Duplicate HDMI surface");return -1; }
    if(w<=0||h<=0||w>8192||h>8192)return fail("Invalid HDMI dimensions");
    width=w;height=h;error_text[0]=0;xd=XOpenDisplay(NULL);
    if(!xd)return fail("XOpenDisplay failed");
    PFNEGLGETPLATFORMDISPLAYEXTPROC platform=(PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress("eglGetPlatformDisplayEXT");
    if(platform)ed=platform(EGL_PLATFORM_X11_EXT,xd,NULL);
    if(ed==EGL_NO_DISPLAY)ed=eglGetDisplay((EGLNativeDisplayType)xd);
    if(ed==EGL_NO_DISPLAY||!eglInitialize(ed,NULL,NULL))return fail("EGL X11 initialization failed");
    initialized=1;
    if(!eglBindAPI(EGL_OPENGL_ES_API))return fail("Cannot select OpenGL ES");
    const EGLint attrs[]={EGL_SURFACE_TYPE,EGL_WINDOW_BIT,EGL_RENDERABLE_TYPE,EGL_OPENGL_ES2_BIT,EGL_RED_SIZE,8,EGL_GREEN_SIZE,8,EGL_BLUE_SIZE,8,EGL_NONE};
    EGLConfig configs[512],config=NULL;EGLint count=0,id=0;
    if(!eglChooseConfig(ed,attrs,configs,512,&count)||!count)return fail("No EGL RGB8 window configuration");
    for(int i=0;i<count;i++) {
        EGLint red,green,blue,alpha;eglGetConfigAttrib(ed,configs[i],EGL_RED_SIZE,&red);eglGetConfigAttrib(ed,configs[i],EGL_GREEN_SIZE,&green);
        eglGetConfigAttrib(ed,configs[i],EGL_BLUE_SIZE,&blue);eglGetConfigAttrib(ed,configs[i],EGL_ALPHA_SIZE,&alpha);
        if(red==8&&green==8&&blue==8&&!alpha) { config=configs[i];break; }
    }
    if(!config)config=configs[0];
    eglGetConfigAttrib(ed,config,EGL_NATIVE_VISUAL_ID,&id);
    XVisualInfo request={0};request.visualid=id?(VisualID)id:XVisualIDFromVisual(DefaultVisual(xd,DefaultScreen(xd)));
    int matches=0;XVisualInfo *visual=XGetVisualInfo(xd,VisualIDMask,&request,&matches);
    if(!visual)return fail("No X11 visual for EGL configuration");
    Window root=RootWindow(xd,visual->screen);colormap=XCreateColormap(xd,root,visual->visual,AllocNone);
    XSetWindowAttributes attr={0};attr.colormap=colormap;attr.override_redirect=True;attr.background_pixel=0;
    window=XCreateWindow(xd,root,x,y,(unsigned)w,(unsigned)h,0,visual->depth,InputOutput,visual->visual,CWColormap|CWOverrideRedirect|CWBackPixel,&attr);
    XFree(visual);
    if(!window)return fail("Cannot create HDMI X11 window");
    XStoreName(xd,window,"PCB HDMI GPU");XSelectInput(xd,window,KeyPressMask|StructureNotifyMask|ExposureMask);
    delete_window=XInternAtom(xd,"WM_DELETE_WINDOW",False);XSetWMProtocols(xd,window,&delete_window,1);
    Atom bypass=XInternAtom(xd,"_NET_WM_BYPASS_COMPOSITOR",False),cardinal=XInternAtom(xd,"CARDINAL",False);unsigned long one=1;
    XChangeProperty(xd,window,bypass,cardinal,32,PropModeReplace,(unsigned char *)&one,1);XMapRaised(xd,window);XSync(xd,False);
    const EGLint ctx[]={EGL_CONTEXT_CLIENT_VERSION,2,EGL_NONE};context=eglCreateContext(ed,config,EGL_NO_CONTEXT,ctx);
    surface=eglCreateWindowSurface(ed,config,(EGLNativeWindowType)window,NULL);
    if(context==EGL_NO_CONTEXT||surface==EGL_NO_SURFACE||!eglMakeCurrent(ed,surface,surface,context))return fail("Cannot create HDMI EGL context/surface");
    current=1;
    if(!eglSwapInterval(ed,1))return fail("Cannot set HDMI swap interval");
    image_program=program("attribute vec2 position;attribute vec2 texcoord;varying highp vec2 uv;void main(){gl_Position=vec4(position,0.,1.);uv=texcoord;}",
                          "precision highp float;varying highp vec2 uv;uniform sampler2D image;void main(){vec3 c=texture2D(image,uv).rgb;gl_FragColor=vec4(c.b,c.g,c.r,1.);}");
    color_program=program("attribute vec2 position;void main(){gl_Position=vec4(position,0.,1.);}",
                          "precision mediump float;uniform vec4 color;void main(){gl_FragColor=color;}");
    overlay_program=program("attribute vec2 position;attribute vec2 texcoord;varying highp vec2 uv;void main(){gl_Position=vec4(position,0.,1.);uv=texcoord;}",
                            "precision highp float;varying highp vec2 uv;uniform sampler2D image;void main(){gl_FragColor=texture2D(image,uv);}");
    if(!image_program||!color_program||!overlay_program)return fail(error_text);
    color_location=glGetUniformLocation(color_program,"color");glUseProgram(image_program);glUniform1i(glGetUniformLocation(image_program,"image"),0);
    glUseProgram(overlay_program);glUniform1i(glGetUniformLocation(overlay_program,"image"),0);
    glGenTextures(2,textures);glGenTextures(2,overlay_textures);glPixelStorei(GL_UNPACK_ALIGNMENT,1);glDisable(GL_DEPTH_TEST);
    for(int i=0;i<4;i++) {
        glBindTexture(GL_TEXTURE_2D,i<2?textures[i]:overlay_textures[i-2]);glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_LINEAR);glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_LINEAR);
        glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_S,GL_CLAMP_TO_EDGE);glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_T,GL_CLAMP_TO_EDGE);
    }
    return 0;
}
static int check_gl(void) {
    GLenum code=glGetError();if(code==GL_NO_ERROR)return 0;
    snprintf(error_text,sizeof error_text,"HDMI OpenGL error 0x%x",code);return -1;
}
static GLfloat px(float x) { return 2*x/width-1; }
static GLfloat py(float y) { return 1-2*y/height; }
int pcb_hdmi_upload(int channel,const uint8_t *pixels,int w,int h) {
    if(!current||channel<0||channel>1||!pixels||w<=0||h<=0||w>4096||h>4096) { snprintf(error_text,sizeof error_text,"Invalid HDMI image buffer");return -1; }
    glActiveTexture(GL_TEXTURE0);glBindTexture(GL_TEXTURE_2D,textures[channel]);
    if(tw[channel]!=w||th[channel]!=h) {
        glTexImage2D(GL_TEXTURE_2D,0,GL_RGB,w,h,0,GL_RGB,GL_UNSIGNED_BYTE,pixels);tw[channel]=w;th[channel]=h;
    } else glTexSubImage2D(GL_TEXTURE_2D,0,0,0,w,h,GL_RGB,GL_UNSIGNED_BYTE,pixels);
    return check_gl();
}
int pcb_hdmi_clip(int x,int y,int w,int h) {
    if(!current||x<0||y<0||w<=0||h<=0||x+w>width||y+h>height)return -1;
    clip[0]=x;clip[1]=y;clip[2]=w;clip[3]=h;glEnable(GL_SCISSOR_TEST);glScissor(x,height-y-h,w,h);return 0;
}
int pcb_hdmi_begin(void) {
    if(!current)return -1;
    glViewport(0,0,width,height);glBindBuffer(GL_ARRAY_BUFFER,0);glDisable(GL_SCISSOR_TEST);glDisable(GL_BLEND);glClearColor(0,0,0,1);glClear(GL_COLOR_BUFFER_BIT);
    clip[0]=0;clip[1]=0;clip[2]=width;clip[3]=height;return 0;
}
int pcb_hdmi_image(int channel,float x,float y,float w,float h) {
    if(!current||channel<0||channel>1||!tw[channel])return -1;
    const GLfloat positions[]={px(x),py(y),px(x),py(y+h),px(x+w),py(y),px(x+w),py(y+h)};
    const GLfloat coords[]={0,0,0,1,1,0,1,1};glDisable(GL_BLEND);glUseProgram(image_program);
    glActiveTexture(GL_TEXTURE0);glBindTexture(GL_TEXTURE_2D,textures[channel]);
    glEnableVertexAttribArray(0);glEnableVertexAttribArray(1);glVertexAttribPointer(0,2,GL_FLOAT,GL_FALSE,0,positions);glVertexAttribPointer(1,2,GL_FLOAT,GL_FALSE,0,coords);
    glDrawArrays(GL_TRIANGLE_STRIP,0,4);return check_gl();
}
static void color(float red,float green,float blue,float alpha) {
    glUseProgram(color_program);glDisableVertexAttribArray(1);glEnableVertexAttribArray(0);
    glEnable(GL_BLEND);glBlendFunc(GL_SRC_ALPHA,GL_ONE_MINUS_SRC_ALPHA);glUniform4f(color_location,red,green,blue,alpha);
}
static void rectangle(float x0,float y0,float x1,float y1) {
    const GLfloat positions[]={px(x0),py(y0),px(x0),py(y1),px(x1),py(y0),px(x1),py(y1)};
    glVertexAttribPointer(0,2,GL_FLOAT,GL_FALSE,0,positions);glDrawArrays(GL_TRIANGLE_STRIP,0,4);
}
/* Small bitmap font becomes GPU triangles, never a full-frame CPU overlay. */
static const uint8_t glyphs[38][7]={
 {14,17,19,21,25,17,14},{4,12,4,4,4,4,14},{14,17,1,2,4,8,31},{30,1,1,14,1,1,30},{2,6,10,18,31,2,2},
 {31,16,16,30,1,1,30},{14,16,16,30,17,17,14},{31,1,2,4,8,8,8},{14,17,17,14,17,17,14},{14,17,17,15,1,1,14},
 {14,17,17,31,17,17,17},{30,17,17,30,17,17,30},{14,17,16,16,16,17,14},{30,17,17,17,17,17,30},{31,16,16,30,16,16,31},
 {31,16,16,30,16,16,16},{14,17,16,23,17,17,15},{17,17,17,31,17,17,17},{14,4,4,4,4,4,14},{7,2,2,2,2,18,12},
 {17,18,20,24,20,18,17},{16,16,16,16,16,16,31},{17,27,21,21,17,17,17},{17,25,21,19,17,17,17},{14,17,17,17,17,17,14},
 {30,17,17,30,16,16,16},{14,17,17,17,21,18,13},{30,17,17,30,20,18,17},{15,16,16,14,1,1,30},{31,4,4,4,4,4,4},
 {17,17,17,17,17,17,14},{17,17,17,17,17,10,4},{17,17,17,21,21,21,10},{17,17,10,4,10,17,17},{17,17,10,4,4,4,4},
 {31,1,2,4,8,16,31},{0,0,0,0,0,12,12},{0,0,0,31,0,0,0}};
static void text(float x,float y,const char *label) {
    int used=0;
    for(int i=0;label[i]&&i<40;i++) {
        unsigned char ch=(unsigned char)label[i];int index=ch>='0'&&ch<='9'?ch-'0':ch>='A'&&ch<='Z'?ch-'A'+10:ch=='.'?36:ch=='-'?37:-1;
        if(index<0)continue;
        for(int row=0;row<7;row++)for(int col=0;col<5;col++)if(glyphs[index][row]&(1<<(4-col))) {
            float l=x+i*18+col*3,t=y+row*3;
            const GLfloat quad[]={px(l),py(t),px(l),py(t+3),px(l+3),py(t),px(l+3),py(t),px(l),py(t+3),px(l+3),py(t+3)};
            memcpy(glyph_vertices+used,quad,sizeof quad);used+=12;
        }
    }
    if(used) { glVertexAttribPointer(0,2,GL_FLOAT,GL_FALSE,0,glyph_vertices);glDrawArrays(GL_TRIANGLES,0,used/2); }
}
int pcb_hdmi_text(float x,float y,const char *label) {
    if(!current||!label)return -1;
    color(.75f,.75f,.75f,1);text(x,y,label);return check_gl();
}
int pcb_hdmi_box(float x0,float y0,float x1,float y1,int cid,const char *label) {
    if(!current||cid<0||cid>2||!label)return -1;
    static const float rgb[3][3]={{0,230.f/255,80.f/255},{1,77.f/255,79.f/255},{1,212.f/255,59.f/255}};
    color(rgb[cid][0],rgb[cid][1],rgb[cid][2],1);
    rectangle(x0,y0,x1,y0+3);rectangle(x0,y1-3,x1,y1);rectangle(x0,y0,x0+3,y1);rectangle(x1-3,y0,x1,y1);
    float label_w=(float)strnlen(label,40)*18+8,x=x0,y=y0-29;
    if(x+label_w>clip[0]+clip[2])x=clip[0]+clip[2]-label_w;
    if(x<clip[0])x=(float)clip[0];
    if(y<clip[1])y=(float)clip[1];
    color(.025f,.08f,.12f,.9f);rectangle(x,y,x+label_w,y+27);
    color(rgb[cid][0],rgb[cid][1],rgb[cid][2],1);text(x+4,y+3,label);return check_gl();
}
int pcb_hdmi_present(void) {
    if(!current)return -1;
    if(!eglSwapBuffers(ed,surface)) { snprintf(error_text,sizeof error_text,"HDMI swap failed: 0x%x",eglGetError());return -1; }
    return 0;
}
static double seconds(void) { struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec*1e-9; }
/* One ctypes call keeps all GPU submission outside the Python GIL. */
int pcb_hdmi_frame(const PcbHdmiImage *images,const PcbHdmiAtlas *atlases,const PcbHdmiSprite *sprites,int count,double *timings) {
    if(!current||!images||!atlases||!timings||count<0||count>2048||(!sprites&&count))return -1;
    double started=seconds(),upload_ms=0,atlas_ms=0;
    if(pcb_hdmi_begin()<0)return -1;
    for(int c=0;c<2;c++) {
        const PcbHdmiImage *image=images+c;const PcbHdmiAtlas *atlas=atlases+c;
        if(!image->visible)continue;
        if(pcb_hdmi_clip(image->x,image->y,image->w,image->h)<0)return -1;
        if(image->visible==2) { if(pcb_hdmi_text(image->x+40,image->y+image->h/2,c?"B NO SIGNAL":"A NO SIGNAL")<0)return -1;continue; }
        if(image->upload) { double before=seconds();if(pcb_hdmi_upload(c,image->pixels,image->width,image->height)<0)return -1;upload_ms+=(seconds()-before)*1000; }
        if(pcb_hdmi_image(c,image->x,image->y,image->w,image->h)<0)return -1;
        if(atlas->upload) {
            if(!atlas->pixels||atlas->width<=0||atlas->height<=0||atlas->width>4096||atlas->height>4096)return -1;
            double before=seconds();glBindTexture(GL_TEXTURE_2D,overlay_textures[c]);
            if(atlas_width[c]!=atlas->width||atlas_height[c]!=atlas->height) {
                glTexImage2D(GL_TEXTURE_2D,0,GL_RGBA,atlas->width,atlas->height,0,GL_RGBA,GL_UNSIGNED_BYTE,atlas->pixels);atlas_width[c]=atlas->width;atlas_height[c]=atlas->height;
            } else glTexSubImage2D(GL_TEXTURE_2D,0,0,0,atlas->width,atlas->height,GL_RGBA,GL_UNSIGNED_BYTE,atlas->pixels);
            atlas_ms+=(seconds()-before)*1000;
        }
        glUseProgram(overlay_program);glActiveTexture(GL_TEXTURE0);glBindTexture(GL_TEXTURE_2D,overlay_textures[c]);
        glEnable(GL_BLEND);glBlendFunc(GL_ONE,GL_ONE_MINUS_SRC_ALPHA);glEnableVertexAttribArray(0);glEnableVertexAttribArray(1);
        for(int i=0;i<count;i++) {
            const PcbHdmiSprite *s=sprites+i;if(s->camera!=c)continue;
            if(!atlas_width[c]||s->w<=0||s->h<=0)return -1;
            const GLfloat vertices[]={px(s->x),py(s->y),px(s->x),py(s->y+s->h),px(s->x+s->w),py(s->y),px(s->x+s->w),py(s->y+s->h)};
            const GLfloat uv[]={s->u0,s->v0,s->u0,s->v1,s->u1,s->v0,s->u1,s->v1};
            glVertexAttribPointer(0,2,GL_FLOAT,GL_FALSE,0,vertices);glVertexAttribPointer(1,2,GL_FLOAT,GL_FALSE,0,uv);glDrawArrays(GL_TRIANGLE_STRIP,0,4);
        }
    }
    if(check_gl()<0)return -1;
    double swapped=seconds();if(pcb_hdmi_present()<0)return -1;double finished=seconds();
    timings[0]=upload_ms;timings[1]=atlas_ms;timings[2]=(swapped-started)*1000-upload_ms-atlas_ms;
    timings[3]=(finished-swapped)*1000;timings[4]=(finished-started)*1000;return 0;
}
int pcb_hdmi_poll(void) {
    if(!xd)return 0;
    while(XPending(xd)) {
        XEvent event;XNextEvent(xd,&event);
        if(event.type==KeyPress&&XLookupKeysym(&event.xkey,0)==XK_Escape)return 27;
        if(event.type==ClientMessage&&(Atom)event.xclient.data.l[0]==delete_window)return 27;
        if(event.type==DestroyNotify) { window=0;return 27; }
    }
    return 0;
}
