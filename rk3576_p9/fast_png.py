"""Standard PNG with byte-exact decoded BGR; native filtering/libdeflate optional."""
import ctypes as C
from pathlib import Path
import threading
import cv2
import numpy as np


class FastPNG:
    def __init__(self,library=None,level=1,filter_type=1,workers=0):
        if (type(level)is not int or not 1<=level<=12 or type(filter_type)is not int or filter_type not in (0,1,2)
            or type(workers)is not int or workers not in (0,2,3,4)):
            raise ValueError('invalid PNG compression options')
        self.lock=threading.Lock();self.library=None;self.context=None;self.output=None;self.capacity=0
        self.closed=False;self.error='';self.backend='OpenCV PNG (RLE)'
        try:
            lib=C.CDLL(str(library or Path(__file__).with_name('libpcb_fast_png.so')))
            lib.pcb_png_create.argtypes=[C.c_int,C.c_int];lib.pcb_png_create.restype=C.c_void_p
            lib.pcb_png_create_parallel.argtypes=[C.c_int,C.c_int];lib.pcb_png_create_parallel.restype=C.c_void_p
            lib.pcb_png_destroy.argtypes=[C.c_void_p];lib.pcb_png_destroy.restype=None
            lib.pcb_png_bound.argtypes=[C.c_void_p,C.c_uint,C.c_uint];lib.pcb_png_bound.restype=C.c_size_t
            lib.pcb_png_encode.argtypes=[C.c_void_p,C.c_void_p,C.c_size_t,C.c_uint,C.c_uint,C.c_size_t,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)]
            lib.pcb_png_encode.restype=C.c_int
            context=lib.pcb_png_create_parallel(workers,filter_type) if workers else lib.pcb_png_create(level,filter_type)
            if not context:raise RuntimeError('PNG compressor allocation failed')
            self.library=lib;self.context=context
            self.backend=('Parallel PNG RLE '+str(workers) if workers else 'libdeflate PNG')+' ('+('NONE','SUB','UP')[filter_type]+')'
        except (OSError,AttributeError,RuntimeError) as error:self.error=str(error)

    def _disable_native(self):
        if self.context is not None:self.library.pcb_png_destroy(self.context);self.context=None
        self.output=None;self.capacity=0

    def encode(self,image):
        if (not isinstance(image,np.ndarray) or image.dtype!=np.uint8 or image.ndim!=3 or image.shape[2]!=3
            or min(image.shape[:2])<1 or max(image.shape[:2])>4096 or image.nbytes>31*1024*1024):
            raise ValueError('PNG requires a bounded nonempty RGB8/BGR image')
        image=np.ascontiguousarray(image);height,width=image.shape[:2]
        with self.lock:
            if self.closed:raise RuntimeError('PNG encoder is closed')
            if self.context is not None:
                try:
                    bound=self.library.pcb_png_bound(self.context,width,height)
                    if not bound:raise RuntimeError('PNG dimensions exceed encoder bounds')
                    if self.capacity<bound:self.output=C.create_string_buffer(bound);self.capacity=bound
                    written=C.c_size_t()
                    code=self.library.pcb_png_encode(self.context,image.ctypes.data,image.nbytes,width,height,image.strides[0],self.output,self.capacity,C.byref(written))
                    if code or not 0<written.value<=self.capacity:raise RuntimeError('Native PNG encoding failed: '+str(code))
                    return C.string_at(self.output,written.value)
                except (MemoryError,RuntimeError) as error:
                    self.error=str(error);self._disable_native();self.backend='OpenCV PNG (RLE)'
            options=[cv2.IMWRITE_PNG_COMPRESSION,1,cv2.IMWRITE_PNG_STRATEGY,cv2.IMWRITE_PNG_STRATEGY_RLE]
            if hasattr(cv2,'IMWRITE_PNG_FILTER'):options.extend([cv2.IMWRITE_PNG_FILTER,cv2.IMWRITE_PNG_FILTER_SUB])
            ok,data=cv2.imencode('.png',image,options)
            if not ok:raise RuntimeError('PNG fallback encoding failed')
            return data.tobytes()

    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True;self._disable_native()

    def __del__(self):
        try:self.close()
        except Exception:pass
