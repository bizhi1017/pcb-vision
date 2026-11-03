"""Dual-camera service. Input devices must be confirmed; --demo never opens hardware."""
import argparse
import json
import threading
import time
import socket
import base64
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from urllib.request import Request, urlopen
import cv2
import numpy as np
from snapshot_storage import SnapshotStore,StorageError,snapshot_name
from event_log import EventLog
from adaptive_transport import NetworkEncoder,AckWindow,DEFAULT as NET_DEFAULT,settings as net_settings
from live_overlay import LiveOverlay
from browser_preview import BrowserPreview,network_info,asset as browser_asset,PROFILES as BROWSER_PROFILES
import uuid
from collections import deque
from capture_profiles import DEFAULT as CAPTURE_DEFAULT,profile as capture_profile,selection as capture_selection

CHANNELS = ('A', 'B')
PCB_CLASSES=('missing','stain','scratch')
PCB_LABELS=('缺件','污渍','划痕')
SOFTWARE_REVISION='dual-transport-v3.13.0-p9'


class Hub:
    def __init__(self, calibrations=None, model=None, hdmi_available=False,calibration_dir=None,snapshot_store=None):
        self.lock = threading.RLock()
        self.events=EventLog()
        self.events.emit('双路服务初始化 · '+SOFTWARE_REVISION)
        self.stop = threading.Event()
        self.capture_profile=CAPTURE_DEFAULT;self.capture_channels=set(CHANNELS);self.capture_generation=0;self.capture_switching=False
        self.capture_controller=None;self.capture_saved_1080=None
        self.config = {'hdmi': 'off', 'push_url': '', 'push_fps': 2,
                       'pre':{'A':0,'B':0},'threshold':{'A':200,'B':200},
                       'preview':{'A':'detail','B':'detail'},
                       'transport':{c:dict(NET_DEFAULT) for c in CHANNELS},
                       'fpga_codec':{c:'lossless' for c in CHANNELS},
                       'image':{'A':{'gain':1.,'black':16,'gamma':2.2},'B':{'gain':1.,'black':16,'gamma':2.2}},
                       'push': {'A': False, 'B': False}, 'detect': {'A': False, 'B': False},'browser_push':{'A':True,'B':True}}
        self.config['capture']=dict(profile='1080p60',channels=['A','B'])
        self.channels = {c: {'frame': None, 'processed': None, 'processed_seq': 0,
                            'seq': 0, 'at': 0., 'fps': 0., 'error': '',
                            'push_ok': 0, 'push_error': '', 'infer_ms': None,
                            'detect_epoch':0,'detections':[],'infer_error':'','infer_fps':0.,
                            'measure_busy': False,'processed_reference':None} for c in CHANNELS}
        self.live_overlays={c:LiveOverlay() for c in CHANNELS}
        self.calibrations = calibrations or {}
        self.calibration_dir=calibration_dir;self.calibration_sessions={}
        self.calibration_locks={c:threading.Lock() for c in CHANNELS}
        self.model = model
        self.hdmi_available = hdmi_available
        self.hdmi_stats=dict(mode='off',backend='inactive',video_fps=0.,submit_fps=0.)
        self.pre_control=None
        self.memory={}
        self.network={c:NetworkEncoder(self,c) for c in CHANNELS};self.net_sessions={}
        # New Qt subscribers get unmodified video and separate detection metadata.
        # Legacy Qt/Wi-Fi retain the existing annotated encoder, only on demand.
        self.client_network={c:NetworkEncoder(self,c,client_overlay=True) for c in CHANNELS}
        self.fast_client_network={c:NetworkEncoder(self,c,client_overlay=True,fast_lossless=True) for c in CHANNELS}
        self.client_references={}
        self.annotation_cache={};self.annotation_locks={c:threading.Lock() for c in CHANNELS}
        self.snapshot_store=snapshot_store if snapshot_store is not None else SnapshotStore()
        self.snapshot_locks={c:threading.Lock() for c in CHANNELS}
        self.hq_cache={};self.hq_threads={};self.hq_requests={};self.hq_ready={c:threading.Event() for c in CHANNELS}
        self.raw_ready={c:threading.Event() for c in CHANNELS}
        self.compat_requests={}
        self.browser=BrowserPreview(self)

    def quiesce_capture(self,generation):
        with self.lock:
            self.capture_switching=True;self.capture_generation=generation;self.config['hdmi']='off'
            workers=list(self.hq_threads.values());encoders=list(self.network.values())+list(self.client_network.values())+list(self.fast_client_network.values())
            for c,s in self.channels.items():
                s.update(frame=None,raw_packet=None,gray_packet=None,at=0.,processed=None,processed_reference=None,detections=[],detect_epoch=s['detect_epoch']+1,
                         fps=0.,hq_fps=0.,infer_fps=0.,infer_ms=None,error='',hq_error='',infer_error='',push_error='',hq_source_skipped=0)
                self.raw_ready[c].set();self.hq_ready[c].set()
        for encoder in encoders:encoder.close()
        for worker in workers:
            worker.join(6)
            if worker.is_alive():raise RuntimeError('旧高清转换尚未结束')
        deadline=time.monotonic()+6
        while self.hdmi_stats.get('mode','off')!='off' and not self.stop.is_set():
            if time.monotonic()>deadline:raise RuntimeError('HDMI旧映射尚未释放')
            self.stop.wait(.02)
        with self.lock:
            self.hq_threads.clear();self.hq_cache.clear();self.hq_requests.clear();self.client_references.clear();self.annotation_cache.clear();self.calibration_sessions.clear()
            self.network={c:NetworkEncoder(self,c) for c in CHANNELS};self.client_network={c:NetworkEncoder(self,c,client_overlay=True) for c in CHANNELS}
            self.fast_client_network={c:NetworkEncoder(self,c,client_overlay=True,fast_lossless=True) for c in CHANNELS}
            self.live_overlays={c:LiveOverlay() for c in CHANNELS};self.browser=BrowserPreview(self)

    def prepare_capture(self,target,generation):
        p=capture_profile(target['profile'])
        with self.lock:
            if p.mode and not self.capture_profile.mode:
                self.capture_saved_1080={k:json.loads(json.dumps(self.config[k])) for k in ('transport','detect','pre','threshold','push','browser_push','preview')}
            if not p.mode and self.capture_saved_1080:
                self.config.update(self.capture_saved_1080);self.capture_saved_1080=None
            self.capture_profile=p;self.capture_generation=generation;self.capture_channels=set(target['channels']);self.config['capture']=json.loads(json.dumps(target))
            for c in CHANNELS:
                if p.mode:
                    self.config['pre'][c]=0
                    self.config['transport'][c].update(max_width=p.width,fps=p.fps,mbps=600.,mode='lossy')
                    self.config['preview'][c]='detail'
                if c not in self.capture_channels:
                    for k in ('detect','push','browser_push'):self.config[k][c]=False
            if self.calibration_dir:
                from board_calibration import validate_calibration
                selected={}
                for c in CHANNELS:
                    path=self.calibration_folder()/('camera_'+c+'.json')
                    if path.is_file():validate_calibration(json.loads(path.read_text(encoding='utf-8')),c);selected[c]=str(path)
                self.calibrations=selected

    def calibration_folder(self):
        if not self.calibration_dir:raise ValueError('未配置标定保存目录')
        base=Path(self.calibration_dir)
        return base/'4k30' if self.capture_profile.mode else base

    def finish_capture(self,target,hdmi):
        with self.lock:
            self.capture_switching=False
            self.config['hdmi']=('split' if len(target['channels'])==2 else target['channels'][0]) if hdmi!='off' else 'off'
        self.events.emit('采集模式：'+target['profile']+' · '+','.join(target['channels']))

    def configure(self, patch):
        if isinstance(patch,dict) and 'capture' in patch:
            if set(patch)!={'capture'}:raise ValueError('采集模式需单独提交')
            if self.capture_controller is None:raise ValueError('当前采集源不支持档位切换')
            self.capture_controller.request(patch['capture']);return self.status()
        if not isinstance(patch, dict) or set(patch)-set(self.config):
            raise ValueError('unknown configuration')
        if 'fpga_codec' in patch and ('pre' in patch or 'threshold' in patch):
            raise ValueError('apply FPGA codec and preprocess in separate requests')
        with self.lock:
            if self.capture_switching:raise ValueError('采集模式切换中，请稍候')
            new = json.loads(json.dumps(self.config))
            for k, v in patch.items():
                if k=='fpga_codec':
                    if self.pre_control is None or not getattr(self.pre_control,'codec_enabled',False):raise ValueError('P7 FPGA codec bitstream not active')
                    if not isinstance(v,dict) or set(v)-set(CHANNELS) or any(x not in ('raw','lossless','lossy9','lossy6') for x in v.values()):raise ValueError('invalid FPGA compression mode')
                    new[k].update(v)
                    continue
                if k=='transport':
                    if not isinstance(v,dict) or set(v)-set(CHANNELS):raise ValueError('invalid transport camera')
                    for c,values in v.items():
                        new[k][c]=net_settings(new[k][c],values)
                        if new[k][c]['max_width']>self.capture_profile.width:raise ValueError('网络尺寸不能超过真实采集尺寸')
                        if new[k][c]['fps']>self.capture_profile.fps:raise ValueError('网络帧率不能超过采集档位')
                        new['preview'][c]='detail' if new[k][c]['max_width']==self.capture_profile.width else 'fast'
                elif k in ('pre','threshold'):
                    if self.pre_control is None:raise ValueError('FPGA preprocess bitstream not active')
                    maximum=4 if k=='pre' else 2040
                    if not isinstance(v,dict) or set(v)-set(CHANNELS) or any(type(x)is not int or not 0<=x<=maximum for x in v.values()):raise ValueError('invalid preprocess settings')
                    new[k].update(v)
                elif k=='preview':
                    if not isinstance(v,dict) or set(v)-set(CHANNELS) or any(x not in ('fast','detail') for x in v.values()):raise ValueError('preview fast/detail')
                    if any(mode=='detail' and 'raw_packet' not in self.channels[c] for c,mode in v.items()):raise ValueError('full RAW frame not available yet')
                    new[k].update(v)
                    for c,mode in v.items():
                        new['transport'][c]['max_width']=self.capture_profile.width if mode=='detail' else 960
                        if mode=='fast':new['transport'][c]['mode']='lossy'
                elif k=='image':
                    if not isinstance(v,dict) or set(v)-set(CHANNELS):raise ValueError('invalid image channels')
                    for camera,values in v.items():
                        if not isinstance(values,dict) or set(values)-{'gain','black','gamma'}:raise ValueError('invalid image settings')
                        for field,value in values.items():
                            if field=='gain' and (type(value) not in (int,float) or not .25<=value<=2):raise ValueError('gain 0.25..2')
                            if field=='black' and (type(value) is not int or not 0<=value<=64):raise ValueError('black 0..64')
                            if field=='gamma' and (type(value) not in (int,float) or not 1<=value<=3):raise ValueError('gamma 1..3')
                            new[k][camera][field]=value
                elif k in ('push', 'detect','browser_push'):
                    if not isinstance(v, dict) or set(v)-set(CHANNELS): raise ValueError('invalid channels')
                    if any(type(x) is not bool for x in v.values()): raise ValueError('expected boolean')
                    if k == 'detect' and any(v.values()) and self.model is None:
                        raise ValueError('RKNN model is not loaded')
                    new[k].update(v)
                elif k == 'hdmi':
                    if v not in ('off', 'A', 'B', 'split'): raise ValueError('invalid HDMI mode')
                    if v != 'off' and not self.hdmi_available: raise ValueError('HDMI window not enabled on board')
                    new[k] = v
                elif k == 'push_url':
                    if v and (urlparse(v).scheme not in ('http', 'https') or not urlparse(v).netloc):
                        raise ValueError('invalid HTTP push URL')
                    new[k] = v
                elif k == 'push_fps':
                    if type(v) not in (int, float) or not 0.2 <= v <= 10: raise ValueError('push_fps 0.2..10')
                    new[k] = v
            if any(new['push'].values()) and not new['push_url']: raise ValueError('push URL required')
            for c in CHANNELS:
                if new['detect'][c] and not self.config['detect'][c] and c not in patch.get('preview',{}):
                    new['preview'][c]='detail'
                    new['transport'][c]['max_width']=self.capture_profile.width
                if new['pre'][c]:
                    if patch.get('detect',{}).get(c):raise ValueError('return to color mode before defect detection')
                    new['detect'][c]=False
            for c in CHANNELS:
                if c not in self.capture_channels and any(new[k][c] for k in ('detect','push','browser_push')):raise ValueError('该相机已在单路采集模式中停止')
                if self.capture_profile.mode and new['pre'][c]:raise ValueError('4K模式当前只支持彩色原图')
                if new['transport'][c]['mode']=='lossless' and new['transport'][c]['max_width']!=self.capture_profile.width:raise ValueError('PNG无损预览必须保留真实源尺寸')
            if new['hdmi']=='split' and len(self.capture_channels)!=2 or new['hdmi'] in CHANNELS and new['hdmi'] not in self.capture_channels:raise ValueError('HDMI目标相机已停止')
            write_started=time.monotonic()
            if 'fpga_codec' in patch:self.pre_control.apply_codec(new['fpga_codec'])
            if 'pre' in patch or 'threshold' in patch:self.pre_control.apply(new['pre'],new['threshold'])
            for c in CHANNELS:
                if c not in self.capture_channels and any(new[k][c] for k in ('detect','push','browser_push')):raise ValueError('该相机已在单路采集模式中停止')
                if self.capture_profile.mode and new['pre'][c]:raise ValueError('4K模式当前只支持彩色原图')
                if new['detect'][c]!=self.config['detect'][c] or new['pre'][c]!=self.config['pre'][c] or new['fpga_codec'][c]!=self.config['fpga_codec'][c]:
                    s=self.channels[c]
                    s.update(detect_epoch=s['detect_epoch']+1,processed=None,processed_reference=None,processed_seq=0,detections=[],infer_error='',infer_ms=None,infer_fps=0.,infer_at=time.monotonic(),inference_source_width=None,inference_source_height=None,hq_decode_ms=None)
                if new['pre'][c]!=self.config['pre'][c] or new['threshold'][c]!=self.config['threshold'][c]:
                    self.channels[c]['control_write_ms']=round((time.monotonic()-write_started)*1000,2)
                    if new['pre'][c]!=self.config['pre'][c]:
                        self.channels[c].update(switch_started=time.monotonic(),switch_to_frame_ms=None)
            self.config = new
            changes=[]
            for key,title in [('pre','预处理'),('threshold','边缘阈值'),('detect','缺陷检测'),('push','远程推送'),('browser_push','手机网页推送'),('preview','预览模式'),('image','亮度设置'),('transport','网络压缩'),('fpga_codec','FPGA编码')]:
                if key in patch:changes.append(title+' '+','.join(c+':'+str(new[key][c]) for c in patch[key]))
            if 'hdmi' in patch:changes.append('HDMI '+new['hdmi'])
            if 'push_url' in patch:changes.append('远程接收地址已更新')
            if changes:self.events.emit('；'.join(changes))
            return self.status()

    def publish(self, channel, frame,**metadata):
        now = time.monotonic()
        with self.lock:
            if channel not in self.capture_channels or metadata.get('capture_generation',self.capture_generation)!=self.capture_generation:return
            s = self.channels[channel]
            if 'mode_applied' in metadata and metadata['mode_applied']==self.config['pre'][channel] and 'switch_started' in s:
                s['switch_to_frame_ms']=round((now-s.pop('switch_started'))*1000,2)
            if metadata.get('mode_applied',0)!=s.get('mode_applied',0):s.update(processed=None,processed_seq=0)
            interval = now-s['at']
            s['fps'] = .8*s['fps']+.2/interval if s['at'] and interval > 0 else 0
            s.update(seq=s['seq']+1, at=now, error='')
            if frame is not None:s.update(frame=frame.copy(),preview_seq=s['seq'],preview_at=now)
            old_kind=(bool(s.get('raw_packet')),bool(s.get('gray_packet')))
            s.update(metadata)
            if old_kind!=(bool(s.get('raw_packet')),bool(s.get('gray_packet'))):
                self.hq_cache.pop(channel,None);self.hq_ready[channel].clear()
            self.raw_ready[channel].set()

    def publish_preview(self,channel,frame,sequence,source_at,mode=0,capture_generation=None):
        with self.lock:
            if channel not in self.capture_channels or capture_generation is not None and capture_generation!=self.capture_generation:return
            s=self.channels[channel]
            if mode==s.get('mode_applied',0):s.update(frame=frame.copy(),preview_seq=sequence,preview_at=source_at)

    def _decode_hq(self,channel):
        from vid008_pcie import full_resolution,processed_frame,hq_native,ImageBufferPool
        # Network encoder, inference reference and HDMI's active/latest tracker
        # inputs can overlap. More reusable storage is not a longer frame queue.
        geometry=self.capture_profile;generation=self.capture_generation
        image_buffers=ImageBufferPool(shape=geometry.shape,limit=6 if geometry.mode else 8,shared=True)
        if hasattr(os,'sched_getaffinity'):
            allowed=os.sched_getaffinity(0);preferred={cpu for cpu in allowed if cpu>=4}
            if len(preferred)>=2:
                try:
                    os.sched_setaffinity(0,preferred)
                    with self.lock:self.channels[channel]['hq_cpu_affinity']=sorted(preferred)
                except OSError:pass
        last=None;small_at=0.;small_mode=None;published_times=deque(maxlen=240)
        decode_costs=deque(maxlen=120);decode_overruns=0;profile_at=0.;decode_profile={}
        while not self.stop.is_set() and generation==self.capture_generation:
            with self.lock:
                s=self.channels[channel];now=time.monotonic()
                needed=now-self.hq_requests.get(channel,0)<1 and now-s['at']<2
                packet=s.get('raw_packet');gray=s.get('gray_packet');seq=s['seq'];mode=s.get('mode_applied',0)
                params=dict(s.get('raw_parameters',{}));source_at=s['at']
                if seq==last:self.raw_ready[channel].clear()
            if not needed or not (packet or gray):self.stop.wait(.005);continue
            if seq==last:self.raw_ready[channel].wait(.02);continue
            started=time.monotonic()
            try:
                if gray:
                    image,_,_=processed_frame(gray,CHANNELS.index(channel),geometry);image=cv2.cvtColor(image,cv2.COLOR_GRAY2BGR)
                else:image,_=full_resolution(packet,CHANNELS.index(channel),output=image_buffers.acquire(),**params)
                decoded_ms=(time.monotonic()-started)*1000
                decode_costs.append(decoded_ms);decode_overruns+=decoded_ms>1000/geometry.fps
                if time.monotonic()-profile_at>=.5:
                    ordered=sorted(decode_costs)
                    decode_profile=dict(hq_decode_p95_ms=round(ordered[int(.95*(len(ordered)-1))],2),hq_decode_peak_ms=round(max(ordered),2))
                    profile_at=time.monotonic()
                # HQ is already immutable and complete. Reuse it for the small
                # compatibility preview instead of a second RAW/RGB conversion.
                with self.lock:legacy=time.monotonic()-self.compat_requests.get(channel,0)<.5
                preview=None
                if mode!=small_mode or time.monotonic()-small_at>=(1/30 if legacy else .5):
                    preview=cv2.resize(image,(960,540),interpolation=cv2.INTER_AREA);small_at=time.monotonic();small_mode=mode
                with self.lock:
                    current=self.channels[channel]
                    if (generation==self.capture_generation and current.get('mode_applied',0)==mode and bool(current.get('raw_packet'))==bool(packet)
                            and bool(current.get('gray_packet'))==bool(gray)):
                        self.hq_cache[channel]=(image,seq,source_at,mode)
                        native=hq_native()
                        backend='fpga-gray8' if gray else ('native-fused-ea-pthread' if native and hasattr(native,'hq_fused_ea_parallel') else 'native-fused-ea' if native and hasattr(native,'hq_fused_ea') else 'native-hq' if native else 'numpy-hq')
                        self.channels[channel].update(hq_shared_decode_ms=round(decoded_ms,2),hq_preview_ms=round((time.monotonic()-started)*1000-decoded_ms,2),hq_backend=backend,hq_error='',hq_buffer_pool=image_buffers.stats())
                        self.channels[channel].update(decode_profile,hq_decode_overruns=decode_overruns)
                        published=time.monotonic();published_times.append(published)
                        while len(published_times)>2 and published_times[0]<published-2:published_times.popleft()
                        span=published-published_times[0]
                        self.channels[channel]['hq_fps']=(len(published_times)-1)/span if span else 0.
                        self.channels[channel]['hq_source_skipped']=self.channels[channel].get('hq_source_skipped',0)+max(0,seq-last-1) if last is not None else 0
                        if preview is not None:self.publish_preview(channel,preview,seq,source_at,mode)
                        self.hq_ready[channel].set()
                last=seq
            except Exception as exc:
                with self.lock:self.channels[channel]['hq_error']=str(exc)
            with self.lock:
                target=max(30,self.config['transport'][channel]['fps'])
            # Follow requested demand up to sensor rate; preserve full EA demosaic.
            # At sensor rate, a second clock drifts against RAW arrival and
            # drops source frames. Wait for the next sequence/event instead.
            if target<self.capture_profile.fps:self.stop.wait(max(.001,1/target-(time.monotonic()-started)))

    def hq_snapshot(self,channel,min_source_at=None):
        deadline=time.monotonic()+1
        while not self.stop.is_set():
            with self.lock:
                if channel not in self.capture_channels or self.capture_switching:raise ValueError('相机已停止或采集模式切换中')
                s=self.channels[channel];now=time.monotonic()
                if now-s['at']>=2:raise ValueError('no fresh RAW frame')
                if not (s.get('raw_packet') or s.get('gray_packet')):
                    return s['frame'].copy(),s['seq'],s['at'],s.get('mode_applied',0)
                self.hq_requests[channel]=now
                if channel not in self.hq_threads:
                    worker=threading.Thread(target=self._decode_hq,args=(channel,),daemon=True)
                    self.hq_threads[channel]=worker;worker.start()
                cached=self.hq_cache.get(channel)
                if (cached and cached[3]==s.get('mode_applied',0) and now-cached[2]<2
                    and (min_source_at is None or cached[2]>=min_source_at)):return cached
                self.hq_ready[channel].clear()
            if time.monotonic()>deadline:raise ValueError('waiting for shared HQ decoder')
            self.hq_ready[channel].wait(.05)
        raise ValueError('capture stopped')

    def detail_frame(self,channel,after_seq=None):
        image,seq,_,_=self.hq_snapshot(channel)
        if seq==after_seq:return None,seq
        return image.copy(),seq

    def hdmi_source(self,channel):
        """Nonblocking, immutable shared HQ view; never an annotated NPU frame."""
        with self.lock:
            s=self.channels[channel];now=time.monotonic();mode=s.get('mode_applied',0)
            fresh=s['frame'] is not None and now-s['at']<2
            image=None;seq=None;source_at=s['at']
            if fresh:
                if s.get('raw_packet') or s.get('gray_packet'):
                    self.hq_requests[channel]=now
                    if channel not in self.hq_threads:
                        worker=threading.Thread(target=self._decode_hq,args=(channel,),daemon=True)
                        self.hq_threads[channel]=worker;worker.start()
                    cached=self.hq_cache.get(channel)
                    if cached and cached[3]==mode and now-cached[2]<2:image,seq,source_at,mode=cached
                else:image,seq=s['frame'],s['seq']
            active=(image is not None and self.config['detect'][channel] and mode==0 and not s['infer_error']
                    and s['processed_reference'] is not None and s['processed_seq']<=seq
                    and 0<=now-s.get('processed_source_at',0)<.8)
            shared_reference=self.client_references.get(channel)
            reference_png=shared_reference[1] if shared_reference and shared_reference[0]==(s['detect_epoch'],s['processed_seq']) else None
            return dict(image=image,seq=seq,source_at=source_at,mode=mode,detect=self.config['detect'][channel],
                        detect_epoch=s['detect_epoch'],active=bool(active),processed_seq=s['processed_seq'],
                        reference=s['processed_reference'] if active else None,result_at=s.get('processed_source_at',0),reference_png=reference_png,detections=list(s['detections'][:128]) if active else [])

    def measurement_image(self,channel):
        image,seq,_,mode=self.hq_snapshot(channel,min_source_at=time.monotonic()-.1)
        if mode!=0:raise ValueError('请先切回彩色原图，再进行标定或板框测量')
        if image is None:raise ValueError('没有可用相机画面')
        return cv2.resize(image,(960,540),interpolation=cv2.INTER_AREA),seq

    def snapshot_image(self,channel,include_counts=False):
        """Save an exact inspected source; never fall back to an uninspected image."""
        deadline=time.monotonic()+2
        while not self.stop.is_set():
            result=None
            with self.lock:
                s=self.channels[channel];now=time.monotonic()
                if channel not in self.capture_channels or self.capture_switching:raise ValueError('相机已停止或采集模式切换中')
                if s['frame'] is None or now-s['at']>=2:raise ValueError('本路没有新鲜图像，无法截图')
                if not self.config['detect'][channel]:raise ValueError('请先开启本路检测，再保存带缺陷识别框的截图')
                if self.config['pre'][channel]!=0 or s.get('mode_applied',0)!=0:raise ValueError('请先切回彩色原图并开启本路检测')
                if s['infer_error']:raise ValueError('本路检测异常，未保存：'+s['infer_error'])
                if s['processed'] is not None and 0<=now-s.get('processed_source_at',0)<.8:
                    annotations=s.get('processed_annotation')
                    classes=list(annotations['classes']) if annotations is not None else [d['class_id'] for d in s['detections']]
                    if any(type(cid)is not int or cid not in (0,1,2) for cid in classes):raise ValueError('缺陷类别无效，未保存')
                    counts={name:classes.count(cid) for cid,name in enumerate(PCB_CLASSES)}
                    result=(s['processed'],s['processed_seq'],s['detect_epoch'],annotations,counts,self.capture_generation)
            if result:
                image,seq,epoch,annotations,counts,generation=result
                frame=self.result_image(channel,image,seq,epoch,annotations).copy()
                with self.lock:
                    if (self.capture_generation!=generation or self.capture_switching
                        or self.channels[channel]['detect_epoch']!=epoch or not self.config['detect'][channel]):
                        raise ValueError('截图期间检测或采集模式已变化，请重试')
                packed=(frame,seq,'detection',0)
                return packed+(counts,) if include_counts else packed
            if now>=deadline:raise ValueError('未取得有效的新鲜检测结果，未保存，请等待检测恢复后重试')
            self.stop.wait(.02)
        raise ValueError('相机服务已停止，未保存')

    def result_image(self,channel,image,seq,epoch,annotations):
        if annotations is None:return image
        key=(epoch,seq)
        with self.annotation_locks[channel]:
            with self.lock:cached=self.annotation_cache.get(channel)
            if cached and cached[0]==key:return cached[1]
            from inference_model import render_annotations
            result=render_annotations(image,annotations);result.flags.writeable=False
            with self.lock:
                if self.channels[channel]['detect_epoch']==epoch:self.annotation_cache[channel]=(key,result)
            return result

    def display_frame(self,channel,after_seq=None,force_detail=False,copy_frame=True):
        with self.lock:
            s=self.channels[channel];now=time.monotonic()
            if s['frame'] is None or now-s['at']>2:raise ValueError('camera has no fresh frame')
            detail=force_detail or self.config['preview'][channel]=='detail'
            detected=self.config['detect'][channel] and s['processed'] is not None and now-s.get('processed_source_at',0)<=.4
            seq=s['processed_seq'] if detected else s['seq'] if detail else s.get('preview_seq',s['seq'])
            token=(seq,detail,detected)
            if token==after_seq:return None,token
            image=s['processed'] if detected else s['frame']
            annotations=s.get('processed_annotation') if detected else None;epoch=s['detect_epoch']
            has_full=bool(s.get('raw_packet') or s.get('gray_packet'))
        if detail and not detected and has_full:image,seq,_,_=self.hq_snapshot(channel)
        if detected:image=self.result_image(channel,image,seq,epoch,annotations)
        if (seq,detail,detected)==after_seq:return None,after_seq
        if detail:image=image.copy() if copy_frame else image
        elif not copy_frame and image.shape[:2]==(540,960):pass
        else:image=cv2.resize(image,(960,540))
        return image,(seq,detail,detected)

    def clean_network_frame(self,channel,after=None):
        image,seq,at,mode=self.hq_snapshot(channel)
        with self.lock:epoch=self.channels[channel]['detect_epoch']
        token=(seq,mode,False,epoch,0)
        if after and after[0]==seq and after[1]==mode:return None,token,at
        return image,token,at

    def wait_network_source(self,channel,after,timeout=.02):
        with self.lock:
            source=self.channels[channel]
            if not (source.get('raw_packet') or source.get('gray_packet')):
                if after is None or source['seq']!=after[0]:return
                ready=self.raw_ready[channel]
            else:ready=self.hq_ready[channel]
            cached=self.hq_cache.get(channel)
            if cached and (after is None or cached[1]!=after[0]):return
            ready.clear()
        ready.wait(timeout)

    def client_overlay_metadata(self,channel,packet,previous=None):
        with self.lock:
            s=self.channels[channel];epoch=s['detect_epoch'];seq=s['processed_seq']
            age=(time.monotonic()-s.get('processed_source_at',0))*1000
            active=(self.config['detect'][channel] and packet.token[1]==0 and not s['infer_error']
                    and packet.token[3]==epoch and s['processed_reference'] is not None
                    and seq<=packet.token[0] and 0<=age<800)
            meta=dict(enabled=bool(active),epoch=epoch,result_seq=seq,age_ms=round(age,1),max_age_ms=800)
            if not active:return meta,None
            key=(epoch,seq);reference=s['processed_reference'];detections=list(s['detections'][:128])
            cached=self.client_references.get(channel)
        meta.update(detections=detections,width=reference.shape[1],height=reference.shape[0])
        if key!=previous:
            if cached is None or cached[0]!=key:
                gray=LiveOverlay.gray(reference)
                ok,png=cv2.imencode('.png',gray,[cv2.IMWRITE_PNG_COMPRESSION,1])
                if not ok:raise ValueError('tracking reference encoding failed')
                cached=(key,base64.b64encode(png).decode('ascii'))
                with self.lock:self.client_references[channel]=cached
            meta['reference_png']=cached[1]
        return meta,key

    def network_frame(self,channel,after=None):
        with self.lock:
            s=self.channels[channel];now=time.monotonic();epoch=s['detect_epoch']
            if s['frame'] is None or now-s['at']>=2:raise ValueError('no fresh camera frame')
        image,seq,at,mode=self.hq_snapshot(channel)
        with self.lock:
            s=self.channels[channel];epoch=s['detect_epoch']
            active=(self.config['detect'][channel] and not mode and not s['infer_error'] and s['processed_reference'] is not None
                    and time.monotonic()-s.get('processed_source_at',0)<=.4)
            result_seq=s['processed_seq'] if active else 0
            reference=s['processed_reference'];detections=list(s['detections']);result_at=s.get('processed_source_at',0)
        token=(seq,mode,bool(active),epoch,result_seq)
        if token==after or (after and after[0]==seq and after[1]==mode and after[3]==epoch):return None,token,at
        original=image;overlay='off';error='';overlay_started=time.monotonic()
        if active:
            try:image,overlay=self.live_overlays[channel].render(image,reference,detections,(epoch,result_seq),exact=seq==result_seq)
            except (cv2.error,ValueError,KeyError,IndexError) as exc:overlay='waiting';error=str(exc)
        elif self.config['detect'][channel]:overlay='waiting'
        with self.lock:
            if s['detect_epoch']!=epoch or (active and not self.config['detect'][channel]):
                image=original;overlay='off';token=(seq,mode,False,s['detect_epoch'],0)
            s.update(preview_overlay=overlay,preview_overlay_error=error,preview_overlay_ms=round((time.monotonic()-overlay_started)*1000,2),preview_result_age_ms=round((time.monotonic()-result_at)*1000,1) if active else None)
        return image,token,at

    def status(self):
        sd=self.snapshot_store.status()
        inference_worker=self.model.status() if hasattr(self.model,'status') else {}
        with self.lock:
            now = time.monotonic()
            channels = {}
            for c, s in self.channels.items():
                fresh = s['frame'] is not None and now-s['at'] < 2
                channels[c] = dict(online=fresh,enabled=c in self.capture_channels,capture_profile=self.capture_profile.id, seq=s['seq'], fps=round(s['fps'],1) if fresh else 0,
                                   error=s['error'], infer_ms=s['infer_ms'],
                                   infer_fps=round(s['infer_fps'],1),infer_error=s['infer_error'],
                                   inference_timings=dict(s.get('inference_timings',{})),infer_completed=s.get('infer_completed',0),
                                   inference_source_width=s.get('inference_source_width'),inference_source_height=s.get('inference_source_height'),
                                   hq_decode_ms=s.get('hq_decode_ms'),
                                   hq_shared_decode_ms=s.get('hq_shared_decode_ms'),hq_error=s.get('hq_error',''),
                                   hq_fps=round(s.get('hq_fps',0),1),hq_source_skipped=s.get('hq_source_skipped',0),
                                   hq_cpu_affinity=s.get('hq_cpu_affinity'),
                                   awb_backend=s.get('awb_backend','not sampled'),
                                   hq_preview_ms=s.get('hq_preview_ms'),preview_backend=s.get('preview_backend','independent'),
                                   preview_overlay=s.get('preview_overlay','off'),preview_overlay_error=s.get('preview_overlay_error',''),preview_overlay_ms=s.get('preview_overlay_ms'),preview_result_age_ms=s.get('preview_result_age_ms'),
                                   hq_backend=s.get('hq_backend','not started'),hq_wait_ms=s.get('hq_wait_ms'),
                                   hq_decode_p95_ms=s.get('hq_decode_p95_ms'),hq_decode_peak_ms=s.get('hq_decode_peak_ms'),hq_decode_overruns=s.get('hq_decode_overruns',0),
                                   detections=s['detections'] if self.config['detect'][c] and now-s.get('processed_source_at',0)<.4 else [],
                                   client_detections=s['detections'] if self.config['detect'][c] and now-s.get('processed_source_at',0)<.8 else [],
                                   client_detection_fresh=fresh and self.config['detect'][c] and s['processed'] is not None and not s['infer_error'] and now-s.get('processed_source_at',0)<.8,
                                   detection_frame_seq=s['processed_seq'],
                                   detection_fresh=fresh and s['processed'] is not None and now-s.get('processed_source_at',0)<.4,
                                   push_ok=s['push_ok'], push_error=s['push_error'],
                                   dma_wait_ms=s.get('dma_wait_ms'), decode_ms=s.get('decode_ms'),
                                   dma_decode_cpu_affinity=s.get('dma_decode_cpu_affinity'),
                                   decode_max_ms=s.get('decode_max_ms'), source_gaps=s.get('source_gaps',0),
                                   invalid_packets=s.get('invalid_packets',0),dma_queue_dropped=s.get('dma_queue_dropped',0),hq_buffer_pool=s.get('hq_buffer_pool',{}),dma_read_buffer_pool=s.get('dma_read_buffer_pool',{}),dma_decode_buffer_pool=s.get('dma_decode_buffer_pool',{}),dma_read_backend=s.get('dma_read_backend','read'),
                                   invalid_last_bytes=s.get('invalid_last_bytes'),dma_dispatch_ms=s.get('dma_dispatch_ms'),
                                   preview_skipped=s.get('preview_skipped',0),
                                   decode_backend=s.get('decode_backend','unknown'),
                                   mode_applied=s.get('mode_applied',0),
                                   transition_discarded=s.get('transition_discarded',0),
                                   switch_to_frame_ms=s.get('switch_to_frame_ms'),
                                   control_write_ms=s.get('control_write_ms'),
                                   source_discontinuities=s.get('source_discontinuities',0),
                                   calibrated=c in self.calibrations,
                                   calibration_session=self.calibration_sessions[c].status() if c in self.calibration_sessions else None)
                candidates=[self.network[c].status(),self.client_network[c].status(),self.fast_client_network[c].status()]
                channels[c]['transport']=max(candidates,key=lambda row:row.get('delivery_at',0))
                channels[c]['fpga_codec']=s.get('fpga_codec',{})
            hdmi=dict(self.hdmi_stats,source_width=self.capture_profile.width,source_height=self.capture_profile.height,source_fps=self.capture_profile.fps,monitor_width=1920,monitor_height=1080)
            if now-hdmi.get('last_submit_at',0)>2:hdmi.update(video_fps=0.,submit_fps=0.)
            return json.loads(json.dumps(dict(schema='dual-v1',software_revision=SOFTWARE_REVISION, config=self.config, channels=channels,capture=self.capture_controller.status() if self.capture_controller else dict(state='running',current=self.config['capture'],profiles=['1080p60'],switchable=False,reason='当前源不支持采集切换',generation=self.capture_generation),storage=dict(sd=sd),hdmi=hdmi,inference_budget=dict(getattr(self,'inference_budget',{})),
                                              detection_classes=[dict(id=i,name=n,label=PCB_LABELS[i]) for i,n in enumerate(PCB_CLASSES)],
                                              runtime_memory=dict(self.memory),inference_worker=inference_worker,runtime_scheduling=dict(gil_switch_ms=round(sys.getswitchinterval()*1000,3),inference_parent_cpu_affinity=getattr(self,'inference_parent_cpu_affinity',None)),capabilities=dict(hdmi=self.hdmi_available,detect=self.model is not None,preprocess=self.pre_control is not None,fpga_codec=bool(self.pre_control and getattr(self.pre_control,'codec_enabled',False)),snapshots=True,logs=True,adaptive_transport=True,browser_preview=True,browser_push=True,defect_crops=True))))

    def defect_snapshot(self,c):
        with self.lock:
            s=self.channels[c]
            if not self.config['detect'][c]:raise ValueError('请先开启本路缺陷检测')
            if (s['frame'] is None or time.monotonic()-s['at']>=2 or s.get('mode_applied',0)
                    or s['infer_error'] or s['processed_reference'] is None
                    or time.monotonic()-s.get('processed_source_at',0)>=.8):
                raise ValueError('等待本路新的有效高清检测结果后重试')
            detections=[dict(d,box=list(d['box'])) for d in s['detections'] if type(d.get('class_id')) is int and 0<=d['class_id']<3]
            if not detections:raise ValueError('当前检测结果没有缺陷框')
            detections=sorted(detections,key=lambda d:d['score'],reverse=True)[:16]
            image=s['processed_reference'].copy();seq=s['processed_seq']
        ok,png=cv2.imencode('.png',image)
        if not ok:raise ValueError('缺陷高清截图编码失败')
        return dict(camera=c,seq=seq,width=image.shape[1],height=image.shape[0],detections=detections,png=base64.b64encode(png).decode())

    def frame(self, channel, raw=False, after_seq=None):
        with self.lock:
            s = self.channels[channel]
            if s['frame'] is None or time.monotonic()-s['at'] > 2:
                raise ValueError('camera %s has no fresh frame' % channel)
            # Never overlay an old inference result on a different raw frame.
            f = s['frame']; seq = s.get('preview_seq',s['seq']);annotations=None;epoch=s['detect_epoch']
            if not raw and self.config['detect'][channel] and s['processed'] is not None:
                if time.monotonic()-s.get('processed_source_at',0)<=.4:
                    f = s['processed']; seq = s['processed_seq'];annotations=s.get('processed_annotation')
            if seq==after_seq:return None,seq
        return self.result_image(channel,f,seq,epoch,annotations).copy(),seq


