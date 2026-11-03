"""One model context, RK3576 dual-core request with sequential safe fallback."""
from pathlib import Path
import os

def open_runtime(model,factory,compatible=None):
    if compatible is None:
        try:compatible=Path('/proc/device-tree/compatible').read_bytes()
        except OSError:compatible=b''
    dual=('rockchip,rk3576'.encode() in compatible.split(b'\0')
          and os.environ.get('PCB_NPU_DUAL')!='0' and getattr(factory,'NPU_CORE_0_1',None)==3)
    requested=3 if dual else 0;fallback=''
    for mask in ((3,0) if dual else (0,)):
        net=factory();ready=False
        try:
            if net.load_rknn(model):raise RuntimeError('RKNN model load failed')
            try:
                rc=net.init_runtime(core_mask=mask) if mask else net.init_runtime()
                if rc:raise RuntimeError('RKNN runtime init returned '+str(rc))
            except Exception as error:
                if mask==3:fallback=(type(error).__name__+': '+str(error))[:240]
                else:raise
            else:
                ready=True
                return net,dict(requested_core_mask=requested,core_mask=mask,mode='cores 0+1' if mask==3 else 'auto',fallback_reason=fallback)
        finally:
            # A failed context must be fully released before AUTO is tried.
            # If release itself fails, propagate instead of opening another.
            if not ready:net.release()
    raise RuntimeError('RKNN runtime initialization failed')
