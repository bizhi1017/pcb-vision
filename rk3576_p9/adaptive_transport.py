"""Shared software network encoder and closed-loop rate control; not FPGA coding."""
from dataclasses import dataclass
import threading,time,os
from collections import deque
import cv2
from mpp_jpeg import HardwareJPEG
from fast_png import FastPNG
from fast_lossless import FastLossless


DEFAULT=dict(mode='lossy',adaptive=True,mbps=300.,fps=60,max_width=1920)


def next_encode_deadline(previous,started,finished,fps):
    """Keep short scheduling jitter from slowing the configured cadence."""
    period=1/fps
    deadline=(previous if previous else started)+period
    # Recover a short overrun on the original clock. Larger stalls reset the
    # clock; the encoder still requests only the latest distinct source frame.
    return finished if finished-deadline>period else deadline


def settings(current,patch):
    if not isinstance(patch,dict) or set(patch)-set(DEFAULT):raise ValueError('invalid transport settings')
    new=dict(current,**patch)
    if new['mode'] not in ('lossy','lossless') or type(new['adaptive'])is not bool:raise ValueError('invalid compression mode')
    if type(new['mbps']) not in (int,float) or not .25<=new['mbps']<=800:raise ValueError('bandwidth 0.25..800 Mbps per camera')
    if type(new['fps'])is not int or not 1<=new['fps']<=60:raise ValueError('transport fps 1..60')
    if new['max_width'] not in (960,1920,3840) or type(new['max_width'])is not int:raise ValueError('transport width 960, 1920 or 3840')
    if new['mode']=='lossless' and new['max_width'] not in (1920,3840):raise ValueError('无损模式必须保留全分辨率')
    return new


class RateControl:
    def __init__(self,cfg):
        self.cfg=dict(cfg);self.quality=94 if cfg['mode']=='lossy' and cfg['max_width']>=1920 else 86;self.fps=float(cfg['fps']);self.estimate=None;self.ack_ms=0.

    def feedback(self,size,encode_ms,ack_ms=0,ceiling=None,ack_window=1):
        if type(ack_window)is not int or ack_window not in (1,2):raise ValueError('invalid ACK window')
        cfg=self.cfg;self.ack_ms=.8*self.ack_ms+.2*ack_ms if self.ack_ms else ack_ms
        self.estimate=size if self.estimate is None else .75*self.estimate+.25*size
        # Always enforce the byte budget by pacing; adaptive JPEG also changes Q.
        target=min(cfg['fps'],ceiling or cfg['fps']);allowed=cfg['mbps']*1e6/8/target
        if cfg['adaptive'] and cfg['mode']=='lossy':
            # Preserve full-HD detail first; a tight budget lowers fps instead.
            floor=92 if cfg['max_width']>=1920 else 45
            if size>allowed*1.12:self.quality=max(floor,self.quality-3)
            elif size<allowed*.9 and encode_ms<1000/cfg['fps']*.8:self.quality=min(94,self.quality+1)
            if ack_ms/ack_window>1000/cfg['fps']*1.6:self.quality=max(floor,self.quality-2)
        network=cfg['mbps']*1e6/(8*max(1,size,self.estimate))
        self.encode_capacity_fps=1000/max(1,encode_ms)
        # Keep the latest-only encoder ahead of the bounded ACK window's
        # capacity. Encoding is synchronous and already counts in the absolute
        # deadline: imposing a second period from one slow frame adds jitter.
        # A busy encoder still naturally produces fewer distinct frames.
        self.fps=max(.001,min(float(target),network,1100*ack_window/self.ack_ms if self.ack_ms else 60.))
        return self.fps


