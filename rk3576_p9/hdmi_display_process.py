"""Latest-only HDMI display bridge; the camera process owns no EGL context."""
import mmap,os,subprocess,sys,tempfile,threading,time
from pathlib import Path
import numpy as np
from cpu_layout import choose_cpus
from hdmi_cpu_worker import read_message,write_message
from shared_image import shared_descriptor

MAX_BUFFER=32*1024*1024


class ProcessRenderer:
    def __init__(self,width,height,x,y,cpu_test=False):
        self.geometry=(width,height,x,y);self.cpu_test=cpu_test
        self.condition=threading.Condition();self.pending=None;self.closed=False;self.error='';self.process=None
        self.latest={};self.timings={};self.replaced=0;self.peak_pending=0;self.key=0
        self.manager_affinity=[];self.frame_bytes=0;self.copy_ms=0.;self.roundtrip_ms=0.
        self.shared_frames=0
        self.inflight_at=0.
        self.last_polled_frame=-1
        self.files={};self.maps={};self.paths={};self.atlas_tokens={};self.frame_tokens={};self.directory=None
        self.worker=threading.Thread(target=self.run,name='HDMI-display-bridge',daemon=True);self.worker.start()

    @property
    def backend(self):
        with self.condition:return self.latest.get('backend','independent HDMI display process (starting)')

    def present(self,mode,sources,annotations):
        with self.condition:
            if self.error:raise RuntimeError(self.error)
            if self.closed:raise RuntimeError('HDMI display process is closed')
            if self.pending is not None:self.replaced+=1
            self.pending=(mode,sources,annotations);self.peak_pending=1;self.condition.notify_all()

    def poll(self):
        with self.condition:
            # Presentation is asynchronous now: do not replace EGL's old
            # blocking swap with a 500Hz Python source/status polling loop.
            if not self.closed and not self.error and self.latest.get('submitted_frames',0)==self.last_polled_frame:
                self.condition.wait(1/120)
            self.last_polled_frame=self.latest.get('submitted_frames',0)
            if self.error:raise RuntimeError(self.error)
            if self.inflight_at and time.monotonic()-self.inflight_at>8:raise RuntimeError('HDMI display process did not acknowledge its frame')
            return self.key

    def display_stats(self):
        with self.condition:
            stats=dict(video_frames=0,submitted_frames=0,last_submit_at=0.,video_fps=0.,submit_fps=0.)
            stats.update(self.latest)
            stats['display_pipeline']=dict(backend='isolated EGL process',pid=self.process.pid if self.process else None,
                pending_slots=self.peak_pending,replaced_jobs=self.replaced,manager_cpu_affinity=list(self.manager_affinity),
                copied_bytes=self.frame_bytes,shared_frames=self.shared_frames,copy_ms=self.copy_ms,roundtrip_ms=self.roundtrip_ms,error=self.error)
            if time.monotonic()-stats.get('last_submit_at',0)>2:stats.update(video_fps=0.,submit_fps=0.)
            return stats

    def start_process(self):
        base='/dev/shm' if os.name=='posix' and Path('/dev/shm').is_dir() and os.access('/dev/shm',os.W_OK) else None
        self.directory=tempfile.TemporaryDirectory(prefix='pcb_hdmi_display_',dir=base)
        for kind in ('frame','atlas'):
            for c in ('A','B'):
                name=kind+c;path=Path(self.directory.name)/(name+'.bin')
                file=open(path,'w+b',buffering=0);file.truncate(MAX_BUFFER)
                self.files[name]=file;self.maps[name]=mmap.mmap(file.fileno(),MAX_BUFFER,access=mmap.ACCESS_WRITE);self.paths[name]=str(path)
        args=[sys.executable,str(Path(__file__).with_name('hdmi_display_worker.py'))]
        if self.cpu_test:args.append('--cpu-test')
        self.process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0) if os.name=='nt' else 0)

    def pack(self,mode,sources,annotations):
        started=time.monotonic();packed={};used=0;shared_frames=0
        for c in ('A','B') if mode=='split' else (mode,):
            source=sources[c];image=source.get('image');shape=None;shared=None
            if image is not None:
                if image.dtype!=np.uint8 or image.ndim!=3 or image.shape[2]!=3 or image.nbytes>MAX_BUFFER:
                    raise ValueError('Invalid HDMI shared image layout')
                shape=image.shape;token=(source['seq'],source['mode'],shape);shared=shared_descriptor(image)
                if shared is not None:shared_frames+=1
                elif self.frame_tokens.get(c)!=token:
                    target=np.ndarray(shape,np.uint8,buffer=self.maps['frame'+c]);target[:]=image;del target;used+=image.nbytes
                    self.frame_tokens[c]=token
            atlas=getattr(annotations[c],'atlas',None);texture=None
            if atlas and atlas.get('pixels') is not None:
                pixels=atlas['pixels'];token=atlas['token'];upload=self.atlas_tokens.get(c)!=token
                if pixels.dtype!=np.uint8 or pixels.ndim!=3 or pixels.shape[2]!=4 or pixels.nbytes>MAX_BUFFER:
                    raise ValueError('Invalid HDMI shared atlas layout')
                if upload:
                    target=np.ndarray(pixels.shape,np.uint8,buffer=self.maps['atlas'+c]);target[:]=pixels;del target
                    self.atlas_tokens[c]=token;used+=pixels.nbytes
                texture=dict(token=token,shape=pixels.shape,quads=atlas['quads'])
            packed[c]=dict(shape=shape,shared=shared,seq=source['seq'],mode=source['mode'],source_at=source['source_at'],
                result_at=source.get('result_at',0),active=source.get('active',False),annotations=list(annotations[c]),atlas=texture)
        self.frame_bytes=used;self.shared_frames=shared_frames;self.copy_ms=round((time.monotonic()-started)*1000,2)
        return dict(mode=mode,sources=packed,geometry=self.geometry,mailboxes=self.paths)

    def run(self):
        try:
            if hasattr(os,'sched_getaffinity'):
                try:
                    selected=choose_cpus('hdmi',os.sched_getaffinity(0));os.sched_setaffinity(0,selected);self.manager_affinity=sorted(selected)
                except OSError:pass
            while True:
                with self.condition:
                    while not self.closed and self.pending is None:self.condition.wait()
                    if self.closed:return
                    mode,sources,annotations=self.pending;self.pending=None
                if self.process is None:self.start_process()
                # Keep the HQ ndarray roots alive until the child has uploaded
                # these exact pixels and ACKed. Pool storage cannot recycle early.
                job=self.pack(mode,sources,annotations)
                started=time.monotonic()
                with self.condition:self.inflight_at=started
                write_message(self.process.stdin,job);job=None
                packet=read_message(self.process.stdout);elapsed=round((time.monotonic()-started)*1000,2)
                sources=annotations=None
                if 'fatal' in packet:raise RuntimeError(packet['fatal'])
                if packet.get('forbidden_modules'):raise RuntimeError('HDMI display worker imported camera/NPU service')
                with self.condition:
                    self.inflight_at=0.;self.roundtrip_ms=elapsed;self.latest=packet['stats'];self.timings=self.latest.get('renderer_timings',{});self.key=packet['key']
                    self.condition.notify_all()
        except Exception as error:
            with self.condition:
                if not self.closed:self.error=str(error)
        finally:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(3)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(2)

    def close(self):
        with self.condition:self.closed=True;self.pending=None;self.condition.notify_all()
        if self.process is not None and self.process.poll() is None:self.process.terminate()
        self.worker.join(4)
        if self.process is not None:
            if self.process.poll() is None:self.process.kill();self.process.wait(2)
            if self.worker.is_alive():self.worker.join(2)
            self.process.stdin.close();self.process.stdout.close()
        if self.worker.is_alive():raise RuntimeError('HDMI display bridge did not stop')
        for memory in self.maps.values():memory.close()
        for file in self.files.values():file.close()
        self.maps.clear();self.files.clear()
        if self.directory is not None:
            directory=Path(self.directory.name).resolve()
            allowed={Path(tempfile.gettempdir()).resolve(),Path('/dev/shm').resolve()}
            if directory.parent not in allowed or not directory.name.startswith('pcb_hdmi_display_'):raise RuntimeError('Unexpected display buffer directory')
            self.directory.cleanup()