def capture(hub, channel, source, demo=False):
    cap = None
    try:
        if not demo:
            cap = cv2.VideoCapture(source, cv2.CAP_V4L2)
            if not cap.isOpened(): raise RuntimeError('capture device failed: '+source)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH,1920); cap.set(cv2.CAP_PROP_FRAME_HEIGHT,1080)
            cap.set(cv2.CAP_PROP_BUFFERSIZE,2)
        index = 0
        while not hub.stop.is_set():
            if demo:
                frame = np.full((540,960,3),(35,65,35) if channel=='A' else (65,35,35),np.uint8)
                cv2.putText(frame,'%s DEMO %d'%(channel,index),(80,260),0,2,(240,240,240),3)
                hub.stop.wait(.05); index += 1
            else:
                ok, frame = cap.read()
                if not ok: raise RuntimeError('capture read failed')
            hub.publish(channel,frame)
    except Exception as exc:
        with hub.lock: hub.channels[channel]['error'] = str(exc)
    finally:
        if cap is not None: cap.release()


from inference_model import Detector
from inference_process import ProcessDetector

def inference_video_metrics(hub):
    with hub.lock:
        now=time.monotonic()
        nominal=hub.capture_profile.fps
        sources={c:(dict(hub.config['transport'][c]),hub.channels[c].get('hq_fps',0),now-hub.channels[c]['at']<2) for c in hub.capture_channels}
    active=False;speeds=[]
    for c in sources:
        cfg,hq,fresh=sources[c]
        if cfg['mode']!='lossy' or cfg['fps']<nominal*.95:continue
        for encoder in (hub.client_network[c],hub.fast_client_network[c]):
            with encoder.lock:
                demanded=any(now-at<1.5 and fps>=nominal*.95 for at,fps in encoder.demands.values())
                stats=dict(encoder.stats)
            if not demanded:continue
            active=True;speeds.append(hq if fresh else 0.)
            if now-stats.get('delivery_at',0)<2:
                speeds.append(stats.get('delivered_fps',0.))
                # Zero painting does not diagnose available board processing.
                if stats.get('client_preview_fps',0)>0:speeds.append(stats['client_preview_fps'])
            else:speeds.append(0.)
    return active,speeds

