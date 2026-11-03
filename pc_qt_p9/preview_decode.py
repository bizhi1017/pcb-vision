"""Decode and prepare Qt's opaque raster off the GUI thread; retain RGB for tracking."""
import os
import threading
import sys
from PyQt5.QtGui import QImage
try:
    from lossless_decode import decode_rgb as decode_lossless_rgb,available as lossless_available
except ImportError:
    # Preserve the Qt JPEG/PNG fallback on installations without NumPy.
    def lossless_available():return False
    def decode_lossless_rgb(data):raise ValueError('高速无损解码不可用')
try:
    import cv2
    import numpy as np
    cv2.setNumThreads(1)
except ImportError:
    cv2=None

DECODER_BACKEND=('OpenCV native / Zstd lossless' if lossless_available() else 'OpenCV native') if cv2 is not None else 'Qt fallback (OpenCV unavailable)'
DECODER_ID='opencv' if cv2 is not None else 'qt'
_live_thread=threading.local()


def configure_live_thread():
    """Keep live video responsive when Windows moves the window to background.

    This is a per-thread QoS hint, not a priority/affinity or system power change.
    Older Windows and other platforms retain their normal scheduler behavior.
    """
    if hasattr(_live_thread,'scheduling'):return _live_thread.scheduling
    # Video receivers, ACK workers and Qt paint callbacks share one interpreter.
    # Short native operations should not wait for a 5ms Python handoff quantum.
    sys.setswitchinterval(.001)
    status='系统默认'
    if os.name=='nt':
        try:
            import ctypes
            from ctypes import wintypes
            class PowerState(ctypes.Structure):
                _fields_=[('Version',wintypes.ULONG),('ControlMask',wintypes.ULONG),('StateMask',wintypes.ULONG)]
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.GetCurrentThread.restype=wintypes.HANDLE
            kernel.SetThreadInformation.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
            kernel.SetThreadInformation.restype=wintypes.BOOL
            # ThreadPowerThrottling=3, EXECUTION_SPEED=1: explicitly disable
            # energy-saving execution for this latency-sensitive video thread.
            state=PowerState(1,1,0)
            ok=kernel.SetThreadInformation(kernel.GetCurrentThread(),3,ctypes.byref(state),ctypes.sizeof(state))
            status='HighQoS' if ok else '系统默认（QoS请求未生效）'
        except (AttributeError,OSError):status='系统默认（不支持QoS）'
    _live_thread.scheduling=status
    return status


def decode_image(data):
    configure_live_thread()
    if cv2 is None:return QImage.fromData(data)
    if data.startswith(b'PCZ1'):pixels=decode_lossless_rgb(data)
    else:
        flag=getattr(cv2,'IMREAD_COLOR_RGB',cv2.IMREAD_COLOR)
        pixels=cv2.imdecode(np.frombuffer(data,np.uint8),flag)
        if pixels is None:return QImage()
        if not hasattr(cv2,'IMREAD_COLOR_RGB'):pixels=cv2.cvtColor(pixels,cv2.COLOR_BGR2RGB)
    if sys.byteorder=='little':
        # Qt owns this allocation, including across implicit QImage copies.
        # Native OpenCV fills it while releasing the GIL; SIP's Qt conversion
        # otherwise blocks both receiver threads and GUI Python callbacks.
        image=QImage(pixels.shape[1],pixels.shape[0],QImage.Format_RGB32)
        bits=image.bits();bits.setsize(image.byteCount())
        raster=np.frombuffer(bits,np.uint8).reshape(image.height(),image.bytesPerLine())[:,:image.width()*4].reshape(image.height(),image.width(),4)
        cv2.cvtColor(pixels,cv2.COLOR_RGB2BGRA,dst=raster)
    else:
        image=QImage(pixels.data,pixels.shape[1],pixels.shape[0],pixels.strides[0],QImage.Format_RGB888).convertToFormat(QImage.Format_RGB32)
    # Preserve the original RGB tracking array, independent of Qt's owned raster.
    image._preview_pixels=pixels
    return image
