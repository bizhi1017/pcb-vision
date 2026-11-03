"""VID008 AB RAW10 DMA adapter; protocol from supplied display.py/raw10_convert.c.

Importing this file does not open devices. Hardware access occurs only in run().
"""
import os
import stat
import struct
import threading
import time
import ctypes as C
from functools import lru_cache
from pathlib import Path
from collections import deque
import weakref
import errno
import numpy as np
from capture_profiles import profile as capture_profile,selection as capture_selection,DEFAULT as CAPTURE_DEFAULT,P8_BUILD,RETIRED_P8_BUILD,RETIRED_P8R1_BUILD,P8_CAPABILITY,PROFILE_REGISTER,SENSOR_MASK_REGISTER,CAPABILITY_REGISTER
from capture_control import StopEvent

STRIDE=2432
ROWS=1097
FRAME_BYTES=STRIDE*ROWS
CAPACITY=FRAME_BYTES+16
BUILD=0x56303868
LOW_GAIN_BUILD=0x56303930
PRE_BUILD=0x56305036
from fpga_codec import BUILD as CODEC_BUILD, MAX_PACKET, MODES as CODEC_MODES, decode as decode_codec, native_available as codec_native_available


def source_address(data):
    # The caller retains data throughout each synchronous C operation (and
    # joins all HQ bands). np.frombuffer also supports read-only leased views.
    return data if isinstance(data,bytes) else np.frombuffer(data,np.uint8,count=1).ctypes.data

def processed_frame(data,camera,profile=None):
    geometry=capture_profile(profile or CAPTURE_DEFAULT);size=geometry.gray_bytes
    if len(data)!=size+16:raise ValueError('processed frame length mismatch')
    magic,length,tag,inverse=struct.unpack_from('<4I',data,size)
    mode=(tag>>8)&7
    if (magic!=0x54373056 or length!=size or inverse!=(size^0xffffffff) or tag>>24!=1
        or (tag>>16)&255!=camera or tag&0xc000 or (tag>>11)&7!=4 or not 1<=mode<=4):
        raise ValueError('invalid processed frame trailer')
    return np.frombuffer(data,np.uint8,count=size).reshape(geometry.height,geometry.width),tag&255,mode
EXPECTED={0x44:1944,0x48:1097,0x4c:0xA2B,0x50:2,0x54:60,0x58:2430,
          0x5c:BUILD,0x64:FRAME_BYTES,0x70:0x2D59500E,0x74:0x20000,0x78:STRIDE}


def validate_packet(data,camera,profile=None):
    geometry=capture_profile(profile or CAPTURE_DEFAULT);frame_bytes=geometry.raw_bytes
    if camera not in (0,1) or len(data)!=frame_bytes+16: raise ValueError('RAW10 frame length/camera mismatch')
    magic,size,tag,inverse=struct.unpack_from('<4I',data,frame_bytes)
    if (magic!=0x54373056 or size!=frame_bytes or inverse!=(size^0xffffffff)
            or tag>>24!=1 or ((tag>>16)&255)!=camera or tag&0xc000 or ((tag>>11)&7)!=4):
        raise ValueError('RAW10 trailer invalid or wrong camera')
    return tag&2047