def infer_loop(hub):
    if hasattr(os,'sched_getaffinity'):
        from cpu_layout import choose_cpus
        allowed=os.sched_getaffinity(0);preferred=choose_cpus('inference_parent',allowed)
        try:
            if preferred and preferred!=allowed:os.sched_setaffinity(0,preferred)
        except OSError:pass
        with hub.lock:hub.inference_parent_cpu_affinity=sorted(os.sched_getaffinity(0))
    from inference_budget import InferenceBudget
    budget=InferenceBudget(enabled=os.environ.get('PCB_INFER_VIDEO_PRIORITY')!='0')
    seen = dict.fromkeys(CHANNELS,None)
    seen_hq = dict.fromkeys(CHANNELS,None)
    while not hub.stop.is_set():
        now=time.monotonic()
        if now-budget.last_observe>=.5:
            active,speeds=inference_video_metrics(hub);budget.observe(now,active,speeds,nominal=hub.capture_profile.fps)
            with hub.lock:hub.inference_budget=budget.status()
        worked=False
        for c in CHANNELS:  # one model, fair A/B scheduling, no unbounded frame queue
            with hub.lock:eligible=hub.config['detect'][c] and hub.config['pre'][c]==0
            if not eligible:continue
            # Wait BEFORE taking the frame lease. At this rate each route is
            # still visited fairly, always sampling the freshest HQ image.
            if hub.stop.wait(budget.delay(time.monotonic())):return
            with hub.lock:
                s=hub.channels[c]
                token=(s['detect_epoch'],s['seq'])
                if (not hub.config['detect'][c] or (not s.get('raw_packet') and token==seen[c]) or s['frame'] is None
                    or s.get('mode_applied',0)!=0 or time.monotonic()-s['at']>=2):continue
                packet=s.get('raw_packet');params=dict(s.get('raw_parameters',{}))
                if 'raw_packet' in s and not packet:continue
                frame=None if packet else s['frame'].copy()
                seq=s['seq'];epoch=s['detect_epoch'];source_at=s['at'];seen[c]=token
            try:
                decode_started=time.monotonic()
                if packet:
                    frame,seq,source_at,mode=hub.hq_snapshot(c)
                    if mode!=0:continue
                    token=(epoch,seq)
                    if seen_hq[c]==token:continue
                    seen_hq[c]=token
                decoded_ms=(time.monotonic()-decode_started)*1000
                with hub.lock:
                    if hub.channels[c]['detect_epoch']!=epoch or not hub.config['detect'][c]:continue
                worked=True;t=time.monotonic();budget.started(t);result=hub.model.run(frame)
                image=result['image'] if isinstance(result,dict) else result
                detections=result.get('detections',[]) if isinstance(result,dict) else []
                timings=result.get('timings',{}) if isinstance(result,dict) else {}
                now=time.monotonic()
                with hub.lock:
                    s=hub.channels[c]
                    if not hub.config['detect'][c] or s['detect_epoch']!=epoch or hub.config['pre'][c]!=0:continue
                    interval=now-s.get('infer_at',now)
                    s.update(processed=image,processed_reference=frame,processed_seq=seq,processed_source_at=source_at,detections=detections,
                             processed_annotation=result.get('annotation_data') if isinstance(result,dict) and result.get('deferred_annotation') else None,
                             inference_timings=timings,infer_completed=s.get('infer_completed',0)+1,
                             infer_ms=round((now-t)*1000,1),infer_at=now,infer_error='',
                             hq_decode_ms=s.get('hq_shared_decode_ms',round(decoded_ms,2)),hq_wait_ms=round(decoded_ms,2),inference_source_width=frame.shape[1],inference_source_height=frame.shape[0],
                             infer_fps=1/interval if interval>0 else 0.)
                    if isinstance(result,dict) and result.get('tracking_reference_png'):
                        hub.client_references[c]=((epoch,seq),result['tracking_reference_png'])
            except Exception as exc:
                with hub.lock:
                    if hub.channels[c]['detect_epoch']==epoch:
                        hub.channels[c].update(infer_error=str(exc),processed=None,detections=[])
        if not worked:hub.stop.wait(.005)


