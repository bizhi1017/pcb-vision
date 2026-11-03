"""Negotiated full-frame lossless codec. Missing native support returns PNG fallback."""
import ctypes as C
from pathlib import Path
import threading
import numpy as np


class FastLossless:
    def __init__(self,library=None,profile=2):
        # 1: normal Zstd level 1; 2: parallel literal-only Huffman, no LZ search.
        if type(profile)is not int or profile not in (1,2):raise ValueError('invalid lossless profile')
        self.library=None;self.context=None;self.output=None;self.capacity=0;self.error='';self.closed=False
        self.lock=threading.Lock();self.backend='Zstd Huffman lossless (3 bands)' if profile==2 else 'Zstd LZ lossless (3 bands)'
        try:
            lib=C.CDLL(str(library or Path(__file__).with_name('libpcb_fast_lossless.so')))
            lib.pcb_lossless_create.argtypes=[C.c_int];lib.pcb_lossless_create.restype=C.c_void_p
            lib.pcb_lossless_destroy.argtypes=[C.c_void_p];lib.pcb_lossless_destroy.restype=None
            lib.pcb_lossless_bound.argtypes=[C.c_uint,C.c_uint];lib.pcb_lossless_bound.restype=C.c_size_t
            lib.pcb_lossless_encode.argtypes=[C.c_void_p,C.c_void_p,C.c_size_t,C.c_uint,C.c_uint,C.c_size_t,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)]
            lib.pcb_lossless_encode.restype=C.c_int
            context=lib.pcb_lossless_create(profile)
            if not context:raise RuntimeError('lossless compressor allocation failed')
            self.library=lib;self.context=context
        except (OSError,AttributeError,RuntimeError) as error:self.error=str(error)

    def encode(self,image):
        if not isinstance(image,np.ndarray) or image.dtype!=np.uint8 or image.ndim!=3 or image.shape[2]!=3:
            raise ValueError('lossless input must be BGR8')
        h,w=image.shape[:2]
        if not 0<w<=4096 or not 0<h<=4096 or w*h*3>32*1024*1024:raise ValueError('lossless dimensions outside bounds')
        image=np.ascontiguousarray(image)
        with self.lock:
            if self.closed:raise RuntimeError('lossless encoder closed')
            if self.context is None:return None
            bound=self.library.pcb_lossless_bound(w,h)
            if not bound:raise ValueError('lossless dimensions outside bounds')
            if self.capacity<bound:self.output=C.create_string_buffer(bound);self.capacity=bound
            written=C.c_size_t()
            # NumPy may retain a negative stride on a singleton axis even when
            # C-contiguous. The normalized packed buffer always has this pitch.
            code=self.library.pcb_lossless_encode(self.context,image.ctypes.data,image.nbytes,w,h,w*3,self.output,self.capacity,C.byref(written))
            if code or not 32<written.value<=self.capacity:
                self.error='lossless encoding failed: '+str(code);return None
            return C.string_at(self.output,written.value)

    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True
            if self.context is not None:self.library.pcb_lossless_destroy(self.context);self.context=None
            self.output=None;self.capacity=0

    def __del__(self):
        try:self.close()
        except Exception:pass