class Raw10Converter:
    def __init__(self,awb=True,gain=1.,black=0,backend='auto',gamma=1.):
        self.awb=awb;self.red_gain=1.;self.blue_gain=1.;self.frames=0;self.force_numpy=backend=='numpy'
        self.gain=gain;self.black=black;self.backend='numpy';self.native=None
        self.gamma=gamma;self._gamma_key=None;self._gamma_lut=None;self.awb_backend='not sampled'
        library=Path(__file__).with_name('07_software')/'app/libcamera_preview.so'
        if backend!='numpy' and library.is_file():
            class State(C.Structure):
                _fields_=[('red_gain',C.c_float),('blue_gain',C.c_float),('frames',C.c_uint)]
            try:
                self.library=C.CDLL(str(library));self.native=self.library.raw10_convert
                self.native.argtypes=[C.POINTER(State),C.c_void_p,C.c_void_p,C.c_size_t,
                                      C.c_uint,C.c_uint,C.c_float,C.c_uint,C.c_uint,C.c_uint]
                self.native.restype=C.c_int
                self.native_state=State();self.native_pixels=np.empty((540,960,4),np.uint8)
                self.backend='native-c'
            except (OSError,AttributeError):
                if backend=='native':raise
        if backend=='native' and self.native is None:raise RuntimeError('native RAW10 converter unavailable')

    def finish(self,image):
        if self.gamma==1:return image
        if self._gamma_key!=self.gamma:
            self._gamma_lut=np.rint((np.arange(256,dtype=np.float64)/255)**(1/self.gamma)*255).astype(np.uint8)
            self._gamma_key=self.gamma
        import cv2
        return cv2.LUT(image,self._gamma_lut)

    def update_awb(self,data,camera,profile=None):
        """Same phase-2 upper-eight-bit sampling as the native preview, no RGB render."""
        geometry=capture_profile(profile or CAPTURE_DEFAULT);validate_packet(data,camera,geometry)
        state=self.native_state if self.native else self
        frames=state.frames;rg=state.red_gain if frames else 1.;bg=state.blue_gain if frames else 1.
        self.awb_backend='off'
        if self.awb:
            lib=hq_native() if not self.force_numpy else False
            if lib and hasattr(lib,'hq_awb_sums') and not geometry.mode:
                self.awb_backend='native-statistics'
                sums=(C.c_uint64*4)()
                if lib.hq_awb_sums(source_address(data),len(data),sums):raise ValueError('native AWB statistics failed')
                count,rs,sg,bs=map(int,sums)
            else:
                self.awb_backend='numpy-statistics'
                def sample(offset):
                    return np.ndarray((geometry.height//8,geometry.width//8),np.uint8,buffer=data,offset=geometry.crop_y*geometry.stride+(geometry.crop_x//4)*5+offset,strides=(8*geometry.stride,10))
                a=sample(0);blue=sample(1);red=sample(geometry.stride);d=sample(geometry.stride+1)
                green=(a.astype(np.uint16)+d+1)>>1
                good=(red>16)&(red<235)&(blue>16)&(blue<235)&(green>16)&(green<235)
                count=np.count_nonzero(good)
                rs=red[good].sum(dtype=np.float64);sg=green[good].sum(dtype=np.float64);bs=blue[good].sum(dtype=np.float64)
            if count>100:
                nr=float(min(4.,max(.25,float(sg)/float(rs))))
                nb=float(min(4.,max(.25,float(sg)/float(bs))))
                if self.native:
                    nr=np.float32(nr);nb=np.float32(nb)
                    rg=float(np.float32(.9)*np.float32(rg)+np.float32(.1)*nr) if frames else float(nr)
                    bg=float(np.float32(.9)*np.float32(bg)+np.float32(.1)*nb) if frames else float(nb)
                else:rg=.9*rg+.1*nr if frames else nr;bg=.9*bg+.1*nb if frames else nb
        state.red_gain=rg;state.blue_gain=bg;state.frames=frames+1

    def convert(self,data,camera,profile=None):
        geometry=capture_profile(profile or CAPTURE_DEFAULT);sequence=validate_packet(data,camera,geometry)
        if geometry.mode:
            import cv2
            self.update_awb(data,camera,geometry)
            state=self.native_state if self.native else self
            image,_=full_resolution(data,camera,gain=self.gain,black=self.black,gamma=self.gamma,red_gain=float(state.red_gain),blue_gain=float(state.blue_gain),profile=geometry)
            return cv2.resize(image,(960,540),interpolation=cv2.INTER_AREA),sequence
        if self.native is not None:
            code=self.native(C.byref(self.native_state),self.native_pixels.ctypes.data,source_address(data),len(data),
                             camera,2,float(self.gain),self.black,int(self.awb),0)
            if code:raise ValueError('native RAW10 conversion failed: %d'%code)
            return self.finish(self.native_pixels[:,:,:3].copy()),sequence
        # Four upper 8-bit samples followed by four packed 2-bit LSBs.
        # Match reference preview: upper eight bits, crop x=4,y=16,1920x1080.
        # Each RAW10 group contains two Bayer cells. Strided views avoid
        # repacking all 1944x1097 pixels before taking only the preview crop.
        def samples(offset):
            return np.ndarray((540,480,2),np.uint8,buffer=data,
                              offset=16*STRIDE+5+offset,strides=(2*STRIDE,5,2))
        a=samples(0);blue=samples(1);red=samples(STRIDE);d=samples(STRIDE+1)
        green=a.astype(np.uint16);green+=d;green+=1;green>>=1
        if self.awb:
            rr=red[::4,::2,0];gg=green[::4,::2,0];bb=blue[::4,::2,0]
            good=(rr>16)&(rr<235)&(gg>16)&(gg<235)&(bb>16)&(bb<235)
            if good.sum()>100:
                rg=float(np.clip(gg[good].mean()/rr[good].mean(),.25,4))
                bg=float(np.clip(gg[good].mean()/bb[good].mean(),.25,4))
                self.red_gain=.9*self.red_gain+.1*rg if self.frames else rg
                self.blue_gain=.9*self.blue_gain+.1*bg if self.frames else bg
        out=np.empty((540,480,2,3),np.uint8)
        # Apply gain via 256-entry tables, not two full float64 frame arrays.
        values=np.arange(256,dtype=np.float64)
        values=np.maximum(values-self.black,0)
        blut=np.clip(values*self.gain*self.blue_gain,0,255).astype(np.uint8)
        rlut=np.clip(values*self.gain*self.red_gain,0,255).astype(np.uint8)
        glut=np.clip(values*self.gain,0,255).astype(np.uint8)
        out[:,:,:,0]=blut[blue];out[:,:,:,1]=glut[green];out[:,:,:,2]=rlut[red]
        self.frames+=1
        return self.finish(out.reshape(540,960,3)),sequence


_hq_native=None
_hq_pool=None
_hq_pool_lock=threading.Lock()


def hq_pool():
    global _hq_pool
    with _hq_pool_lock:
        if _hq_pool is None:
            from concurrent.futures import ThreadPoolExecutor
            def initialize():
                if hasattr(os,'sched_getaffinity'):
                    allowed=os.sched_getaffinity(0);preferred={cpu for cpu in allowed if cpu>=4}
                    # Confirmed RK3576 layout, consistent with capture CPU4/6.
                    if len(preferred)>=2:os.sched_setaffinity(0,preferred)
            _hq_pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix='hq-band',initializer=initialize)
        return _hq_pool

def hq_native():
    global _hq_native
    if _hq_native is None:
        try:
            lib=C.CDLL(str(Path(__file__).with_name('libraw10_hq.so')))
            lib.hq_unpack.argtypes=[C.c_void_p,C.c_size_t,C.c_void_p];lib.hq_unpack.restype=C.c_int
            lib.hq_tone.argtypes=[C.c_void_p,C.c_void_p,C.c_void_p,C.c_size_t];lib.hq_tone.restype=C.c_int
            try:
                lib.hq_fused_ea.argtypes=[C.c_void_p,C.c_size_t,C.c_void_p,C.c_void_p,C.c_int,C.c_int]
                lib.hq_fused_ea.restype=C.c_int
            except AttributeError:pass  # Previous native library remains compatible.
            try:
                lib.hq_fused_ea_parallel.argtypes=[C.c_void_p,C.c_size_t,C.c_void_p,C.c_void_p]
                lib.hq_fused_ea_parallel.restype=C.c_int
            except AttributeError:pass
            try:
                lib.hq_fused_ea_profile.argtypes=[C.c_void_p,C.c_size_t,C.c_void_p,C.c_void_p]+[C.c_int]*5
                lib.hq_fused_ea_profile.restype=C.c_int
            except AttributeError:pass
            try:
                lib.hq_awb_sums.argtypes=[C.c_void_p,C.c_size_t,C.POINTER(C.c_uint64)];lib.hq_awb_sums.restype=C.c_int
            except AttributeError:pass
            _hq_native=lib
        except (OSError,AttributeError):_hq_native=False
    return _hq_native

@lru_cache(maxsize=16)
def tone_tables(gain,black,red_gain,blue_gain,gamma):
    values=np.maximum(np.arange(1024,dtype=np.float64)-black*4,0)*gain/4
    tables=np.ascontiguousarray([np.rint((np.clip(values*g,0,255)/255)**(1/gamma)*255).astype(np.uint8) if gamma!=1 else np.clip(values*g,0,255).astype(np.uint8) for g in (blue_gain,1.,red_gain)])
    tables.flags.writeable=False
    return tables


class ImageBufferPool:
    """Cache at most four RGB buffers; reuse only after the last view dies."""
    def __init__(self,shape=(1080,1920,3),limit=4,shared=False):
        self.shape=shape;self.limit=limit;self.free=[];self.allocated=0;self.reused=0;self.fallback=0;self.lock=threading.Lock();self.shared=shared;self.shared_allocated=0

    def release(self,storage):
        with self.lock:self.free.append(storage)

    def acquire(self):
        with self.lock:
            if self.free:storage=self.free.pop();self.reused+=1
            elif self.allocated<self.limit:
                from shared_image import allocate_storage,SharedStorage
                storage=allocate_storage(int(np.prod(self.shape)),self.shared);self.allocated+=1
                self.shared_allocated+=isinstance(storage,SharedStorage)
            else:
                self.fallback+=1
                return np.empty(self.shape,np.uint8)
        # A bytearray base keeps ndarray slices/reshape views tied to this
        # root array, so an encoder/detector holding a view prevents reuse.
        image=np.ndarray(self.shape,np.uint8,buffer=storage)
        weakref.finalize(image,self.release,storage)
        return image

    def stats(self):
        with self.lock:return dict(allocated=self.allocated,cached=len(self.free),reused=self.reused,fallback=self.fallback,shared_allocated=self.shared_allocated)


def full_resolution(data,camera,gain=.8,black=16,red_gain=1.,blue_gain=1.,gamma=1.,backend='auto',output=None,profile=None):
    """Full RAW10 unpack + edge-aware Bayer demosaic; no 2x2 pixel decimation."""
    geometry=capture_profile(profile or CAPTURE_DEFAULT);sequence=validate_packet(data,camera,geometry)
    native=hq_native() if backend!='numpy' else False
    if backend=='native' and not native:raise RuntimeError('native HQ library unavailable')
    fused=bool(native and hasattr(native,'hq_fused_ea'))
    if geometry.mode:
        import cv2
        if output is None:output=np.empty(geometry.shape,np.uint8)
        if output.shape!=geometry.shape or output.dtype!=np.uint8 or not output.flags.c_contiguous or not output.flags.writeable:raise ValueError('invalid HQ output buffer')
        luts=tone_tables(gain,black,red_gain,blue_gain,gamma)
        if native:
            if not hasattr(native,'hq_fused_ea_profile'):raise ValueError('4K requires the updated native HQ library')
            if native.hq_fused_ea_profile(source_address(data),len(data),luts.ctypes.data,output.ctypes.data,geometry.width,geometry.height,geometry.stride,geometry.crop_x,geometry.crop_y):raise ValueError('4K HQ conversion failed')
        else:
            rows=np.frombuffer(data,np.uint8,count=geometry.raw_bytes).reshape(geometry.raw_height,geometry.stride)
            groups=rows[geometry.crop_y:geometry.crop_y+geometry.height,:geometry.line_bytes].reshape(geometry.height,geometry.raw_width//4,5)[:,geometry.crop_x//4:(geometry.crop_x+geometry.width)//4]
            raw=groups[:,:,:4].astype(np.uint16)<<2
            for i in range(4):raw[:,:,i]|=(groups[:,:,4]>>(2*i))&3
            bgr=cv2.cvtColor(raw.reshape(geometry.height,geometry.width),cv2.COLOR_BayerGR2BGR_EA)
            for i in range(3):output[:,:,i]=luts[i][bgr[:,:,i]]
        return output,sequence
    if native and not fused:
        raw=np.empty((1080,1920),np.uint16)
        if native.hq_unpack(source_address(data),len(data),raw.ctypes.data):raise ValueError('HQ RAW unpack failed')
    elif not fused:
        rows=np.frombuffer(data,np.uint8,count=FRAME_BYTES).reshape(ROWS,STRIDE)
        groups=rows[16:1096,:2430].reshape(1080,486,5)[:,1:481]
        raw=(groups[:,:,:4].astype(np.uint16)<<2)
        for i in range(4):raw[:,:,i]|=(groups[:,:,4]>>(2*i))&3
    import cv2
    # OpenCV's short Bayer code uses its opposite-corner naming convention.
    # GBRG input is verified by the synthetic R/B placement test.
    if not fused:raw=raw.reshape(1080,1920)
    luts=tone_tables(gain,black,red_gain,blue_gain,gamma)
    if output is None:output=np.empty((1080,1920,3),np.uint8)
    if output.shape!=(1080,1920,3) or output.dtype!=np.uint8 or not output.flags.c_contiguous or not output.flags.writeable:raise ValueError('invalid HQ output buffer')
    if fused and hasattr(native,'hq_fused_ea_parallel'):
        # The native call joins its own two writes before returning. Retain
        # source/LUT/output leases for the entire synchronous operation.
        if native.hq_fused_ea_parallel(source_address(data),len(data),luts.ctypes.data,output.ctypes.data):raise ValueError('parallel fused HQ conversion failed')
        return output,sequence
    if native:
        tables=np.ascontiguousarray(luts,dtype=np.uint8)
        # Two bands share immutable input/LUT and write disjoint output rows.
        # Fused C uses three real RAW rows. Legacy OpenCV uses eight overlap
        # rows to isolate its artificial band edges; both preserve full EA.
        def band(y0,y1):
            if fused:
                if native.hq_fused_ea(source_address(data),len(data),tables.ctypes.data,output.ctypes.data,y0,y1):raise ValueError('fused HQ conversion failed')
                return
            start=max(0,y0-8);end=min(1080,y1+8)
            bgr=cv2.cvtColor(raw[start:end],cv2.COLOR_BayerGR2BGR_EA)
            bgr=bgr[y0-start:y1-start]
            if native.hq_tone(bgr.ctypes.data,tables.ctypes.data,output[y0:y1].ctypes.data,1920*(y1-y0)):raise ValueError('HQ tone failed')
        futures=[hq_pool().submit(band,540,1080)] if fused else [hq_pool().submit(band,0,540),hq_pool().submit(band,540,1080)]
        # Always join both writes, including the error path.
        errors=[]
        if fused:
            try:band(0,540)
            except Exception as exc:errors.append(exc)
        for future in futures:
            try:future.result()
            except Exception as exc:errors.append(exc)
        if errors:raise errors[0]
    else:
        bgr=cv2.cvtColor(raw,cv2.COLOR_BayerGR2BGR_EA)
        for i in range(3):output[:,:,i]=luts[i][bgr[:,:,i]]
    return output,sequence


def validate_capture_identity(build,control):
    if build in (0,0xffffffff) or control==0xffffffff:
        raise RuntimeError('PCIe寄存器不可读（BUILD=0x%08X，CTRL=0x%08X）；请退出服务，重新枚举相机PCIe并加载驱动后再启动'%(build,control))
    if build==RETIRED_P8_BUILD:
        raise RuntimeError('旧P8位流存在PCIe复位连接问题，请烧写P8R2修正版（BUILD 0x%08X）后再启动'%P8_BUILD)
    if build==RETIRED_P8R1_BUILD:
        raise RuntimeError('旧P8R1位流存在4K数据标记问题，请烧写P8R2修正版（BUILD 0x%08X）后再启动'%P8_BUILD)
    if build not in (BUILD,LOW_GAIN_BUILD,PRE_BUILD,CODEC_BUILD,P8_BUILD):
        raise ValueError('未知FPGA BUILD：0x%08X'%build)
    if control & ~3:raise ValueError('无效DMA控制寄存器：0x%08X'%control)
    if control:raise RuntimeError('Another DMA stream is already active')


def inspect(read_reg,profile=None,channels=None,build=None):
    validate_capture_identity(read_reg(0x5c),read_reg(0x60))
    geometry=capture_profile(profile or CAPTURE_DEFAULT)
    expected=dict(EXPECTED)
    if build==P8_BUILD:expected.update({0x44:geometry.raw_width,0x48:geometry.raw_height,0x54:geometry.fps,0x58:geometry.line_bytes,0x64:geometry.raw_bytes,0x70:geometry.fingerprint,0x74:0x30000,0x78:geometry.stride})
    for offset,value in expected.items():
        actual=read_reg(offset)
        if offset==0x5c and actual in (BUILD,LOW_GAIN_BUILD,PRE_BUILD,CODEC_BUILD,P8_BUILD):continue
        if actual!=value: raise ValueError('FPGA register 0x%x: 0x%x != 0x%x'%(offset,actual,value))
    if read_reg(0x60): raise RuntimeError('Another DMA stream is active; stop it first')
    for camera in [('A','B').index(c) for c in channels] if channels is not None else (0,1):
        link=read_reg(0x100+camera*32)
        if not (link>>28==11 and not link&0x0f800000 and (link>>21)&3==3):
            raise RuntimeError('Camera %d is not ready'%camera)


def decode_preview_packet(data,camera,converter,pre_enabled=False,requested_mode=0):
    """Validate before skipping obsolete transition packets; never decode stale RAW."""
    if pre_enabled and len(data)!=CAPACITY:
        gray,seq,mode=processed_frame(data,camera)
        if mode!=requested_mode:return None,seq,mode
        import cv2
        return cv2.cvtColor(cv2.resize(gray,(960,540)),cv2.COLOR_GRAY2BGR),seq,mode
    seq=validate_packet(data,camera)
    if pre_enabled and seq>255:raise ValueError('RAW frame has nonzero preprocess tag')
    if pre_enabled and requested_mode!=0:return None,seq,0
    frame,seq=converter.convert(data,camera)
    return frame,seq,0


class DeferredPreview:
    """One latest slot: CPU preview never delays DMA credit return."""
    def __init__(self,hub,channel,converter,allowed=None,profile=None,capture_generation=None,stop=None):
        self.hub=hub;self.channel=channel;self.converter=converter;self.allowed=allowed
        self.profile=capture_profile(profile or CAPTURE_DEFAULT);self.capture_generation=capture_generation;self.stop=stop or hub.stop
        self.lock=threading.Lock();self.event=threading.Event();self.pending=None;self.skipped=0
        self.closed=threading.Event();self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def close(self):self.closed.set();self.event.set();self.thread.join(1)

    def submit(self,data,camera,mode,sequence,at,config):
        with self.lock:
            if self.pending is not None:self.skipped+=1
            self.pending=(data,camera,mode,sequence,at,dict(config));self.event.set()

    def parameters(self,config):
        c=self.converter
        return dict(gain=config['gain'],black=config['black'],gamma=config.get('gamma',2.2),
                    red_gain=float(c.native_state.red_gain) if c.native else c.red_gain,
                    blue_gain=float(c.native_state.blue_gain) if c.native else c.blue_gain,**({'profile':self.profile.id} if self.profile.mode else {}))

    def run(self):
        if self.allowed and hasattr(os,'sched_setaffinity'):os.sched_setaffinity(0,self.allowed)
        while not self.stop.is_set() and not self.closed.is_set():
            if not self.event.wait(.05):continue
            with self.lock:job=self.pending;self.pending=None;self.event.clear()
            if job is None:continue
            data,camera,mode,seq,at,cfg=job;started=time.monotonic()
            try:
                self.converter.gain=cfg['gain'];self.converter.black=cfg['black'];self.converter.gamma=cfg.get('gamma',2.2)
                with self.hub.lock:
                    cached=self.hub.hq_cache.get(self.channel)
                    shared=bool(cached and cached[3]==mode and time.monotonic()-cached[2]<.2
                                and time.monotonic()-self.hub.hq_requests.get(self.channel,0)<.4)
                if shared:
                    if not mode:self.converter.update_awb(data,camera,self.profile) if self.profile.mode else self.converter.update_awb(data,camera)
                    with self.hub.lock:
                        self.hub.channels[self.channel].update(preview_backend='shared-hq',awb_backend=self.converter.awb_backend if not mode else 'inactive-gray')
                    self.stop.wait(max(0,1/30-(time.monotonic()-started)))
                    continue
                if mode:
                    import cv2
                    gray,_,_=processed_frame(data,camera);frame=cv2.cvtColor(cv2.resize(gray,(960,540)),cv2.COLOR_GRAY2BGR)
                else:frame,_=self.converter.convert(data,camera,self.profile) if self.profile.mode else self.converter.convert(data,camera)
                self.hub.publish_preview(self.channel,frame,seq,at,mode,**({'capture_generation':self.capture_generation} if self.capture_generation is not None else {}))
                ms=(time.monotonic()-started)*1000
                with self.hub.lock:
                    self.hub.channels[self.channel].update(decode_ms=round(ms,2),decode_max_ms=max(round(ms,2),self.hub.channels[self.channel].get('decode_max_ms',0)),preview_skipped=self.skipped,
                        decode_backend='fpga-gray8' if mode else self.converter.backend)
            except Exception as exc:
                with self.hub.lock:self.hub.channels[self.channel]['error']=str(exc)
            self.stop.wait(max(0,1/30-(time.monotonic()-started)))


_libc_read=None
_libc_read_lock=threading.Lock()


def read_into_existing(fd,storage,capacity,pool):
    global _libc_read
    if memoryview(storage).nbytes<capacity:raise ValueError('DMA buffer is smaller than requested read')
    if _libc_read is None:
        with _libc_read_lock:
            if _libc_read is None:
                try:
                    lib=C.CDLL(None,use_errno=True);fn=lib.read
                    fn.argtypes=[C.c_int,C.c_void_p,C.c_size_t];fn.restype=C.c_ssize_t
                    _libc_read=fn
                except (OSError,AttributeError,TypeError):_libc_read=False
    if not _libc_read:
        pool.read_backend='read';return os.read(fd,capacity)
    keepalive=C.c_char.from_buffer(storage)
    while True:
        count=_libc_read(fd,C.addressof(keepalive),capacity)
        if count>=0:break
        code=C.get_errno()
        if code==errno.EINTR:continue
        raise OSError(code,os.strerror(code))
    pool.read_backend='libc-read'
    return storage[:count]


def read_pooled_packet(fd,capacity,pool):
    storage=pool.acquire()
    if not hasattr(os,'readv') or getattr(pool,'readv_disabled',False):
        return read_into_existing(fd,storage,capacity,pool)
    try:size=os.readv(fd,[storage])
    except OSError as exc:
        if exc.errno not in (errno.ENOSYS,errno.EINVAL,errno.EOPNOTSUPP):raise
        pool.readv_disabled=True
        return read_into_existing(fd,storage,capacity,pool)
    pool.read_backend='readv'
    return storage[:size]


class PacketPump:
    """Return DMA credits before CPU decoding; two complete latest packets only."""
    def __init__(self,read,capacity,stop,on_close=None,affinity=None):
        self.read=read;self.capacity=capacity;self.stop=stop;self.on_close=on_close;self.affinity=affinity
        self.closed=threading.Event();self.ready=threading.Event();self.lock=threading.Lock()
        self.pending=deque(maxlen=2);self.dropped=0;self.error=None
        self.thread=threading.Thread(target=self.run,daemon=True)

    def start(self):self.thread.start()

    def run(self):
        try:
            if self.affinity and hasattr(os,'sched_setaffinity'):os.sched_setaffinity(0,self.affinity)
            while not self.stop.is_set() and not self.closed.is_set():
                before=time.monotonic();data=self.read(self.capacity);at=time.monotonic()
                if len(data)==0:raise OSError('DMA returned an empty packet')
                with self.lock:
                    if len(self.pending)==2:self.dropped+=1
                    self.pending.append((data,at-before,at));self.ready.set()
        except Exception as exc:
            with self.lock:self.error=exc;self.ready.set()
        finally:
            if self.on_close:self.on_close()

    def get(self):
        self.ready.wait(.05)
        with self.lock:
            if self.pending:
                item=self.pending.popleft()
                if not self.pending:self.ready.clear()
                return item
            if self.error:raise self.error
            return None

    def close(self):
        # The enclosing session has a bounded shutdown deadline and must stay
        # alive if a kernel read cannot exit. Never orphan a DMA owner thread.
        self.closed.set();self.ready.set();self.thread.join()


class PcieCapture:
    def __init__(self,hub,target=None):
        self.hub=hub
        self.target=capture_selection(target or dict(profile='1080p60',channels=['A','B']))
        self.profile=capture_profile(self.target['profile']);self.generation=getattr(hub,'capture_generation',0)
        self.stopping=threading.Event();self.stop=StopEvent(hub.stop if hub is not None else threading.Event(),self.stopping);self.ready=threading.Event();self.complete={c:0 for c in self.target['channels']}
        self.error='';self.switchable=False;self.supported=['1080p60'];self.reason='4K需要P8位流和新DMA驱动';self.read_fds=set();self.build=0;self.driver_limit=0
        self.fd=None;self.pre_enabled=False;self.codec_enabled=False;self.control_lock=threading.Lock();self.diagnostics={}

    def diagnostic_snapshot(self,stage,descriptor=None):
        """Read the existing control descriptor before stop/rollback clears counters."""
        with self.control_lock:
            fd=self.fd if descriptor is None else descriptor
            if fd is None:return self.diagnostics
            try:
                offsets=list(range(0x20,0x80,4))+list(range(0x80,0xc0,4))+list(range(0x100,0x180,4))
                regs={offset:struct.unpack('<I',os.pread(fd,4,offset))[0] for offset in offsets}
                cameras={}
                for i,c in enumerate(('A','B')):
                    base=0x140+i*32;observed=regs[base];frame=regs[base+4];pack=regs[base+8]
                    cameras[c]=dict(observed_dt=(observed>>16)&63,observed_wc=observed&65535,
                        frame_valid=bool(frame>>31),frame_error=bool(frame&(1<<30)),
                        frame_seen_lines=(frame>>16)&4095,frame_good_lines=frame&65535,
                        pack_last_lines=pack&4095,pack_max_fifo=(pack>>16)&8191,
                        signature='0x%08x'%regs[base+12])
                self.diagnostics=dict(stage=stage,at=time.monotonic(),complete_frames=dict(self.complete),
                    registers={'0x%03x'%k:'0x%08x'%v for k,v in regs.items()},cameras=cameras)
            except (OSError,struct.error) as error:
                self.diagnostics=dict(stage=stage,read_error=str(error),complete_frames=dict(self.complete))
            return self.diagnostics

    def request_stop(self):
        self.stopping.set()
        with self.control_lock:
            if os.name=='posix':
                import fcntl
                for descriptor in self.read_fds:
                    try:fcntl.ioctl(descriptor,0x7143)
                    except OSError:pass

    def _close_read_fd(self,descriptor):
        # Cancellation and close share the lock so a recycled descriptor can
        # never receive the old session's cancellation ioctl.
        with self.control_lock:
            self.read_fds.discard(descriptor)
            os.close(descriptor)

    def apply_codec(self,modes):
        with self.control_lock:
            if self.fd is None or not self.codec_enabled:raise ValueError('P7 FPGA codec bitstream not active')
            previous=struct.unpack('<I',os.pread(self.fd,4,0x38))[0]
            value=previous
            for i,c in enumerate(('A','B')):
                old=(previous>>(16*i))&65535
                code=CODEC_MODES.index(modes[c])
                if old&3!=code:
                    new=((((old>>2)+1)&16383)<<2)|code
                    value=(value&~(65535<<(16*i)))|(new<<(16*i))
            try:
                if os.pwrite(self.fd,struct.pack('<I',value),0x38)!=4:raise OSError('codec configuration write failed')
                if struct.unpack('<I',os.pread(self.fd,4,0x38))[0]!=value:raise OSError('codec configuration readback mismatch')
            except Exception:
                os.pwrite(self.fd,struct.pack('<I',previous),0x38)
                raise
            return value

    def apply(self,modes,thresholds):
        with self.control_lock:
            if self.fd is None or not self.pre_enabled:raise ValueError('FPGA preprocess bitstream not active')
            packed=modes['A']|(modes['B']<<3);threshold=thresholds['A']|(thresholds['B']<<11)
            previous={offset:os.pread(self.fd,4,offset) for offset in (0x34,0x30)}
            try:
                for offset,value in [(0x34,threshold),(0x30,packed)]:
                    if os.pwrite(self.fd,struct.pack('<I',value),offset)!=4:raise OSError('FPGA mode write failed')
                    if struct.unpack('<I',os.pread(self.fd,4,offset))[0]!=value:raise OSError('FPGA mode readback mismatch')
            except Exception:
                for offset,value in previous.items():os.pwrite(self.fd,value,offset)
                raise

    def run(self):
        # Linux modules intentionally imported here so decoder tests work on Windows.
        import fcntl
        fd=None;enabled=False;threads=[]
        barrier=threading.Barrier(len(self.target['channels'])+1,timeout=12)
        def require_char(path):
            if not stat.S_ISCHR(os.stat(path).st_mode):raise ValueError('Expected character device: '+path)
        def worker(camera):
            channel=('A','B')[camera];cfd=None;pump=None;input_buffers=None;decoded_buffers=None;converter=Raw10Converter()
            preview=None;allowed=os.sched_getaffinity(0)
            last_source=None;last_mode=None;source_gaps=0;source_discontinuities=0;decode_max=0.
            transition_discarded=0;invalid_packets=0
            try:
                # Keep CRC/decompression beside each DMA reader. Leave the big
                # cores to the full-resolution demosaic and network encoder.
                from cpu_layout import choose_cpus
                preferred=choose_cpus('capture',allowed,camera=camera)
                if preferred and preferred!=allowed:os.sched_setaffinity(0,preferred)
                with self.hub.lock:self.hub.channels[channel]['dma_decode_cpu_affinity']=sorted(os.sched_getaffinity(0))
                preview=DeferredPreview(self.hub,channel,converter,allowed,self.profile,self.generation,self.stop)
                node='/dev/ANLOGIC-PCI0_c2h_%d'%camera;require_char(node)
                cfd=os.open(node,os.O_RDONLY|os.O_TRUNC)
                capacity=self.profile.packet_capacity if self.codec_enabled else self.profile.raw_bytes+16
                fcntl.ioctl(cfd,0x40107142,struct.pack('<4I',4 if self.profile.mode else 8,capacity,0,0))
                with self.control_lock:self.read_fds.add(cfd)
                barrier.wait()
                if self.codec_enabled:
                    little=(0,2)[camera];affinity={little} if little in allowed else None
                    input_buffers=ImageBufferPool((capacity,),limit=4)
                    decoded_buffers=ImageBufferPool((self.profile.raw_bytes+16,),limit=4)
                    pump=PacketPump(lambda size,fd=cfd:read_pooled_packet(fd,size,input_buffers),capacity,self.stop,
                                    on_close=lambda fd=cfd:self._close_read_fd(fd),affinity=affinity)
                    pump.start();cfd=None
                while not self.stop.is_set():
                    with self.hub.lock:
                        image_config=self.hub.config.get('image',{}).get(channel,{'gain':.8,'black':16})
                    before=time.monotonic()
                    if pump:
                        item=pump.get()
                        if item is None:continue
                        data,wait_seconds,received=item
                    else:
                        data=os.read(cfd,capacity);received=time.monotonic();wait_seconds=received-before
                    if self.stop.is_set():break
                    with self.hub.lock:requested=self.hub.config.get('pre',{}).get(channel,0)
                    mode=0
                    try:
                        if self.codec_enabled:
                            codec_start=time.monotonic();data,codec_meta=decode_codec(data,camera,output=decoded_buffers.acquire(),profile=self.profile)
                            codec_meta['decode_ms']=round((time.monotonic()-codec_start)*1000,3)
                            codec_meta['saving_pct']=round(100*(1-codec_meta['wire_bytes']/codec_meta['raw_bytes']),2)
                            with self.hub.lock:
                                self.hub.channels[channel]['fpga_codec']=codec_meta
                                expected_codec=self.hub.config['fpga_codec'][channel]
                            if codec_meta['mode']!=expected_codec:
                                transition_discarded+=1;last_source=None;last_mode=None
                                continue
                        if self.pre_enabled and len(data)!=self.profile.raw_bytes+16:
                            _,seq,mode=processed_frame(data,camera,self.profile)
                        else:
                            seq=validate_packet(data,camera,self.profile);mode=0
                            if self.pre_enabled and seq>255:raise ValueError('RAW frame has nonzero preprocess tag')
                    except ValueError as exc:
                        invalid_packets+=1;self.complete[channel]=0
                        with self.hub.lock:self.hub.channels[channel].update(error=str(exc),invalid_packets=invalid_packets,invalid_last_bytes=len(data))
                        continue
                    with self.hub.lock:obsolete=self.pre_enabled and mode!=self.hub.config['pre'][channel]
                    if obsolete or mode!=requested:
                        transition_discarded+=1;last_source=None;last_mode=None
                        with self.hub.lock:self.hub.channels[channel]['transition_discarded']=transition_discarded
                        continue
                    if self.codec_enabled:seq=codec_meta['seq']
                    parameters=preview.parameters(image_config)
                    self.hub.publish(channel,None,raw_packet=data if mode==0 else None,
                                     gray_packet=data if mode else None,mode_applied=mode,raw_parameters=parameters,capture_generation=self.generation)
                    self.complete[channel]+=1
                    with self.hub.lock:published=self.hub.channels[channel]['seq'];at=self.hub.channels[channel]['at']
                    preview.submit(data,camera,mode,published,at,image_config)
                    decode_ms=(time.monotonic()-received)*1000
                    decode_max=max(decode_max,decode_ms)
                    if last_source is not None and last_mode==mode:
                        delta=(seq-last_source)&(0xffffffff if self.codec_enabled else 255 if self.pre_enabled else 2047)
                        half=0x80000000 if self.codec_enabled else 128 if self.pre_enabled else 1024
                        if 1<delta<half:source_gaps+=delta-1
                        elif delta==0 or delta>=half:source_discontinuities+=1
                    last_source=seq;last_mode=mode
                    with self.hub.lock:
                        self.hub.channels[channel].update(source_seq=seq,
                            decode_backend='fpga-gray8' if mode else converter.backend,
                            source_gaps=source_gaps,source_discontinuities=source_discontinuities,
                            dma_dispatch_max_ms=round(decode_max,2),
                            dma_wait_ms=round(wait_seconds*1000,2),
                            dma_queue_dropped=pump.dropped if pump else 0,
                            dma_read_buffer_pool=input_buffers.stats() if input_buffers else {},
                            dma_decode_buffer_pool=decoded_buffers.stats() if decoded_buffers else {},
                            dma_read_backend=getattr(input_buffers,'read_backend','read'),
                            dma_dispatch_ms=round(decode_ms,2))
            except Exception as exc:
                barrier.abort()
                if not self.stop.is_set():
                    self.diagnostic_snapshot('worker-error-'+channel)
                    self.error=str(exc);self.request_stop()
                    with self.hub.lock:self.hub.channels[channel]['error']=str(exc)
            finally:
                if pump is not None:pump.close()
                if cfd is not None:self._close_read_fd(cfd)
                if preview is not None:preview.close()
        try:
            if os.geteuid()!=0:raise PermissionError('PCIe capture requires root')
            path='/dev/ANLOGIC-PCI0_user';require_char(path);fd=os.open(path,os.O_RDWR)
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            read=lambda offset:struct.unpack('<I',os.pread(fd,4,offset))[0]
            build=read(0x5c);self.build=build
            validate_capture_identity(build,read(0x60))
            if build==P8_BUILD:
                if read(CAPABILITY_REGISTER)!=P8_CAPABILITY:raise ValueError('P8 capability signature mismatch')
                self.switchable=True
                try:driver_limit=int(Path('/sys/module/anlogic_pci/parameters/frame_max_bytes').read_text())
                except (OSError,ValueError):driver_limit=0
                self.driver_limit=driver_limit
                if driver_limit>=capture_profile('4k30').packet_capacity:self.supported.append('4k30');self.reason=''
                else:self.reason='4K需要加载支持大帧的新DMA驱动'
                if self.profile.id not in self.supported:raise ValueError(self.reason)
                os.pwrite(fd,struct.pack('<I',0),SENSOR_MASK_REGISTER)
                if self.stop.wait(.03):return
                os.pwrite(fd,struct.pack('<I',self.profile.mode),PROFILE_REGISTER)
                if read(PROFILE_REGISTER)!=self.profile.mode:raise ValueError('Capture mode readback mismatch')
                if self.stop.wait(.03):return
                mask=sum(1<<('A','B').index(c) for c in self.target['channels'])
                os.pwrite(fd,struct.pack('<I',mask),SENSOR_MASK_REGISTER)
                deadline=time.monotonic()+35
                while True:
                    try:inspect(read,self.profile,self.target['channels'],build);break
                    except (ValueError,RuntimeError):
                        if time.monotonic()>deadline:raise
                        if self.stop.wait(.05):return
            else:
                if self.target!=dict(profile='1080p60',channels=['A','B']):raise ValueError('Capture switching requires the P8 bitstream')
                inspect(read)
            self.codec_enabled=build in (CODEC_BUILD,P8_BUILD)
            self.pre_enabled=build in (PRE_BUILD,CODEC_BUILD,P8_BUILD)
            if self.codec_enabled:
                if not codec_native_available():raise RuntimeError('P7 requires libfpga_codec.so; run sh build_fpga_codec.sh before starting capture')
                value=struct.unpack('<I',os.pread(fd,4,0x38))[0]
                with self.hub.lock:self.hub.config['fpga_codec']={c:CODEC_MODES[(value>>(16*i))&3] for i,c in enumerate(('A','B'))}
            self.fd=fd
            if self.pre_enabled:
                self.apply(self.hub.config['pre'],self.hub.config['threshold']);self.hub.pre_control=self
            for camera in [('A','B').index(c) for c in self.target['channels']]:
                t=threading.Thread(target=worker,args=(camera,),daemon=True);threads.append(t);t.start()
            barrier.wait()
            if self.stop.is_set():return
            mask=sum(1<<('A','B').index(c) for c in self.target['channels'])
            if os.pwrite(fd,struct.pack('<I',mask),0x60)!=4:raise OSError('DMA enable failed')
            enabled=True;self.ready.set()
            while not self.stop.wait(.2):
                if all(not t.is_alive() for t in threads):break
        except Exception as exc:
            barrier.abort()
            if not self.stop.is_set():
                if fd is not None:self.diagnostic_snapshot('startup-error',fd)
                self.error=str(exc)
            with self.hub.lock:
                for channel in ('A','B'):self.hub.channels[channel]['error']=str(exc)
        finally:
            self.request_stop()
            # Keep the FPGA producer alive until each kernel DMA ring has
            # reached its packet boundary and closed. Disabling the producer
            # first can strand a partially received descriptor in BUSY, which
            # correctly quarantines its buffers and pins the driver module.
            for t in threads:t.join()
            # Only stop the stream that this instance enabled.
            if fd is not None:
                self.hub.pre_control=None
                with self.control_lock:self.fd=None
                try:
                    if enabled:os.pwrite(fd,struct.pack('<I',0),0x60)
                    if self.switchable:os.pwrite(fd,struct.pack('<I',0),SENSOR_MASK_REGISTER)
                finally:os.close(fd)