def push_loop(hub, channel):
    last=0
    while not hub.stop.is_set():
        with hub.lock: cfg=json.loads(json.dumps(hub.config))
        if cfg['push'][channel]:
            try:
                packet=hub.network[channel].get(last,consumer='push',max_fps=cfg['push_fps'])
                if packet is not None:
                    req=Request(cfg['push_url'],data=packet.data,headers={'Content-Type':'image/'+packet.codec,
                                'X-Camera-ID':channel,'X-Frame-ID':str(packet.id),'X-Timestamp':str(time.time())},method='POST')
                    with urlopen(req,timeout=3) as response: response.read(1024)
                    with hub.lock:
                        hub.channels[channel]['push_ok']+=1;hub.channels[channel]['push_error']=''
                    last=packet.id
            except Exception as exc:
                with hub.lock: hub.channels[channel]['push_error']=str(exc)
        hub.stop.wait(1/cfg['push_fps'])


def placeholder(text):
    f=np.zeros((540,960,3),np.uint8)
    cv2.putText(f,text,(40,270),0,1,(190,190,190),2)
    return f


def hdmi_frame(hub):
    with hub.lock:mode=hub.config['hdmi']
    if mode=='off': return placeholder('HDMI OFF')
    frames=[]
    for c in (CHANNELS if mode=='split' else (mode,)):
        f=hub.hdmi_source(c)['image']
        if f is None:f=placeholder(c+' NO SIGNAL')
        frames.append(cv2.resize(f,(960,540)) if mode=='split' else f)
    return np.hstack(frames) if mode=='split' else frames[0]