class AckWindow:
    """At most two transmitted frames; acknowledgements never create credit."""
    def __init__(self,limit=1):
        if limit not in (1,2):raise ValueError('ACK window must be one or two')
        self.limit=limit;self.entries={};self.lock=threading.Lock();self.event=threading.Event();self.peak=0

    def reserve(self,frame_id,at,source_at,nbytes):
        with self.lock:
            if len(self.entries)>=self.limit or frame_id in self.entries:raise ValueError('ACK window full or duplicate frame')
            self.entries[frame_id]=dict(frame_id=frame_id,at=at,source_at=source_at,nbytes=nbytes,ack_at=None)
            self.peak=max(self.peak,len(self.entries))

    def ack(self,frame_id,at,**metrics):
        with self.lock:
            entry=self.entries.get(frame_id)
            if entry is None or entry['ack_at'] is not None:return False
            entry.update(metrics,ack_at=at);self.event.set();return True

    def completed(self):
        with self.lock:
            rows=sorted((row for row in self.entries.values() if row['ack_at'] is not None),key=lambda row:row['ack_at'])
            for row in rows:del self.entries[row['frame_id']]
            return rows

    def full(self):
        with self.lock:return len(self.entries)>=self.limit

    def expired(self,now):
        with self.lock:return any(row['ack_at'] is None and now-row['at']>=3 for row in self.entries.values())

    def wait(self):
        with self.lock:
            if any(row['ack_at'] is not None for row in self.entries.values()):return
            self.event.clear()
        self.event.wait(.025)


@dataclass(frozen=True)
class Encoded:
    id:int
    token:object
    data:bytes
    codec:str
    width:int
    height:int
    quality:int
    raw_bytes:int
    encode_ms:float
    source_at:float
    version:tuple


