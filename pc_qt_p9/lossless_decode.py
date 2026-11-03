"""Bounded, independently checked PCZ1 lossless frame decoding to exact RGB8."""
import ctypes as C
import ctypes.util
from pathlib import Path
import struct
import sys
import threading
import zlib
import numpy as np

HEADER=struct.Struct('!4s7I')
MAX_BYTES=32*1024*1024
_local=threading.local()


def _library():
    if hasattr(_local,'library'):return _local.library
    name=str(Path(__file__).with_name('native')/'zstd.dll') if sys.platform=='win32' else C.util.find_library('zstd')
    if not name:raise RuntimeError('Zstd decoder unavailable')
    lib=C.CDLL(name)
    lib.ZSTD_createDCtx.argtypes=[];lib.ZSTD_createDCtx.restype=C.c_void_p
    lib.ZSTD_freeDCtx.argtypes=[C.c_void_p];lib.ZSTD_freeDCtx.restype=C.c_size_t
    lib.ZSTD_decompressDCtx.argtypes=[C.c_void_p,C.c_void_p,C.c_size_t,C.c_void_p,C.c_size_t];lib.ZSTD_decompressDCtx.restype=C.c_size_t
    lib.ZSTD_getFrameContentSize.argtypes=[C.c_void_p,C.c_size_t];lib.ZSTD_getFrameContentSize.restype=C.c_ulonglong
    lib.ZSTD_findFrameCompressedSize.argtypes=[C.c_void_p,C.c_size_t];lib.ZSTD_findFrameCompressedSize.restype=C.c_size_t
    lib.ZSTD_isError.argtypes=[C.c_size_t];lib.ZSTD_isError.restype=C.c_uint
    _local.library=lib;return lib


class _Context:
    def __init__(self,lib):
        self.lib=lib;self.pointer=lib.ZSTD_createDCtx()
        if not self.pointer:raise MemoryError('Zstd decoder allocation failed')
    def __del__(self):
        if getattr(self,'pointer',None):self.lib.ZSTD_freeDCtx(self.pointer);self.pointer=None


def available():
    try:_library();return True
    except (OSError,AttributeError,RuntimeError):return False


def decode_rgb(data):
    if not isinstance(data,bytes) or not HEADER.size<len(data)<=MAX_BYTES:raise ValueError('invalid lossless frame length')
    magic,w,h,raw_size,stored,checksum,transform,header_crc=HEADER.unpack_from(data)
    if (magic!=b'PCZ1' or not 0<w<=4096 or not 0<h<=4096 or raw_size!=w*h*3 or raw_size>MAX_BYTES
        or stored!=len(data)-HEADER.size or transform!=3 or zlib.crc32(data[:28])!=header_crc):raise ValueError('invalid lossless frame header')
    lib=_library();payload=data[HEADER.size:]
    if len(payload)<12:raise ValueError('missing lossless plane sizes')
    sizes=struct.unpack_from('!3I',payload)
    if any(not size for size in sizes) or sum(sizes)+12!=len(payload):raise ValueError('invalid lossless plane sizes')
    if not hasattr(_local,'context'):_local.context=_Context(lib)
    raw=np.empty(raw_size,np.uint8)
    at=12
    for i,size in enumerate(sizes):
        chunk=payload[at:at+size];at+=size
        start=h*i//3;end=h*(i+1)//3;expected=(end-start)*w*3
        if lib.ZSTD_getFrameContentSize(chunk,size)!=expected:raise ValueError('lossless content size mismatch')
        compressed=lib.ZSTD_findFrameCompressedSize(chunk,size)
        if lib.ZSTD_isError(compressed) or compressed!=size:raise ValueError('invalid or trailing lossless data')
        written=lib.ZSTD_decompressDCtx(_local.context.pointer,raw.ctypes.data+start*w*3,expected,chunk,size)
        if lib.ZSTD_isError(written) or written!=expected:raise ValueError('lossless decompression failed')
    if zlib.crc32(raw)!=checksum:raise ValueError('lossless pixel checksum mismatch')
    rgb=np.empty((h,w,3),np.uint8)
    for i in range(3):
        start=h*i//3;end=h*(i+1)//3
        planes=raw[start*w*3:end*w*3].reshape(3,end-start,w)
        np.cumsum(planes,axis=2,dtype=np.uint8,out=planes)
        rgb[start:end,:,1]=planes[0]
        np.add(planes[2],planes[0],out=rgb[start:end,:,0])
        np.add(planes[1],planes[0],out=rgb[start:end,:,2])
    return rgb