from hdmi_output import HdmiWindow


def monitor_events(hub):
    previous={};sd_previous=None;due=time.monotonic()+10
    names={'error':'采集','infer_error':'缺陷检测','push_error':'远程推送','hq_error':'高清解码'}
    while not hub.stop.is_set():
        with hub.lock:
            now=time.monotonic()
            states={c:dict(online=s['frame'] is not None and now-s['at']<2,fps=s['fps'],infer_fps=s['infer_fps'],
                           **{key:s.get(key,'') for key in names}) for c,s in hub.channels.items()}
        for c,state in states.items():
            old=previous.get(c,{})
            if state['online']!=old.get('online'):
                hub.events.emit('相机在线' if state['online'] else '相机无新鲜图像','INFO' if state['online'] else 'WARN',c)
            for key,title in names.items():
                if state[key]!=old.get(key,''):
                    hub.events.emit(title+'错误：'+state[key] if state[key] else title+'已恢复','ERROR' if state[key] else 'INFO',c)
        previous=states
        sd=hub.snapshot_store.status();signature=tuple(sd.get(k) for k in ('mounted','writable','path','error'))
        if signature!=sd_previous:
            hub.events.emit('SD卡就绪：'+sd.get('snapshot_root','') if sd.get('mounted') and sd.get('writable') else 'SD卡：'+sd.get('error','不可写'),
                            'INFO' if sd.get('mounted') and sd.get('writable') else 'WARN')
            sd_previous=signature
        if time.monotonic()>=due:
            hub.events.emit(' | '.join('%s 采集%.1f fps / 检测%.1f fps'%(c,s['fps'] if s['online'] else 0,s['infer_fps']) for c,s in states.items()))
            with hub.lock:hdmi=dict(hub.hdmi_stats)
            if hdmi.get('mode')!='off':hub.events.emit('HDMI %s · %s · 实际视频%.1f fps · 提交%.1f fps · 组合%.1f ms · 显示提交%.1f ms'%(hdmi.get('mode'),hdmi.get('backend'),hdmi.get('video_fps',0),hdmi.get('submit_fps',0),hdmi.get('compose_ms',0),hdmi.get('present_ms',0)))
            due=time.monotonic()+10
        hub.stop.wait(.5)


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def handle(self):
        try:super().handle()
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):self.close_connection=True
    def setup(self):
        super().setup();self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)

    def log_message(self,*args): pass

    def send(self, code, data, mime='application/json'):
        hub=self.server.hub
        if 200<=code<300 and hasattr(self,'result_generation'):
            with hub.lock:
                if hub.capture_switching or self.result_generation!=hub.capture_generation:
                    code=409;data=dict(error='采集模式已改变，本次旧画面操作已取消');mime='application/json'
        if not isinstance(data,bytes): data=json.dumps(data).encode()
        try:
            self.send_response(code);self.send_header('Content-Type',mime)
            self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store')
            self.end_headers();self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):self.close_connection=True

    def stream(self, channel):
        hub=self.server.hub
        self.close_connection=True
        self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        self.connection.settimeout(3)
        self.send_response(200)
        self.send_header('Content-Type','multipart/x-mixed-replace; boundary=frame')
        self.send_header('Cache-Control','no-store')
        self.send_header('Connection','close')
        self.end_headers()
        last=-1;due=0
        try:
            while not hub.stop.is_set():
                if time.monotonic()<due:
                    hub.stop.wait(.002);continue
                with hub.lock:
                    if hub.config['preview'][channel]=='fast':hub.compat_requests[channel]=time.monotonic()
                try:frame,seq=hub.display_frame(channel,after_seq=last)
                except ValueError:
                    # Close stale stream so the GUI can report no signal and reconnect.
                    break
                if frame is None:
                    hub.stop.wait(.002);continue
                ok,jpg=cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,94 if frame.shape[1]>=1920 else 80])
                if not ok:continue
                data=jpg.tobytes()
                self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(data)).encode()+b'\r\n\r\n'+data+b'\r\n')
                self.wfile.flush();last=seq;due=time.monotonic()+1/60
        except (OSError,ConnectionError):pass

    def adaptive_stream(self,c,client_overlay=False,fast_lossless=False):
        self.close_connection=True
        hub=self.server.hub;identifier=uuid.uuid4().hex
        window=AckWindow(2 if client_overlay else 1)
        state=dict(camera=c,pending=0,event=threading.Event(),lock=threading.Lock(),window=window)
        with hub.lock:hub.net_sessions[identifier]=state
        # A full lossless frame is ~3MB. Keep enough bytes in flight for GbE
        # while the application ACK window still limits us to two frames.
        self.connection.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,1024*1024)
        self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1);self.connection.settimeout(3)
        self.send_response(200);self.send_header('Content-Type','multipart/x-mixed-replace; boundary=frame')
        self.send_header('X-Client-ID',identifier);self.send_header('X-Decoder-Metadata','1')
        self.send_header('X-Ack-Window',str(window.limit))
        if client_overlay:self.send_header('X-Client-Overlay','1')
        self.send_header('Cache-Control','no-store');self.send_header('Connection','close');self.end_headers()
        encoder=(hub.fast_client_network[c] if fast_lossless else hub.client_network[c]) if client_overlay else hub.network[c]
        last=None;reference_key=None;deliveries=deque(maxlen=240);heartbeat_at=time.monotonic()
        try:
            while not hub.stop.is_set():
                for row in window.completed():
                    now=row['ack_at'];elapsed=(now-row['at'])*1000
                    encoder.ack(elapsed,window=window.limit)
                    deliveries.append((now,row['nbytes']))
                    while len(deliveries)>2 and deliveries[0][0]<now-2:deliveries.popleft()
                    span=now-deliveries[0][0]
                    with encoder.lock:
                        encoder.stats.update(delivery_at=now,delivery_ms=round(elapsed,2),source_ack_ms=round((now-row['source_at'])*1000,2),
                            delivered_fps=round((len(deliveries)-1)/span,2) if span else 0.,
                            wire_mbps=round(sum(size for _,size in list(deliveries)[1:])*8/span/1e6,2) if span else 0.,
                            client_decode_ms=row.get('decode_ms',0),client_decoder=row.get('decoder','legacy'),client_overlay_ms=row.get('overlay_ms',0),
                            client_preview_fps=row.get('preview_fps',0),ack_window=window.limit,ack_peak_inflight=window.peak)
                if window.expired(time.monotonic()):break
                if window.full():window.wait();continue
                packet=encoder.get(last,consumer=identifier,timeout=.25)
                if packet is None:
                    with hub.lock:fresh=time.monotonic()-hub.channels[c]['at']<2
                    if not fresh:break
                    if time.monotonic()-heartbeat_at>=1:
                        self.wfile.write(b'--frame\r\nContent-Type: application/x-heartbeat\r\nContent-Length: 0\r\n\r\n');self.wfile.flush();heartbeat_at=time.monotonic()
                    continue
                started=time.monotonic()
                window.reserve(packet.id,started,packet.source_at,len(packet.data))
                with state['lock']:state['event'].clear();state['pending']=packet.id
                mime='application/x-pcb-zstd' if packet.codec=='pcz' else 'image/'+packet.codec
                header=('--frame\r\nContent-Type: %s\r\nContent-Length: %d\r\nX-Frame-ID: %d\r\nX-Source-ID: %d\r\n'%
                        (mime,len(packet.data),packet.id,packet.token[0])).encode()
                if client_overlay:
                    meta,reference_key=hub.client_overlay_metadata(c,packet,reference_key)
                    header+=b'X-Overlay: '+base64.b64encode(json.dumps(meta,separators=(',',':')).encode())+b'\r\n'
                    header+=('X-Capture-Generation: %d\r\n'%encoder.capture_generation).encode()
                send_started=time.monotonic()
                self.wfile.write(header+b'\r\n');self.wfile.write(packet.data);self.wfile.write(b'\r\n');self.wfile.flush()
                with encoder.lock:encoder.stats['socket_send_ms']=round((time.monotonic()-send_started)*1000,2)
                last=packet.id
        except (OSError,ConnectionError):pass
        finally:
            with hub.lock:hub.net_sessions.pop(identifier,None)

    def dispatch(self, post=False):
        if hasattr(self,'result_generation'):del self.result_generation
        hub=self.server.hub
        parts=urlparse(self.path).path.strip('/').split('/')
        try:
            body=None
            if post:
                length=int(self.headers.get('Content-Length',0))
                if not 0<length<=8192: raise ValueError('invalid request size')
                raw_body=self.rfile.read(length)
                if len(raw_body)!=length:self.close_connection=True;return
                body=json.loads(raw_body)
            if not post and parts in ([''],['m'],['index.html']):
                return self.send(200,browser_asset('index.html'),'text/html; charset=utf-8')
            if not post and parts==['favicon.ico']:return self.send(204,b'')
            if not post and len(parts)==2 and parts[0]=='web' and parts[1] in ('app.js','style.css'):
                return self.send(200,browser_asset(parts[1]),'text/javascript; charset=utf-8' if parts[1].endswith('.js') else 'text/css; charset=utf-8')
            if not post and parts==['api','v2','net']:
                return self.send(200,network_info(self.server.server_port))
            if not post and len(parts)==5 and parts[:3]==['api','v2','browser'] and parts[4]=='frame':
                query=parse_qs(urlparse(self.path).query,keep_blank_values=True)
                if set(query)-{'profile','after'} or any(len(x)!=1 for x in query.values()):raise ValueError('invalid browser frame query')
                profile=query.get('profile',['smooth'])[0];after=int(query.get('after',['0'])[0]);c=parts[3]
                if c not in CHANNELS or profile not in BROWSER_PROFILES or not 0<=after<2**63:raise ValueError('invalid browser camera/profile/after')
                try:frame=hub.browser.get(c,profile,after)
                except ValueError as exc:return self.send(503,dict(error=str(exc)))
                if frame is None:return self.send(204,b'')
                meta=dict(frame.meta);meta['remaining_ms']=max(0,(meta.pop('expires_at',0)-time.monotonic())*1000)
                self.send_response(200);self.send_header('Content-Type','image/'+frame.codec)
                self.send_header('Content-Length',str(len(frame.data)));self.send_header('Cache-Control','no-store')
                self.send_header('X-Browser-ID',str(frame.id));self.send_header('X-Browser-Meta',base64.b64encode(json.dumps(meta,separators=(',',':')).encode()).decode())
                self.end_headers();self.wfile.write(frame.data);return
            if parts==['api','v2','status'] and not post: return self.send(200,hub.status())
            if parts==['api','v2','logs'] and not post:
                query=parse_qs(urlparse(self.path).query)
                if set(query)-{'after','session'} or any(len(v)!=1 for v in query.values()):raise ValueError('invalid log query')
                after=int(query.get('after',['0'])[0]);session=query.get('session',[None])[0]
                if session is not None and len(session)>64:raise ValueError('invalid log session')
                return self.send(200,hub.events.read(after,session))
            if parts==['api','v2','storage'] and not post:return self.send(200,dict(sd=hub.snapshot_store.status()))
            if parts==['api','v2','config'] and post:
                return self.send(200,hub.configure(body))
            if len(parts)==6 and parts[:3]==['api','v2','camera'] and parts[3] in CHANNELS and parts[4]=='calibration' and post:
                from board_calibration import CalibrationSession,save_calibration,annotated
                c=parts[3];action=parts[5]
                if action not in ('reset','sample','lens','plane','import'):raise ValueError('unknown calibration action')
                if not hub.calibration_locks[c].acquire(blocking=False):return self.send(409,{'error':'本路标定正在处理，请稍候'})
                try:
                    with hub.lock:
                        if hub.capture_switching or c not in hub.capture_channels:raise ValueError('本路已停止或采集模式正在切换')
                        calibration_generation=hub.capture_generation;folder=hub.calibration_folder() if hub.calibration_dir else None
                    def ensure_current():
                        if hub.capture_switching or calibration_generation!=hub.capture_generation:raise ValueError('采集模式已改变，请重新开始本档位标定')
                    png=None
                    if action=='reset':
                        allowed={'columns','rows','square_mm','board_thickness_mm'}
                        if not isinstance(body,dict) or set(body)-allowed:raise ValueError('invalid calibration settings')
                        session=CalibrationSession(c,**body)
                        with hub.lock:
                            ensure_current();hub.calibration_sessions[c]=session
                    elif action=='import':
                        if not hub.calibration_dir:raise ValueError('未配置标定保存目录')
                        path=save_calibration(body,c,folder)
                        with hub.lock:
                            ensure_current();hub.calibrations[c]=path
                    else:
                        session=hub.calibration_sessions.get(c)
                        if session is None:raise ValueError('请先开始棋盘标定')
                        if action=='lens':session.solve_lens()
                        else:
                            image,seq=hub.measurement_image(c)
                            if action=='sample':corners=session.sample(image,seq)
                            else:
                                if not hub.calibration_dir:raise ValueError('未配置标定保存目录')
                                data,corners=session.plane(image)
                                path=save_calibration(data,c,folder)
                                with hub.lock:
                                    ensure_current();hub.calibrations[c]=path
                            png=annotated(image,corners,session.pattern)
                    with hub.lock:
                        ensure_current();session=hub.calibration_sessions.get(c)
                    descriptions={'reset':'开始棋盘标定','sample':'已采集棋盘姿态','lens':'镜头标定通过','plane':'平面标定已保存','import':'标定文件已导入'}
                    detail=('，%d张'%len(session.points)) if session and action=='sample' else ('，RMS %.3f px'%session.rms if session and action=='lens' else '')
                    hub.events.emit(descriptions[action]+detail,camera=c)
                    return self.send(200,dict(camera=c,action=action,calibrated=c in hub.calibrations,
                                               session=session.status() if session else None,png=png))
                finally:hub.calibration_locks[c].release()
            if len(parts)==5 and parts[:3]==['api','v2','camera'] and parts[3] in CHANNELS:
                c=parts[3]; action=parts[4]
                if action in ('snapshot','measure','defects','hq.jpg','snap.jpg'):
                    with hub.lock:
                        if hub.capture_switching or c not in hub.capture_channels:raise ValueError('相机已停止或采集模式切换中')
                        self.result_generation=hub.capture_generation
                if action=='defects' and post:
                    if body!={}:raise ValueError('缺陷截图请求应为空对象')
                    if not hub.snapshot_locks[c].acquire(blocking=False):return self.send(409,{'error':'本路正在获取截图，请稍候'})
                    try:return self.send(200,hub.defect_snapshot(c))
                    finally:hub.snapshot_locks[c].release()
                if action=='stream' and not post:
                    query=parse_qs(urlparse(self.path).query)
                    return self.adaptive_stream(c,query.get('overlay')==['client'],query.get('lossless')==['fast'])
                if action=='transport_ack' and post:
                    if not isinstance(body,dict) or set(body)-{'client_id','frame_id','decode_ms','decoder','overlay_ms','preview_fps'}:raise ValueError('invalid stream ACK')
                    if type(body.get('frame_id'))is not int or type(body.get('decode_ms')) not in (int,float) or not 0<=body['decode_ms']<=30000:raise ValueError('invalid stream ACK timing')
                    if 'decoder' in body and body['decoder'] not in ('opencv','qt'):raise ValueError('invalid preview decoder')
                    if 'overlay_ms' in body and (type(body['overlay_ms']) not in (int,float) or not 0<=body['overlay_ms']<=30000):raise ValueError('invalid overlay timing')
                    if 'preview_fps' in body and (type(body['preview_fps']) not in (int,float) or not 0<=body['preview_fps']<=240):raise ValueError('invalid client preview fps')
                    with hub.lock:state=hub.net_sessions.get(body.get('client_id'))
                    accepted=False
                    if state and state['camera']==c:
                        if 'window' in state:
                            accepted=state['window'].ack(body['frame_id'],time.monotonic(),decode_ms=round(body['decode_ms'],2),
                                decoder=body.get('decoder','legacy'),overlay_ms=round(body.get('overlay_ms',0),2),preview_fps=round(body.get('preview_fps',0),2))
                            if accepted:
                                with state['lock']:
                                    state.update(decode_ms=round(body['decode_ms'],2),decoder=body.get('decoder','legacy'),
                                                 overlay_ms=round(body.get('overlay_ms',0),2),preview_fps=round(body.get('preview_fps',0),2))
                                    state['event'].set()
                        else:
                            with state['lock']:
                                accepted=state['pending']==body['frame_id']
                                if accepted:
                                    state['decode_ms']=round(body['decode_ms'],2);state['decoder']=body.get('decoder','legacy')
                                    state['overlay_ms']=round(body.get('overlay_ms',0),2);state['preview_fps']=round(body.get('preview_fps',0),2);state['event'].set()
                    if urlparse(self.path).query=='quiet=1':return self.send(204,b'')
                    return self.send(200,dict(accepted=accepted))
                if action=='snapshot' and post:
                    if (not isinstance(body,dict) or set(body)-{'sd','pc','client_time'}
                        or any(type(body.get(k)) is not bool for k in ('sd','pc')) or not (body['sd'] or body['pc'])):
                        raise ValueError('请至少选择SD卡或上位机一个保存位置')
                    if not hub.snapshot_locks[c].acquire(blocking=False):return self.send(409,{'error':'本路正在截图保存，请稍候'})
                    try:
                        frame,seq,kind,mode,counts=hub.snapshot_image(c,include_counts=True)
                        day,name=snapshot_name(c,seq,counts,body.get('client_time'))
                        ok,data=cv2.imencode('.png',frame,[cv2.IMWRITE_PNG_COMPRESSION,3])
                        if not ok:raise ValueError('PNG截图生成失败')
                        png=data.tobytes();sd=dict(requested=body['sd'],ok=False,path='',error='')
                        if body['sd']:
                            try:sd.update(ok=True,path=hub.snapshot_store.save(day,name,png))
                            except StorageError as exc:sd['error']=str(exc)
                            hub.events.emit('SD卡截图已保存：'+sd['path'] if sd['ok'] else sd['error'],'INFO' if sd['ok'] else 'ERROR',c)
                        if body['pc']:hub.events.emit('高清截图已编码并回传上位机：%d×%d，源帧%d'%(frame.shape[1],frame.shape[0],seq),camera=c)
                        import hashlib
                        return self.send(200,dict(camera=c,seq=seq,kind=kind,mode=mode,width=frame.shape[1],height=frame.shape[0],
                            date=day,filename=name,defect_counts=counts,sha256=hashlib.sha256(png).hexdigest(),sd=sd,
                            png=base64.b64encode(png).decode() if body['pc'] else None))
                    finally:hub.snapshot_locks[c].release()
                if action=='raw_stats' and not post:
                    from vid008_pcie import FRAME_BYTES,ROWS,STRIDE,validate_packet
                    with hub.lock:
                        s=hub.channels[c]
                        if not s.get('raw_packet') or time.monotonic()-s['at']>2:raise ValueError('no fresh RAW frame in current mode')
                        raw=s['raw_packet'];params=dict(s['raw_parameters']);seq=s['seq'];geometry=hub.capture_profile
                    validate_packet(raw,CHANNELS.index(c),geometry)
                    rows=np.frombuffer(raw,np.uint8,count=geometry.raw_bytes).reshape(geometry.raw_height,geometry.stride)
                    groups=rows[geometry.crop_y:geometry.crop_y+geometry.height:8,:geometry.line_bytes].reshape(geometry.height//8,geometry.raw_width//4,5)[:,geometry.crop_x//4:(geometry.crop_x+geometry.width)//4:8]
                    samples=groups[:,:,:4].astype(np.uint16)<<2
                    for i in range(4):samples[:,:,i]|=(groups[:,:,4]>>(2*i))&3
                    return self.send(200,dict(camera=c,seq=seq,sampled_pixels=samples.size,
                                             raw_mean=float(samples.mean()),raw_p95=float(np.percentile(samples,95)),
                                             raw_saturated_pct=float((samples>=1020).mean()*100),tone=params))
                if action=='hq.jpg' and not post:
                    frame,_=hub.display_frame(c,force_detail=True)
                    ok,jpg=cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,96])
                    if not ok:raise ValueError('JPEG failed')
                    return self.send(200,jpg.tobytes(),'image/jpeg')
                if action=='stream.mjpg' and not post:return self.stream(c)
                if action=='snap.jpg' and not post:
                    frame,_=hub.frame(c)
                    ok,jpg=cv2.imencode('.jpg',cv2.resize(frame,(960,540)),[cv2.IMWRITE_JPEG_QUALITY,80])
                    if not ok: raise ValueError('JPEG failed')
                    return self.send(200,jpg.tobytes(),'image/jpeg')
                if action=='measure' and post:
                    if c not in hub.calibrations: raise ValueError('camera '+c+' requires its own calibration')
                    with hub.lock:
                        calibration_path=hub.calibrations[c]
                        if hub.channels[c]['measure_busy']: return self.send(409,{'error':'measurement busy'})
                        hub.channels[c]['measure_busy']=True
                    try:
                        from board_outline import measure_outline
                        frame,seq=hub.measurement_image(c)
                        result,png=measure_outline(frame,calibration_path=calibration_path)
                        hub.events.emit('板框测量完成：%.2f × %.2f mm（初测）'%(result['long_mm'],result['short_mm']),camera=c)
                        return self.send(200,dict(camera=c,seq=seq,result=result,png=base64.b64encode(png).decode()))
                    finally:
                        with hub.lock: hub.channels[c]['measure_busy']=False
            self.send(404,{'error':'not found'})
        except (ValueError,KeyError,TypeError) as exc:
            if post:hub.events.emit('操作失败 '+urlparse(self.path).path+'：'+str(exc),'ERROR')
            self.send(400,{'error':str(exc)})
        except Exception as exc:
            if post:hub.events.emit('操作异常 '+urlparse(self.path).path+'：'+str(exc),'ERROR')
            self.send(500,{'error':str(exc)})

    def do_GET(self): self.dispatch()
    def do_POST(self): self.dispatch(True)


