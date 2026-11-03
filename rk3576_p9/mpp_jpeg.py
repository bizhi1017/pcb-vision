"""Optional RK hardware JPEG, software fallback is handled by NetworkEncoder."""
import ctypes as C
import time,os
from collections import OrderedDict
from pathlib import Path
import cv2,numpy as np


def bgr_to_nv12(image):
    h,w=image.shape[:2]
    if h%2 or w%2:raise ValueError('NV12 requires even image size')
    planar=cv2.cvtColor(image,cv2.COLOR_BGR2YUV_I420).reshape(-1)
    result=np.empty((h*3//2,w),np.uint8);flat=result.reshape(-1);n=w*h
    flat[:n]=planar[:n];flat[n::2]=planar[n:n+n//4];flat[n+1::2]=planar[n+n//4:]
    return result


class HardwareJPEG:
    def __init__(self):
        self.context=None;self.shape=None;self.error='';self.library=None;self.checked=False;self.timings={};self.planar=False
        self.rga=False;self.rga_disabled=False;self.rga_checked=False;self.rga_error=''
        self.direct=False;self.direct_disabled=False;self.direct_error='';self.source_buffers=OrderedDict()
        try:
            lib=C.CDLL(str(Path(__file__).with_name('libpcb_mpp_jpeg.so')))
            lib.pcb_jpeg_create.argtypes=[C.c_int,C.c_int,C.c_int];lib.pcb_jpeg_create.restype=C.c_void_p
            if hasattr(lib,'pcb_jpeg_create_i420'):
                lib.pcb_jpeg_create_i420.argtypes=[C.c_int,C.c_int,C.c_int];lib.pcb_jpeg_create_i420.restype=C.c_void_p;self.planar=True
            lib.pcb_jpeg_encode.argtypes=[C.c_void_p,C.c_void_p,C.c_size_t,C.c_int,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)]
            lib.pcb_jpeg_encode.restype=C.c_int;lib.pcb_jpeg_free.argtypes=[C.c_void_p];lib.pcb_jpeg_free.restype=None
            if hasattr(lib,'pcb_jpeg_buffer_cached'):
                lib.pcb_jpeg_buffer_cached.argtypes=[C.c_void_p];lib.pcb_jpeg_buffer_cached.restype=C.c_int
            if hasattr(lib,'pcb_jpeg_pipeline_stats'):
                lib.pcb_jpeg_pipeline_stats.argtypes=[C.c_void_p,C.POINTER(C.c_double),C.c_size_t];lib.pcb_jpeg_pipeline_stats.restype=C.c_int
            if self.planar and hasattr(lib,'pcb_jpeg_encode_bgr'):
                lib.pcb_jpeg_encode_bgr.argtypes=lib.pcb_jpeg_encode.argtypes+[C.POINTER(C.c_double),C.POINTER(C.c_double)]
                lib.pcb_jpeg_encode_bgr.restype=C.c_int;self.rga=True
            if self.rga and all(hasattr(lib,n) for n in ('pcb_jpeg_import_source','pcb_jpeg_release_source','pcb_jpeg_encode_source')):
                lib.pcb_jpeg_import_source.argtypes=[C.c_void_p,C.c_void_p,C.c_size_t];lib.pcb_jpeg_import_source.restype=C.c_uint64
                lib.pcb_jpeg_release_source.argtypes=[C.c_uint64];lib.pcb_jpeg_release_source.restype=None
                lib.pcb_jpeg_encode_source.argtypes=[C.c_void_p,C.c_uint64,C.c_int,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t),C.POINTER(C.c_double),C.POINTER(C.c_double)]
                lib.pcb_jpeg_encode_source.restype=C.c_int;self.direct=True
            self.library=lib
        except OSError as exc:self.error=str(exc)

    def close(self):
        for storage,handle in self.source_buffers.values():self.library.pcb_jpeg_release_source(handle)
        self.source_buffers.clear()
        if self.context:self.library.pcb_jpeg_free(self.context);self.context=None

    def source_handle(self,image):
        if not self.direct or self.direct_disabled or os.environ.get('PCB_JPEG_DIRECT_INPUT')=='0':return 0
        from shared_image import SharedStorage
        storage=image
        while getattr(storage,'base',None) is not None:storage=storage.base
        # Ordinary/temporary arrays retain the existing DMA-copy path. Holding
        # the storage, rather than its ndarray root, still lets the pool reuse
        # that buffer after this synchronous encode returns.
        if not isinstance(storage,SharedStorage):return 0
        key=(image.ctypes.data,image.nbytes)
        if key in self.source_buffers:
            self.source_buffers.move_to_end(key);return self.source_buffers[key][1]
        handle=self.library.pcb_jpeg_import_source(self.context,image.ctypes.data,image.nbytes)
        if not handle:
            self.direct_disabled=True;self.direct_error='RGA HQ virtual import failed';return 0
        self.source_buffers[key]=(storage,handle)
        while len(self.source_buffers)>16:
            _,(_,old)=self.source_buffers.popitem(last=False);self.library.pcb_jpeg_release_source(old)
        return handle

    def encode(self,image,quality):
        if self.library is None:return None
        h,w=image.shape[:2]
        if self.shape!=(h,w):
            self.close();create=self.library.pcb_jpeg_create_i420 if self.planar else self.library.pcb_jpeg_create
            self.context=create(w,h,quality);self.shape=(h,w);self.checked=False;self.rga_checked=False
            if not self.context:self.error='MPP context creation failed';self.library=None;return None
            self.output=np.empty((h*w*3+8192,),np.uint8)
        started=time.perf_counter()
        length=C.c_size_t();used_rga=self.rga and not self.rga_disabled;nv12=None
        if used_rga:
            image=np.ascontiguousarray(image);color=C.c_double();hardware=C.c_double()
            importing=time.perf_counter();handle=self.source_handle(image);import_ms=(time.perf_counter()-importing)*1000
            if handle:
                rc=self.library.pcb_jpeg_encode_source(self.context,handle,quality,self.output.ctypes.data,self.output.nbytes,C.byref(length),C.byref(color),C.byref(hardware))
                if rc:
                    self.direct_disabled=True;self.direct_error='RGA HQ source conversion failed '+str(rc);handle=0
            if not handle:
                rc=self.library.pcb_jpeg_encode_bgr(self.context,image.ctypes.data,image.nbytes,quality,self.output.ctypes.data,self.output.nbytes,C.byref(length),C.byref(color),C.byref(hardware))
            if rc:
                self.rga_disabled=True;self.rga_error='RGA conversion failed '+str(rc);self.checked=False
                return self.encode(image,quality)
            self.timings=dict(color_ms=round(color.value,2),hardware_ms=round(hardware.value,2),input_format='I420',color_backend='RGA BT601 limited',rga_error='',
                              input_backend='shared HQ virtual import' if handle else 'legacy DMA copy',direct_input_error=self.direct_error,source_imports=len(self.source_buffers),source_import_ms=round(import_ms,2))
        else:
            nv12=cv2.cvtColor(image,cv2.COLOR_BGR2YUV_I420) if self.planar else bgr_to_nv12(image)
            converted=time.perf_counter()
            rc=self.library.pcb_jpeg_encode(self.context,nv12.ctypes.data,nv12.nbytes,quality,self.output.ctypes.data,self.output.nbytes,C.byref(length))
            self.timings=dict(color_ms=round((converted-started)*1000,2),hardware_ms=round((time.perf_counter()-converted)*1000,2),input_format='I420' if self.planar else 'NV12',color_backend='OpenCV CPU',rga_error=self.rga_error)
        if rc:
            self.error='MPP encode failed '+str(rc);self.close();self.library=None;return None
        self.timings['buffer_policy']=('cached DMA / explicit sync' if self.library.pcb_jpeg_buffer_cached(self.context) else 'legacy DMA') if hasattr(self.library,'pcb_jpeg_buffer_cached') else 'legacy DMA'
        if hasattr(self.library,'pcb_jpeg_pipeline_stats'):
            values=(C.c_double*4)()
            if self.library.pcb_jpeg_pipeline_stats(self.context,values,4)==0:
                self.timings.update({k:round(v,2) for k,v in zip(('input_copy_ms','input_sync_ms','rga_ms','output_cpu_ms'),values)})
        data=self.output[:length.value].tobytes()
        if not self.checked or (used_rga and not self.rga_checked):
            decoded=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
            if nv12 is None:nv12=cv2.cvtColor(image,cv2.COLOR_BGR2YUV_I420)
            expected=cv2.cvtColor(nv12,cv2.COLOR_YUV2BGR_I420 if self.planar else cv2.COLOR_YUV2BGR_NV12)
            threshold=26 if quality>=90 else 18
            if decoded is None or decoded.shape!=image.shape or cv2.PSNR(expected,decoded)<threshold:
                if used_rga:
                    self.rga_disabled=True;self.rga_error='RGA color/reference validation failed';self.checked=False
                    return self.encode(image,quality)
                self.error='MPP JPEG dimension/color self-check failed';self.close();self.library=None;return None
            self.checked=True
            self.rga_checked=used_rga
        return data
