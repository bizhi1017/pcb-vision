"""Main-thread X11/EGL texture display, with an explicit OpenCV fallback."""
import ctypes,threading,time
from pathlib import Path
import cv2
import numpy as np
from hdmi_overlay import COLORS
from hdmi_annotations import OpenCvAtlas,composite_annotations


class NativeImage(ctypes.Structure):
    _fields_=[('pixels',ctypes.c_void_p)]+[(n,ctypes.c_int) for n in ('width','height','upload','visible','x','y','w','h')]


class NativeAtlas(ctypes.Structure):
    _fields_=[('pixels',ctypes.c_void_p)]+[(n,ctypes.c_int) for n in ('width','height','upload')]


class NativeSprite(ctypes.Structure):
    _fields_=[('camera',ctypes.c_int)]+[(n,ctypes.c_float) for n in ('x','y','w','h','u0','v0','u1','v1')]


def image_rect(width,height,image,slot=0,split=False):
    left=slot*(width//2) if split else 0
    available=(width-left if slot else width//2) if split else width
    scale=min(available/image.shape[1],height/image.shape[0])
    w,h=round(image.shape[1]*scale),round(image.shape[0]*scale)
    return left+(available-w)//2,(height-h)//2,w,h


class NativeRenderer:
    backend='EGL/GLES2 + workstation-style overlay'
    def __init__(self,width,height,x,y):
        self.width,self.height=width,height;self.owner=threading.get_ident();self.tokens={};self.atlas_tokens={}
        self.atlases={c:OpenCvAtlas() for c in ('A','B')}
        path=Path(__file__).with_name('libpcb_hdmi_egl.so')
        self.lib=ctypes.CDLL(str(path));lib=self.lib
        lib.pcb_hdmi_error.restype=ctypes.c_char_p
        for name in ('pcb_hdmi_open','pcb_hdmi_frame','pcb_hdmi_poll'):
            getattr(lib,name).restype=ctypes.c_int
        lib.pcb_hdmi_open.argtypes=[ctypes.c_int]*4
        lib.pcb_hdmi_frame.argtypes=[ctypes.POINTER(NativeImage),ctypes.POINTER(NativeAtlas),ctypes.POINTER(NativeSprite),ctypes.c_int,ctypes.POINTER(ctypes.c_double)]
        lib.pcb_hdmi_poll.argtypes=[]
        lib.pcb_hdmi_close.argtypes=[];lib.pcb_hdmi_close.restype=None
        self.check(lib.pcb_hdmi_open(width,height,x,y))

    def check(self,result):
        if result<0:raise RuntimeError(self.lib.pcb_hdmi_error().decode('utf-8','replace'))
        return result

    def present(self,mode,sources,annotations):
        assert threading.get_ident()==self.owner,'HDMI renderer must stay on its X11/EGL thread'
        started=time.monotonic();images=(NativeImage*2)();atlases=(NativeAtlas*2)();sprites=[];keepers=[];tokens={};atlas_tokens={}
        for slot,c in enumerate(('A','B') if mode=='split' else (mode,)):
            source=sources[c];image=source.get('image')
            index=('A','B').index(c)
            if image is None:
                left=slot*(self.width//2) if mode=='split' else 0
                images[index]=NativeImage(None,0,0,0,2,left,0,self.width//2 if mode=='split' else self.width,self.height)
                continue
            token=(source['seq'],source['mode']);update=self.tokens.get(c)!=token
            if image.dtype!=np.uint8 or image.ndim!=3 or image.shape[2]!=3:raise RuntimeError('HDMI requires uint8 BGR pixels')
            packed=np.ascontiguousarray(image) if update else image;keepers.append(packed)
            left,top,w,h=image_rect(self.width,self.height,image,slot,mode=='split')
            images[index]=NativeImage(packed.ctypes.data,packed.shape[1],packed.shape[0],int(update),1,left,top,w,h);tokens[c]=token
            prepared=getattr(annotations[c],'atlas',None);origin='worker'
            if prepared is None:prepared=self.atlases[c].prepare(annotations[c],(left,top,w,h)).atlas;origin='inline'
            pixels=prepared['pixels']
            if pixels is not None:
                keepers.append(pixels);version=(origin,prepared['token']);update_atlas=self.atlas_tokens.get(c)!=version
                atlases[index]=NativeAtlas(pixels.ctypes.data,pixels.shape[1],pixels.shape[0],int(update_atlas));atlas_tokens[c]=version
                sprites.extend(NativeSprite(index,*quad) for quad in prepared['quads'])
        data=(NativeSprite*len(sprites))(*sprites);timings=(ctypes.c_double*5)();calling=time.monotonic()
        self.check(self.lib.pcb_hdmi_frame(images,atlases,data,len(sprites),timings))
        self.tokens.update(tokens);self.atlas_tokens.update(atlas_tokens)
        self.timings=dict(upload_ms=round(timings[0],2),opencv_atlas_upload_ms=round(timings[1],2),draw_ms=round(timings[2],2),swap_ms=round(timings[3],2),native_ms=round(timings[4],2),packet_ms=round((calling-started)*1000,2),call_ms=round((time.monotonic()-calling)*1000,2))

    def poll(self):return self.check(self.lib.pcb_hdmi_poll())
    def close(self):self.lib.pcb_hdmi_close();self.tokens.clear();self.atlas_tokens.clear();self.atlases.clear()


class OpenCvRenderer:
    backend='OpenCV software fallback'
    def __init__(self,width,height,x,y):
        self.width,self.height=width,height;self.name='PCB HDMI'
        self.atlases={c:OpenCvAtlas() for c in ('A','B')}
        cv2.namedWindow(self.name,cv2.WINDOW_NORMAL);cv2.resizeWindow(self.name,width,height)
        cv2.moveWindow(self.name,x,y);cv2.setWindowProperty(self.name,cv2.WND_PROP_FULLSCREEN,cv2.WINDOW_FULLSCREEN)

    def present(self,mode,sources,annotations):
        visible=('A','B') if mode=='split' else (mode,)
        if len(visible)==1:
            image=sources[mode].get('image')
            direct=image is not None and image.shape[:2]==(self.height,self.width) and not annotations[mode]
        else:direct=False
        if direct:canvas=image
        else:
            canvas=np.zeros((self.height,self.width,3),np.uint8)
            for slot,c in enumerate(visible):
                image=sources[c].get('image')
                if image is None:
                    cv2.putText(canvas,c+' NO SIGNAL',(slot*(self.width//2)+40,self.height//2),0,1,(190,190,190),2);continue
                left,top,w,h=image_rect(self.width,self.height,image,slot,mode=='split')
                canvas[top:top+h,left:left+w]=image if image.shape[:2]==(h,w) else cv2.resize(image,(w,h))
                composite_annotations(canvas,annotations[c],(left,top,w,h),self.atlases[c])
        cv2.imshow(self.name,canvas)

    def poll(self):return cv2.waitKey(1)
    def close(self):cv2.destroyWindow(self.name)
