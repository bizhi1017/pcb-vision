"""Serial capture-mode transactions; hardware is opened only by the session factory."""
import copy,threading,time,uuid
from capture_profiles import selection,profile,DEFAULT

class StopEvent:
    def __init__(self,*events):self.events=events
    def is_set(self):return any(e.is_set() for e in self.events)
    def wait(self,timeout=None):
        deadline=None if timeout is None else time.monotonic()+timeout
        while not self.is_set():
            remaining=.02 if deadline is None else min(.02,deadline-time.monotonic())
            if remaining<=0:return False
            self.events[-1].wait(remaining)
        return True

class CaptureController:
    def __init__(self,hub,session_factory,startup_timeout=45):
        self.hub=hub;self.factory=session_factory;self.timeout=startup_timeout
        self.condition=threading.Condition();self.pending=None;self.session=None;self.session_thread=None;self.thread=None
        self.current=dict(profile=DEFAULT.id,channels=['A','B']);self.state='starting';self.error='';self.generation=0
        self.supported=['1080p60'];self.switchable=False;self.reason='等待采集能力';self.saved_1080=None
        self.session_id=uuid.uuid4().hex;self.last_failure=None
        self.last_probe=dict(build_id='0x00000000',dma_frame_capacity=0,complete_frames={})

    def status(self):
        with self.condition:
            session=self.session
            probe=dict(build_id='0x%08x'%getattr(session,'build',0),dma_frame_capacity=getattr(session,'driver_limit',0),complete_frames=dict(getattr(session,'complete',{}))) if session is not None else dict(self.last_probe)
            return dict(state=self.state,error=self.error,current=copy.deepcopy(self.current),requested=copy.deepcopy(self.pending),
                session_id=self.session_id,generation=self.generation,profiles=list(self.supported),switchable=self.switchable,reason=self.reason,
                geometry=self.hub.capture_profile.status(),last_failure=copy.deepcopy(self.last_failure),**probe)

    def start(self):
        self.thread=threading.Thread(target=self.run,name='capture-mode-controller',daemon=True);self.thread.start();return self.thread

    def request(self,value):
        target=selection(value)
        with self.condition:
            if self.state!='running':raise ValueError('采集模式正在切换或未就绪')
            if target==self.current:return self.status()
            if not self.switchable or target['profile'] not in self.supported:raise ValueError(self.reason or '当前位流或驱动不支持该采集模式')
            self.pending=target;self.state='switching';self.error='';self.condition.notify_all();return self.status()

    def _launch(self,target):
        self.hub.prepare_capture(target,self.generation)
        self.session=self.factory(self.hub,target)
        self.session_thread=threading.Thread(target=self.session.run,name='pcie-capture-session',daemon=True);self.session_thread.start()
        deadline=time.monotonic()+self.timeout
        while not self.hub.stop.is_set():
            if self.session.error:raise RuntimeError(self.session.error)
            if self.session.ready.is_set() and all(self.session.complete.get(c,0)>=3 for c in target['channels']):
                with self.condition:
                    self.switchable=self.session.switchable;self.supported=list(self.session.supported);self.reason=self.session.reason
                return
            if not self.session_thread.is_alive():raise RuntimeError(self.session.error or '采集线程已退出')
            if time.monotonic()>deadline:raise RuntimeError('等待目标模式连续完整帧超时')
            self.hub.stop.wait(.02)
        raise InterruptedError('service stopped')

    def _stop_session(self):
        if self.session is not None:self.session.request_stop()
        if self.session_thread is not None:
            self.session_thread.join(25)
            if self.session_thread.is_alive():raise RuntimeError('旧DMA读取未结束，已阻止开启新的采集实例')
        if self.session is not None:
            with self.condition:self.last_probe=dict(build_id='0x%08x'%getattr(self.session,'build',0),dma_frame_capacity=getattr(self.session,'driver_limit',0),complete_frames=dict(getattr(self.session,'complete',{})))
        self.session=None;self.session_thread=None

    def _record_failure(self,target,failure):
        session=self.session
        # Workers snapshot before they cancel DMA; use that preserved view.
        diagnostics=getattr(session,'diagnostics',{})
        if not diagnostics and hasattr(session,'diagnostic_snapshot'):
            diagnostics=session.diagnostic_snapshot('controller-timeout')
        with self.condition:
            self.last_failure=dict(target=copy.deepcopy(target),generation=self.generation,error=str(failure),
                complete_frames=dict(getattr(session,'complete',{})),diagnostics=copy.deepcopy(diagnostics))

    def _switch(self,target):
        previous=copy.deepcopy(self.current);restoring_hdmi=self.hub.config['hdmi'];self.generation+=1
        previous_settings=copy.deepcopy(self.hub.config);saved_1080=copy.deepcopy(self.hub.capture_saved_1080)
        self.hub.quiesce_capture(self.generation)
        self._stop_session()
        try:self._launch(target)
        except Exception as failure:
            self._record_failure(target,failure)
            self.hub.events.emit('采集切换失败，正在恢复：'+str(failure),'ERROR')
            self._stop_session();self.generation+=1;self.hub.quiesce_capture(self.generation)
            with self.hub.lock:self.hub.config=previous_settings;self.hub.capture_saved_1080=saved_1080
            self._launch(previous)
            with self.condition:self.error='切换失败，已恢复原模式：'+str(failure)
            target=previous
        self.current=copy.deepcopy(target);self.hub.finish_capture(target,restoring_hdmi)

    def run(self):
        try:
            self._launch(self.current);self.hub.finish_capture(self.current,'off')
            with self.condition:self.state='running'
            while not self.hub.stop.is_set():
                with self.condition:
                    if self.pending is None:
                        if self.session.error:raise RuntimeError(self.session.error)
                        self.condition.wait(.1);continue
                    target=self.pending;self.pending=None
                self._switch(target)
                with self.condition:self.state='running'
        except Exception as error:
            self._record_failure(self.current,error)
            with self.condition:self.state='error';self.error=str(error);self.reason=str(error)
            self.hub.events.emit('采集模式失败：'+str(error),'ERROR')
        finally:
            try:self._stop_session()
            except Exception as error:
                with self.condition:self.error=str(error);self.state='error'

    def close(self):
        with self.condition:self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(28)
            if self.thread.is_alive():raise RuntimeError('采集控制器尚未停止')
