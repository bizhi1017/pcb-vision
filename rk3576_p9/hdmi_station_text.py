"""17px bold Chinese labels with the workstation palette and panel background."""
from functools import lru_cache
from pathlib import Path
import ctypes,os,threading
import numpy as np

_font=None;_lock=threading.Lock()


class StationText:
    def __init__(self):
        self.lib=None
        if os.name=='posix':
            self.lib=ctypes.CDLL(str(Path(__file__).with_name('libpcb_hdmi_text.so')))
            self.lib.pcb_station_label.argtypes=[ctypes.c_char_p]+[ctypes.c_int]*4+[ctypes.c_void_p,ctypes.c_size_t,ctypes.POINTER(ctypes.c_int),ctypes.POINTER(ctypes.c_int)]
            self.lib.pcb_station_label.restype=ctypes.c_int
        else:
            from PIL import ImageFont
            self.font=ImageFont.truetype('C:/Windows/Fonts/msyhbd.ttc',17)

    def render(self,text,color):
        if self.lib is not None:
            width=ctypes.c_int();height=ctypes.c_int();encoded=text.encode('utf-8')
            args=[encoded,17,*color];size=self.lib.pcb_station_label(*args,None,0,ctypes.byref(width),ctypes.byref(height))
            if size<0:raise RuntimeError('HDMI Chinese font unavailable or label invalid (%d)'%size)
            pixels=np.empty((height.value,width.value,4),np.uint8)
            result=self.lib.pcb_station_label(*args,pixels.ctypes.data,pixels.nbytes,ctypes.byref(width),ctypes.byref(height))
            if result!=size:raise RuntimeError('HDMI label raster failed')
        else:
            from PIL import Image,ImageDraw
            x0,y0,x1,y1=self.font.getbbox(text);image=Image.new('RGBA',(x1-x0+10,y1-y0+4),(9,24,38,225))
            ImageDraw.Draw(image).text((5-x0,2-y0),text,font=self.font,fill=(*color,255))
            pixels=np.asarray(image).copy();pixels[:,:,:3]=((pixels[:,:,:3].astype(np.uint16)*pixels[:,:,3:4]+127)//255).astype(np.uint8)
        pixels.setflags(write=False);return pixels


@lru_cache(maxsize=64)
def station_label(text,color):
    global _font
    with _lock:
        if _font is None:_font=StationText()
        return _font.render(text,color)