def main():
    from runtime_memory import enable_trace,MemoryMonitor
    enable_trace()
    # Native codecs release the GIL frequently. A 5ms Python handoff quantum
    # can consume much of a 16.7ms video period when inference is also active.
    # This changes only this service interpreter, not OS priorities/affinities.
    sys.setswitchinterval(.0005)
    # Several independent camera/display/inference jobs already run concurrently.
    # Avoid creating an additional OpenCV worker pool for each small operation.
    cv2.setNumThreads(1)
    p=argparse.ArgumentParser()
    p.add_argument('--demo',action='store_true');p.add_argument('--source-a');p.add_argument('--source-b')
    p.add_argument('--pcie',action='store_true',help='VID008 AB build 0x56303868 RAW10 DMA')
    p.add_argument('--port',type=int,default=8001);p.add_argument('--bind',default='0.0.0.0')
    p.add_argument('--calib-a');p.add_argument('--calib-b');p.add_argument('--model');p.add_argument('--classes')
    p.add_argument('--calibration-dir',default=str(Path(__file__).with_name('calibrations')))
    p.add_argument('--hdmi-window',action='store_true')
    p.add_argument('--hdmi-x',type=int,default=0);p.add_argument('--hdmi-y',type=int,default=0)
    p.add_argument('--hdmi-width',type=int,default=1920);p.add_argument('--hdmi-height',type=int,default=1080)
    args=p.parse_args()
    if args.pcie and (args.demo or args.source_a or args.source_b):p.error('--pcie is exclusive with demo/V4L2 sources')
    if not args.demo and not args.pcie and (not args.source_a or not args.source_b or args.source_a==args.source_b):
        p.error('supply two confirmed, distinct V4L2 device paths; no guessed video node defaults')
    if args.model and not args.classes: p.error('--classes required with --model')
    if args.demo and args.model: p.error('--demo does not load NPU')
    if args.hdmi_width<=0 or args.hdmi_height<=0:p.error('HDMI dimensions must be positive')
    if args.hdmi_window:
        import os
        if not os.environ.get('DISPLAY'):p.error('DISPLAY required; use start_dual_hdmi.sh in the X11 desktop environment')
    calibrations={c:v for c,v in [('A',args.calib_a),('B',args.calib_b)] if v}
    from board_calibration import validate_calibration
    for c in CHANNELS:
        saved=Path(args.calibration_dir)/('camera_'+c+'.json')
        if c not in calibrations and saved.is_file():
            validate_calibration(json.loads(saved.read_text(encoding='utf-8')),c);calibrations[c]=str(saved)
    for path in calibrations.values():
        if not Path(path).is_file(): p.error('calibration missing: '+path)
    hub=Hub(calibrations,ProcessDetector(args.model,args.classes) if args.model else None,args.hdmi_window,args.calibration_dir)
    hub.events.emit('HTTP服务端口%d；三类模型%s；已加载标定%s'%(args.port,'已启用' if args.model else '未启用',','.join(calibrations) or '无'))
    hdmi=HdmiWindow(args.hdmi_width,args.hdmi_height,args.hdmi_x,args.hdmi_y) if args.hdmi_window else None
    server=ThreadingHTTPServer((args.bind,args.port),Handler);server.daemon_threads=True;server.hub=hub
    addresses=network_info(args.port)
    hub.events.emit('浏览器预览：'+('，'.join(addresses['urls']) or '等待网络IPv4地址'))
    watcher=threading.Thread(target=monitor_events,args=(hub,),daemon=True);watcher.start();threads=[watcher]
    memory=threading.Thread(target=MemoryMonitor(hub).run,daemon=True);memory.start();threads.append(memory)
    if args.pcie:
        from vid008_pcie import PcieCapture
        from capture_control import CaptureController
        hub.capture_controller=CaptureController(hub,PcieCapture);threads.append(hub.capture_controller.start())
    for c,src in [('A',args.source_a),('B',args.source_b)]:
        jobs=[(push_loop,(hub,c))]
        if not args.pcie:jobs.append((capture,(hub,c,src,args.demo)))
        for fn,params in jobs:
            t=threading.Thread(target=fn,args=params,daemon=True);t.start();threads.append(t)
    t=threading.Thread(target=infer_loop,args=(hub,),daemon=True);t.start();threads.append(t)
    t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
    print('dual-v1 service port',args.port,'DEMO ONLY' if args.demo else 'hardware capture')
    try:
        while not hub.stop.is_set():
            presented=False
            if hdmi:
                try:presented=hdmi.update(hub)
                except (cv2.error,RuntimeError,OSError) as exc:
                    hdmi.close()
                    with hub.lock:hub.config['hdmi']='off';hub.hdmi_available=False
                    hub.events.emit('HDMI显示失败：'+str(exc),'ERROR')
                    print('HDMI window failed:',str(exc),flush=True)
            # EGL swap already waits for display cadence. Do not add another
            # 15 ms to every presented frame; idle polling remains bounded.
            hub.stop.wait(0 if presented else .002 if hdmi and hdmi.opened else .02)
    except KeyboardInterrupt: pass
    finally:
        hub.stop.set();server.shutdown();server.server_close()
        for t in threads: t.join(12 if args.pcie else 1)
        if hub.model: hub.model.net.release()
        if hdmi:hdmi.close()


if __name__=='__main__': main()
