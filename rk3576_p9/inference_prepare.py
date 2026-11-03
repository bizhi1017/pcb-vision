"""Reusable CPU input buffers; exact runtime validation before using fast pixels."""
from pathlib import Path
import ctypes as C
import cv2,numpy as np

MODEL_SHAPE=(640,640,3)
TRACKING_SHAPE=(180,320,3)
MODEL_BYTES=640*640*3
COMPACT_BYTES=MODEL_BYTES+180*320*3

class InputPreparer:
    def __init__(self,library=None):
        self.library=None;self.validated=None;self.error='';self.backend='OpenCV'
        try:
            lib=C.CDLL(str(library or Path(__file__).with_name('libpcb_inference_cpu.so')))
            lib.pcb_prepare_1080.argtypes=[C.c_void_p,C.c_size_t,C.c_void_p,C.c_size_t,C.c_void_p,C.c_size_t]
            lib.pcb_prepare_1080.restype=C.c_int;self.library=lib
            self.rgb=np.empty((640,640,3),np.uint8);self.small=np.empty((180,320,3),np.uint8)
        except (OSError,AttributeError) as error:self.error=str(error)

    def prepare(self,frame,legacy):
        if self.library is not None and self.validated is not False and frame.dtype==np.uint8 and frame.shape==(1080,1920,3) and frame.flags.c_contiguous:
            rc=self.library.pcb_prepare_1080(frame.ctypes.data,frame.nbytes,self.rgb.ctypes.data,self.rgb.nbytes,self.small.ctypes.data,self.small.nbytes)
            if rc==0:
                reference=None
                if self.validated is None:
                    image,r,dw,dh=legacy.letterbox(frame,640);reference=cv2.cvtColor(image,cv2.COLOR_BGR2RGB)
                    self.validated=((r,dw,dh)==(640/1920,0.,140.) and np.array_equal(reference,self.rgb)
                        and np.array_equal(cv2.resize(frame,(320,180),interpolation=cv2.INTER_LINEAR),self.small))
                    if not self.validated:self.error='Native model/reference pixels differ from OpenCV'
                if self.validated:
                    self.backend='native exact validated';return self.rgb,640/1920,0.,140.,self.small
                self.backend='OpenCV fallback';return reference,r,dw,dh,None
            self.validated=False;self.error='CPU input preparation failed '+str(rc)
        image,r,dw,dh=legacy.letterbox(frame,640)
        self.backend='OpenCV fallback' if self.validated is False else 'OpenCV'
        return cv2.cvtColor(image,cv2.COLOR_BGR2RGB),r,dw,dh,None
