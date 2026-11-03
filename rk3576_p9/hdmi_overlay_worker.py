"""One replaceable input slot; CPU tracking/OpenCV tiles never block display."""
import threading,time,subprocess,sys,os,tempfile,mmap
from pathlib import Path
import cv2
import numpy as np
from hdmi_overlay import HdmiOverlay
from hdmi_annotations import OpenCvAtlas,Annotations
from hdmi_renderer import image_rect
from hdmi_cpu_worker import read_message,write_message,MAX_ATLAS


class OverlayComposer:
    def __init__(self,width,height,start_thread=True,backend='process'):
        self.width,self.height=width,height;self.condition=threading.Condition();self.pending=None;self.closed=False
        self.results={};self.trackers={c:HdmiOverlay() for c in ('A','B')};self.atlases={c:OpenCvAtlas() for c in ('A','B')}
        self.serial=0;self.replaced=0;self.peak_pending=0;self.worker=None;self.latest_diagnostics={}
        self.backend=backend if start_thread else 'inline';self.cpu_process=None;self.atlas_cache={};self.reference_cache={}
        self.worker_pid=None;self.worker_affinity=[];self.ipc_error='';self.prepare_ms=0.;self.roundtrip_ms=0.;self.process_rss_kb=0;self.rss_at=0.
        self.mailbox_dir=None;self.mailbox_files={};self.mailboxes={};self.mailbox_paths={}
        if start_thread:
            self.worker=threading.Thread(target=self.run_process if backend=='process' else self.run,name='HDMI-overlay-manager',daemon=True);self.worker.start()

    def submit(self,mode,sources):
        if not any(s['active'] for s in sources.values()):return
        if self.worker is None:self.process(mode,sources);return
        with self.condition:
            if self.closed:return
            if self.pending is not None:self.replaced+=1
            self.pending=(mode,sources);self.peak_pending=1;self.condition.notify_all()

    def process(self,mode,sources):
        results={};started=time.monotonic()
        for slot,c in enumerate(('A','B') if mode=='split' else (mode,)):
            s=sources[c]
            if not s['active']:continue
            try:
                boxes,state=self.trackers[c].project(s['image'],s['reference'],s['detections'],(s['detect_epoch'],s['processed_seq']),exact=s['seq']==s['processed_seq'],reference_png=s.get('reference_png'))
                viewport=image_rect(self.width,self.height,s['image'],slot,mode=='split')
                annotations=self.atlases[c].prepare(boxes,viewport)
                results[c]=dict(mode=mode,epoch=s['detect_epoch'],source_at=s['source_at'],result_at=s['result_at'],seq=s['seq'],viewport=viewport,annotations=annotations,state=state,error='')
            except Exception as exc:
                results[c]=dict(mode=mode,epoch=s['detect_epoch'],source_at=s['source_at'],result_at=s['result_at'],seq=s['seq'],annotations=Annotations(),state='waiting',error=str(exc),viewport=None)
        with self.condition:
            self.serial+=1
            completed=time.monotonic()
            for result in results.values():result.update(serial=self.serial,work_ms=round((completed-started)*1000,2),completed_at=completed)
            self.results=results

    def latest(self,mode,sources):
        now=time.monotonic();selected={};diagnostics={}
        with self.condition:results=dict(self.results)
        for slot,c in enumerate(('A','B') if mode=='split' else (mode,)):
            s=sources[c];result=results.get(c)
            if not s['active']:diagnostics[c]=dict(reason='detection inactive');continue
            if not result:diagnostics[c]=dict(reason='waiting for first projection');continue
            lag=s['source_at']-result['source_at'];completed_age=now-result['completed_at'];result_age=now-result['result_at']
            valid=(result['mode']==mode and result['epoch']==s['detect_epoch'] and result['seq']<=s['seq']
                and 0<=lag<.2 and 0<=completed_age<.15 and 0<=result_age<.8
                and result['viewport']==image_rect(self.width,self.height,s['image'],slot,mode=='split'))
            diagnostics[c]=dict(reason=result['state'] if valid else result['error'] or 'expired or changed projection',
                source_lag_ms=round(lag*1000,2),completed_age_ms=round(completed_age*1000,2),result_age_ms=round(result_age*1000,2),work_ms=result['work_ms'])
            if valid:selected[c]=result
        self.latest_diagnostics=diagnostics
        return selected

    def run(self):
        due=0.
        while True:
            with self.condition:
                while not self.closed and (self.pending is None or time.monotonic()<due):
                    self.condition.wait(max(.001,due-time.monotonic()) if self.pending is not None else None)
                if self.closed:return
                mode,sources=self.pending;self.pending=None
            started=time.monotonic();self.process(mode,sources);sources=None
            due=max(started+1/30,time.monotonic())

    def prepare_job(self,mode,sources):
        packed={};started=time.monotonic()
        for slot,c in enumerate(('A','B') if mode=='split' else (mode,)):
            s=sources[c]
            if not s['active']:continue
            key=(s['detect_epoch'],s['processed_seq']);reference=self.reference_cache.get(c)
            if reference is None or reference[0]!=key:
                png=s.get('reference_png')
                reference=(key,png,None if png else cv2.resize(s['reference'],(320,180),interpolation=cv2.INTER_LINEAR))
                self.reference_cache[c]=reference
            packed[c]=dict(epoch=s['detect_epoch'],processed_seq=s['processed_seq'],source_at=s['source_at'],result_at=s['result_at'],seq=s['seq'],
                small=cv2.resize(s['image'],(320,180),interpolation=cv2.INTER_LINEAR),reference_png=reference[1],reference_small=reference[2],
                reference_width=s['reference'].shape[1],reference_height=s['reference'].shape[0],detections=s['detections'],
                viewport=image_rect(self.width,self.height,s['image'],slot,mode=='split'))
        self.prepare_ms=round((time.monotonic()-started)*1000,2)
        return dict(mode=mode,sources=packed,acks={c:item['token'] for c,item in self.atlas_cache.items()},mailboxes=self.mailbox_paths)

    def run_process(self):
        due=0.
        try:
            while True:
                with self.condition:
                    while not self.closed and (self.pending is None or time.monotonic()<due):
                        self.condition.wait(max(.001,due-time.monotonic()) if self.pending is not None else None)
                    if self.closed:return
                    mode,sources=self.pending;self.pending=None
                started=time.monotonic()
                if self.cpu_process is None:
                    self.mailbox_dir=tempfile.TemporaryDirectory(prefix='pcb_hdmi_')
                    for c in ('A','B'):
                        path=Path(self.mailbox_dir.name)/(c+'.rgba');file=open(path,'w+b',buffering=0);file.truncate(MAX_ATLAS)
                        self.mailbox_files[c]=file;self.mailboxes[c]=mmap.mmap(file.fileno(),MAX_ATLAS,access=mmap.ACCESS_WRITE);self.mailbox_paths[c]=str(path)
                    self.cpu_process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('hdmi_cpu_worker.py'))],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,bufsize=0,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0) if os.name=='nt' else 0)
                    self.worker_pid=self.cpu_process.pid
                job=self.prepare_job(mode,sources);sources=None
                before=time.monotonic();write_message(self.cpu_process.stdin,job);packet=read_message(self.cpu_process.stdout)
                self.roundtrip_ms=round((time.monotonic()-before)*1000,2)
                if 'fatal' in packet:raise RuntimeError(packet['fatal'])
                self.worker_affinity=packet['affinity']
                if packet['hardware_modules_loaded']:raise RuntimeError('CPU overlay worker imported a hardware service module')
                received={}
                for c,result in packet['results'].items():
                    atlas=result.pop('atlas');boxes=result.pop('boxes');shape=atlas.pop('shape');atlas['pixels']=None
                    if shape is not None:
                        if len(shape)!=3 or shape[2]!=4 or min(shape)<=0 or int(np.prod(shape))>MAX_ATLAS:raise ValueError('Invalid CPU atlas dimensions')
                        # Copy once before sending the next request. The child
                        # cannot rewrite its mailbox until this handshake ends.
                        pixels=np.ndarray(shape,np.uint8,buffer=self.mailboxes[c]).copy();atlas['pixels']=pixels
                        pixels.setflags(write=False);self.atlas_cache[c]=dict(token=atlas['token'],pixels=pixels)
                    elif boxes:
                        cached=self.atlas_cache.get(c)
                        if cached is None or cached['token']!=atlas['token']:continue
                        atlas['pixels']=cached['pixels']
                    result['annotations']=Annotations(boxes,atlas);received[c]=result
                with self.condition:self.results=received
                now=time.monotonic()
                if os.name=='posix' and now-self.rss_at>2:
                    try:
                        fields=Path('/proc/%d/status'%self.worker_pid).read_text().splitlines()
                        self.process_rss_kb=int(next(line.split()[1] for line in fields if line.startswith('VmRSS:')))
                    except (OSError,StopIteration,ValueError):pass
                    self.rss_at=now
                due=max(started+1/30,time.monotonic())
        except Exception as exc:
            if not self.closed:self.ipc_error=str(exc)
        finally:
            if self.cpu_process is not None and self.cpu_process.poll() is None:
                self.cpu_process.terminate()
                try:self.cpu_process.wait(2)
                except subprocess.TimeoutExpired:self.cpu_process.kill();self.cpu_process.wait(2)

    def close(self):
        with self.condition:self.closed=True;self.pending=None;self.condition.notify_all()
        if self.cpu_process is not None and self.cpu_process.poll() is None:self.cpu_process.terminate()
        if self.worker is not None:self.worker.join(2)
        if self.cpu_process is not None:
            try:self.cpu_process.wait(2)
            except subprocess.TimeoutExpired:self.cpu_process.kill();self.cpu_process.wait(2)
            self.cpu_process.stdin.close();self.cpu_process.stdout.close()
        if self.worker is not None and self.worker.is_alive():self.worker.join(2)
        self.results.clear();self.atlases.clear();self.trackers.clear()
        self.atlas_cache.clear();self.reference_cache.clear()
        for memory in self.mailboxes.values():memory.close()
        for file in self.mailbox_files.values():file.close()
        self.mailboxes.clear();self.mailbox_files.clear()
        if self.mailbox_dir is not None:
            target=Path(self.mailbox_dir.name).resolve();expected=Path(tempfile.gettempdir()).resolve()
            if target.parent!=expected or not target.name.startswith('pcb_hdmi_'):raise RuntimeError('Unexpected HDMI temporary directory')
            self.mailbox_dir.cleanup()
