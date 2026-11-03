"""Latest-frame HDMI presentation independent of inference completion rate."""
from collections import deque
import os,time
import cv2
from hdmi_overlay_worker import OverlayComposer
from hdmi_renderer import NativeRenderer,OpenCvRenderer
from hdmi_display_process import ProcessRenderer


class HdmiWindow:
    def __init__(self,width=1920,height=1080,x=0,y=0,renderer_factory=None,overlay_factory=None,max_fps=None):
        self.width,self.height,self.x,self.y=width,height,x,y
        self.renderer_factory=renderer_factory;self.overlay_factory=overlay_factory;self.renderer=None;self.opened=False;self.key=None;self.source_key=None;self.overlay=None
        self.video_key=None;self.video_times=deque(maxlen=240);self.submit_times=deque(maxlen=240)
        self.trackers={};self.submitted_frames=0;self.video_frames=0
        self.backend_error=''
        self.max_fps=max(1,min(60,float(max_fps if max_fps is not None else os.environ.get('PCB_HDMI_MAX_FPS','30'))))
        self.next_frame_at=0.;self.source_mode=None

    def close(self):
        if self.renderer is not None:
            try:self.renderer.close()
            except (cv2.error,RuntimeError,OSError):pass
        if self.overlay is not None:self.overlay.close()
        self.overlay=None;self.trackers={};self.source_key=None
        self.renderer=None;self.opened=False;self.key=None;self.video_key=None
        self.video_times.clear();self.submit_times.clear()
        self.next_frame_at=0.;self.source_mode=None

    def open(self,hub):
        if self.renderer_factory is not None:self.renderer=self.renderer_factory(self.width,self.height,self.x,self.y)
        else:
            try:
                if os.name!='posix':raise OSError('EGL board renderer unavailable on this platform')
                factory=NativeRenderer if os.environ.get('PCB_HDMI_DISPLAY')=='inline' else ProcessRenderer
                self.renderer=factory(self.width,self.height,self.x,self.y)
            except (OSError,RuntimeError) as exc:
                self.backend_error=str(exc);self.renderer=OpenCvRenderer(self.width,self.height,self.x,self.y)
                hub.events.emit('HDMI GPU不可用，使用软件回退：'+str(exc),'WARN')
        self.overlay=(self.overlay_factory or OverlayComposer)(self.width,self.height);self.trackers=self.overlay.trackers
        self.opened=True;hub.events.emit('HDMI显示后端：'+self.renderer.backend)

    @staticmethod
    def rate(times,now):
        while len(times)>2 and times[0]<now-2:times.popleft()
        return round((len(times)-1)/(times[-1]-times[0]),2) if len(times)>1 and now-times[-1]<2 and times[-1]>times[0] else 0.

    def update(self,hub):
        with hub.lock:mode=hub.config['hdmi']
        if mode=='off':
            if self.opened:self.close()
            with hub.lock:hub.hdmi_stats.update(mode='off',video_fps=0.,submit_fps=0.)
            return False
        if not self.opened:self.open(hub)
        # HDMI has its own consumer clock. Sleep without holding the GIL or
        # shared-state lock instead of repeatedly sampling full HQ metadata.
        now=time.monotonic()
        if self.source_mode==mode and self.next_frame_at>now:
            hub.stop.wait(min(1/self.max_fps,self.next_frame_at-now))
            if hub.stop.is_set():return False
            with hub.lock:
                if hub.config['hdmi']!=mode:return False
        now=time.monotonic();period=1/self.max_fps
        self.next_frame_at=self.next_frame_at+period if self.source_mode==mode and self.next_frame_at and now-self.next_frame_at<period else now+period
        self.source_mode=mode
        started=time.monotonic();channels=('A','B') if mode=='split' else (mode,)
        sources={c:hub.hdmi_source(c) for c in channels}
        source_key=(mode,tuple((c,s['seq'],s['mode'],s['detect_epoch'],s['active'],s['processed_seq'] if s['active'] else 0,s['image'] is not None) for c,s in sources.items()))
        if source_key!=self.source_key:self.overlay.submit(mode,sources);self.source_key=source_key
        results=self.overlay.latest(mode,sources)
        # Apply newly projected boxes with the next source frame. Extra swaps
        # for every tracker result can consume a vblank and skip fresh video.
        key=source_key
        submitted=False
        if key!=self.key:
            annotations={c:[] for c in channels};states={};errors={}
            for c,s in sources.items():
                states[c]='waiting' if s['detect'] else 'off'
                if c in results:
                    result=results[c];annotations[c]=result['annotations'];states[c]=result['state']
                    if result['error']:errors[c]=result['error']
            with hub.lock:
                if hub.config['hdmi']!=mode:return False
                for c,s in sources.items():
                    if hub.channels[c]['detect_epoch']!=s['detect_epoch'] or not hub.config['detect'][c]:
                        annotations[c]=[];states[c]='off'
            composed=time.monotonic()
            try:self.renderer.present(mode,sources,annotations)
            except (OSError,RuntimeError) as exc:
                if not isinstance(self.renderer,NativeRenderer):raise
                self.backend_error=str(exc);self.renderer.close();self.renderer=OpenCvRenderer(self.width,self.height,self.x,self.y)
                hub.events.emit('HDMI GPU提交失败，切换软件回退：'+str(exc),'WARN');self.renderer.present(mode,sources,annotations)
            now=time.monotonic();self.key=key;submitted=True;self.submitted_frames+=1;self.submit_times.append(now)
            video_key=(mode,tuple((c,s['seq'],s['mode'],s['image'] is not None) for c,s in sources.items()))
            if video_key!=self.video_key and any(s['image'] is not None for s in sources.values()):
                self.video_key=video_key;self.video_times.append(now);self.video_frames+=1
            with hub.lock:
                hub.hdmi_stats.update(mode=mode,backend=self.renderer.backend,backend_error=self.backend_error,
                    output_width=self.width,output_height=self.height,video_fps=self.rate(self.video_times,now),submit_fps=self.rate(self.submit_times,now),
                    target_fps=self.max_fps,
                    video_frames=self.video_frames,submitted_frames=self.submitted_frames,last_submit_at=now,
                    compose_ms=round((composed-started)*1000,2),present_ms=round((now-composed)*1000,2),
                    renderer_timings=getattr(self.renderer,'timings',{}),overlay=states,overlay_errors=errors,
                    overlay_box_count={c:len(annotations[c]) for c in channels},
                    overlay_pipeline=dict(pending_slots=self.overlay.peak_pending,replaced_jobs=self.overlay.replaced,target_fps=30,
                        backend=self.overlay.backend,cpu_pid=self.overlay.worker_pid,cpu_affinity=self.overlay.worker_affinity,cpu_rss_kb=self.overlay.process_rss_kb,
                        prepare_ms=self.overlay.prepare_ms,roundtrip_ms=self.overlay.roundtrip_ms,ipc_error=self.overlay.ipc_error,
                        diagnostics=dict(self.overlay.latest_diagnostics),
                        work_ms={c:r['work_ms'] for c,r in results.items()},age_ms={c:round((now-r['source_at'])*1000,2) for c,r in results.items()}),
                    source_age_ms={c:round((now-s['source_at'])*1000,2) if s['image'] is not None else None for c,s in sources.items()})
        if hasattr(self.renderer,'display_stats'):
            with hub.lock:hub.hdmi_stats.update(self.renderer.display_stats())
        if self.renderer.poll()==27:
            hub.configure({'hdmi':'off'});self.close()
            with hub.lock:hub.hdmi_stats.update(mode='off',video_fps=0.,submit_fps=0.)
        return submitted