class NetworkEncoder:
    def __init__(self,hub,camera,client_overlay=False,fast_lossless=False):
        self.hub=hub;self.camera=camera;self.lock=threading.Lock();self.ready=threading.Event()
        self.client_overlay=client_overlay
        self.fast_lossless=fast_lossless;self.lossless=None
        self.worker=None;self.latest=None;self.requested=0.;self.controller=None;self.version=None
        self.counter=0;self.stats={};self.ack_ms=0.;self.ack_window=1
        self.demands={}
        self.stopping=threading.Event()
        self.capture_generation=getattr(hub,'capture_generation',0)
        self.jpeg=None;self.png=None

    def get(self,after=None,timeout=1.5,consumer='default',max_fps=60):
        deadline=time.monotonic()+timeout
        while not self.hub.stop.is_set() and not self.stopping.is_set():
            with self.hub.lock:version=tuple(self.hub.config['transport'][self.camera].items())
            with self.lock:
                self.requested=time.monotonic()
                self.demands[consumer]=(self.requested,max_fps)
                if self.worker is None:
                    self.worker=threading.Thread(target=self.run,daemon=True);self.worker.start()
                item=self.latest
                if item is not None and item.version==version and item.id!=after and time.monotonic()-item.source_at<2:return item
                self.ready.clear()
            if time.monotonic()>=deadline:return None
            self.ready.wait(.025)
        return None

    def ack(self,elapsed_ms,window=1):
        with self.lock:self.ack_ms=max(0.,min(5000.,elapsed_ms));self.ack_window=window

    def close(self):
        self.stopping.set();self.ready.set()
        if self.worker is not None:
            self.worker.join(6)
            if self.worker.is_alive():raise RuntimeError('JPEG encoder still owns a capture frame')
        with self.lock:self.latest=None;self.demands.clear()

    def status(self):
        with self.lock:
            stats=dict(self.stats)
            if time.monotonic()-stats.get('delivery_at',0)>2:stats.update(delivered_fps=0.,wire_mbps=0.)
            return stats

    def run(self):
        if hasattr(os,'sched_getaffinity'):
            allowed=os.sched_getaffinity(0);preferred={cpu for cpu in allowed if cpu>=4}
            if len(preferred)>=2:
                try:
                    os.sched_setaffinity(0,preferred)
                    with self.lock:self.stats['encoder_cpu_affinity']=sorted(preferred)
                except OSError:pass
        self.jpeg=HardwareJPEG()
        last=None;due=0.
        costs=deque(maxlen=120);intervals=deque(maxlen=120);previous_start=None;profile_at=0.;overruns=0;profile={}
        while not self.hub.stop.is_set() and not self.stopping.is_set():
            with self.lock:
                self.demands={k:v for k,v in self.demands.items() if time.monotonic()-v[0]<1.5}
                active=[fps for at,fps in self.demands.values()]
                needed=bool(active);ceiling=max(active) if active else 1;ack=self.ack_ms;ack_window=self.ack_window
            with self.hub.lock:cfg=dict(self.hub.config['transport'][self.camera])
            key=tuple(cfg.items())
            if self.version!=key:
                self.controller=RateControl(cfg);self.version=key;last=None;due=0.
                costs.clear();intervals.clear();previous_start=None;profile_at=0.;overruns=0;profile={}
            if not needed:self.hub.stop.wait(.005);continue
            delay=due-time.monotonic()
            if delay>0:self.hub.stop.wait(min(.005,delay));continue
            try:
                # PNG is lossless relative to the full-resolution decoded BGR frame.
                prepare_started=time.monotonic()
                source=self.hub.clean_network_frame if self.client_overlay else self.hub.network_frame
                image,token,source_at=source(self.camera,last)
                prepare_ms=(time.monotonic()-prepare_started)*1000
                if image is None:
                    if self.client_overlay and hasattr(self.hub,'wait_network_source'):self.hub.wait_network_source(self.camera,last)
                    else:self.hub.stop.wait(.003)
                    continue
                started=time.monotonic();last=token
                if cfg['mode']=='lossy' and image.shape[1]>cfg['max_width']:
                    image=cv2.resize(image,(cfg['max_width'],round(image.shape[0]*cfg['max_width']/image.shape[1])),interpolation=cv2.INTER_AREA)
                lossless=cfg['mode']=='lossless';quality=self.controller.quality
                codec='jpeg'
                if lossless:
                    data=None
                    if self.fast_lossless:
                        if self.lossless is None:self.lossless=FastLossless()
                        data=self.lossless.encode(image)
                    if data is not None:codec='pcz';engine=self.lossless.backend
                    else:
                        if self.png is None:self.png=FastPNG(workers=3)
                        data=self.png.encode(image);engine=self.png.backend;codec='png'
                else:
                    data=self.jpeg.encode(image,quality)
                    engine='RK MPP JPEG' if data is not None else 'OpenCV CPU'
                    if data is None:
                        ok,array=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,quality])
                        if not ok:raise ValueError('network encoding failed')
                        data=array.tobytes()
                ms=(time.monotonic()-started)*1000
                fps=self.controller.feedback(len(data),ms,ack,ceiling,ack_window=ack_window);self.counter+=1
                costs.append(ms)
                if previous_start is not None:intervals.append((started-previous_start)*1000)
                previous_start=started;overruns+=ms>1000/cfg['fps']
                profile_now=time.monotonic()
                if profile_now-profile_at>=.5:
                    ordered=sorted(costs);gaps=sorted(intervals)
                    profile=dict(encode_p95_ms=round(ordered[int(.95*(len(ordered)-1))],2),
                        encode_peak_ms=round(max(ordered),2),encoder_interval_p95_ms=round(gaps[int(.95*(len(gaps)-1))],2) if gaps else None)
                    profile_at=profile_now
                # Timestamp is the source frame time, never merely the encoding time.
                item=Encoded(self.counter,token,data,codec,image.shape[1],image.shape[0],0 if lossless else quality,image.nbytes,ms,source_at,key)
                with self.lock:
                    self.latest=item;self.stats.update(codec=item.codec,quality=item.quality,width=item.width,height=item.height,
                        encoded_bytes=len(data),raw_bytes=image.nbytes,saving_pct=round(100*(1-len(data)/image.nbytes),2),
                        encode_ms=round(ms,2),prepare_ms=round(prepare_ms,2),scheduled_fps=round(fps,2),ack_ms=round(ack,2),encoder=engine,hardware_error=self.jpeg.error if not lossless else '',png_error=self.png.error if lossless and self.png is not None else '',lossless_error=self.lossless.error if lossless and self.lossless is not None else '',error='',overlay_backend='PC local' if self.client_overlay else 'board')
                    self.stats.update(profile,encode_overruns=overruns,encode_capacity_fps=round(self.controller.encode_capacity_fps,2))
                    self.stats.update(getattr(self.jpeg,'timings',{}) if not lossless and engine=='RK MPP JPEG' else dict(color_ms=None,hardware_ms=None,input_format='BGR CPU',buffer_policy='CPU memory',input_copy_ms=None,input_sync_ms=None,rga_ms=None,output_cpu_ms=None))
                    self.ready.set()
                # Preparation counts toward the period. Keep the absolute
                # cadence through short wakeup jitter, reset after a long stall.
                # The source supplies distinct latest frames, never a replay
                # queue to manufacture fps when the producer falls behind.
                due=next_encode_deadline(due,prepare_started,time.monotonic(),fps)
            except Exception as exc:
                with self.lock:self.stats['error']=str(exc)
                self.hub.stop.wait(.05)
        self.jpeg.close()
        if self.png is not None:self.png.close()
        if self.lossless is not None:self.lossless.close()
