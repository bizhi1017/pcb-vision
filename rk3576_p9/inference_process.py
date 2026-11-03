"""One sequential model worker and two bounded frame mailboxes, no frame queue."""
from pathlib import Path
import os,sys,time,subprocess,tempfile,mmap,threading,atexit
import base64
import numpy as np
from hdmi_cpu_worker import read_message,write_message

CAPACITY=1920*1080*3
REFERENCE_CAPACITY=65536


class ProcessDetector:
    def __init__(self,model,classes,helper=None):
        if not Path(model).is_file() or not Path(classes).is_file():raise ValueError('Missing inference model or classes')
        self.lock=threading.Lock();self.serial=0;self.process=None;self.directory=None;self.files={};self.maps={};self.paths={};self.closed=False
        self.net=self;self.pid=None;self.affinity=[]
        self.last_status=0.;self.memory={};self.scheduling={};self.npu_runtime={}
        self.compact_supported=False;self.preparer=None;self.transfer={}
        try:
            self.directory=tempfile.TemporaryDirectory(prefix='pcb_infer_')
            for name in ('input','output'):
                size=CAPACITY+(REFERENCE_CAPACITY if name=='output' else 0)
                path=Path(self.directory.name)/(name+'.bgr');file=open(path,'w+b',buffering=0);file.truncate(size)
                self.files[name]=file;self.maps[name]=mmap.mmap(file.fileno(),size,access=mmap.ACCESS_WRITE);self.paths[name]=str(path)
            self.process=subprocess.Popen([sys.executable,str(helper or Path(__file__).with_name('inference_cpu_worker.py'))],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=None,bufsize=0,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0) if os.name=='nt' else 0)
            write_message(self.process.stdin,dict(model=str(model),classes=str(classes),mailboxes=self.paths))
            packet=read_message(self.process.stdout)
            if not packet.get('ready'):raise RuntimeError(packet.get('fatal','Inference worker did not initialize'))
            self.pid=packet['pid'];self.affinity=packet['affinity'];self.scheduling=packet.get('scheduling',{});self.npu_runtime=packet.get('npu_runtime',{});atexit.register(self.release)
            self.compact_supported=bool(packet.get('compact_input')) and os.environ.get('PCB_INFER_COMPACT_INPUT')!='0'
            if self.compact_supported:
                from inference_prepare import InputPreparer
                import rk_pcb_view as input_legacy
                self.input_legacy=input_legacy
                self.preparer=InputPreparer()
        except BaseException:self.release();raise

    def run(self,frame):
        if frame.dtype!=np.uint8 or frame.ndim!=3 or frame.shape[2]!=3 or frame.nbytes>3840*2160*3:raise ValueError('Inference requires bounded BGR frame')
        with self.lock:
            if self.closed:raise RuntimeError('Inference process is closed')
            started=time.perf_counter();self.serial+=1
            compact=self.compact_supported and frame.shape in ((1080,1920,3),(2160,3840,3)) and frame.flags.c_contiguous
            if frame.nbytes>CAPACITY and not compact:raise ValueError('4K requires compact inference support')
            prepare_ms=0.;preprocess_backend=None
            if compact:
                from inference_prepare import MODEL_SHAPE,TRACKING_SHAPE,MODEL_BYTES,COMPACT_BYTES
                rgb,ratio,dw,dh,small=self.preparer.prepare(frame,self.input_legacy)
                if (ratio,dw,dh)!=(640/frame.shape[1],0.,140.):raise RuntimeError('Unexpected compact letterbox geometry')
                if small is None:
                    import cv2
                    small=cv2.resize(frame,(320,180),interpolation=cv2.INTER_LINEAR)
                preprocess_backend=self.preparer.backend+' / compact IPC'
                copy_started=time.perf_counter();prepare_ms=(copy_started-started)*1000
                mapped=np.ndarray(MODEL_SHAPE,np.uint8,buffer=self.maps['input']);mapped[:]=rgb;del mapped
                mapped=np.ndarray(TRACKING_SHAPE,np.uint8,buffer=self.maps['input'],offset=MODEL_BYTES);mapped[:]=small;del mapped
                transferred=COMPACT_BYTES
            else:
                copy_started=time.perf_counter()
                mapped=np.ndarray(frame.shape,np.uint8,buffer=self.maps['input']);mapped[:]=frame;del mapped
                transferred=frame.nbytes
            prepared=time.perf_counter()
            write_message(self.process.stdin,dict(shape=frame.shape,serial=self.serial,compact_input=compact));packet=read_message(self.process.stdout)
            if packet.get('fatal'):raise RuntimeError(packet['fatal'])
            if packet.get('serial')!=self.serial or tuple(packet.get('shape',()))!=frame.shape:raise RuntimeError('Inference worker response mismatch')
            if compact and not packet.get('deferred_annotation'):raise RuntimeError('Compact worker returned an annotated image')
            # Finish this copy before the next job can rewrite the mailbox.
            if packet.get('deferred_annotation'):image=frame
            else:
                image=np.ndarray(frame.shape,np.uint8,buffer=self.maps['output']).copy();image.flags.writeable=False
            reference_bytes=packet.pop('tracking_reference_bytes',0)
            if not 0<=reference_bytes<=REFERENCE_CAPACITY:raise RuntimeError('Invalid tracking reference length')
            if reference_bytes:packet['tracking_reference_png']=base64.b64encode(self.maps['output'][CAPACITY:CAPACITY+reference_bytes]).decode('ascii')
            packet['image']=image;finished=time.perf_counter();timings=packet.setdefault('timings',{})
            if compact:
                timings.update(preprocess_ms=round(prepare_ms,2),parent_prepare_ms=round(prepare_ms,2),preprocess_backend=preprocess_backend,
                               total_ms=round(timings.get('total_ms',0)+prepare_ms,2))
            timings.update(input_copy_ms=round((prepared-copy_started)*1000,2),process_roundtrip_ms=round((finished-prepared)*1000,2),
                input_transfer_bytes=transferred,input_transfer_backend='compact RGB+tracking' if compact else 'full BGR',
                parent_total_ms=round((finished-started)*1000,2),worker_pid=self.pid,worker_affinity=self.affinity,backend='isolated process')
            self.transfer=dict(backend=timings['input_transfer_backend'],bytes=transferred,compact_supported=self.compact_supported)
            return packet

    def status(self):
        now=time.monotonic()
        if os.name=='posix' and self.pid and now-self.last_status>=2:
            try:
                rows=Path('/proc/%d/status'%self.pid).read_text().splitlines()
                self.memory={line.split(':')[0]+'_kb':int(line.split()[1]) for line in rows if line.startswith(('VmRSS:','RssAnon:','RssFile:','RssShmem:'))}
            except (OSError,ValueError):pass
            self.last_status=now
        return dict(backend='isolated process',pid=self.pid,cpu_affinity=self.affinity,scheduling=dict(self.scheduling),npu_runtime=dict(self.npu_runtime),input_transfer=dict(self.transfer),alive=self.process is not None and self.process.poll() is None,memory=dict(self.memory),mailbox_bytes=CAPACITY*2+REFERENCE_CAPACITY)

    def release(self):
        if self.closed:return
        self.closed=True
        if self.process is not None:
            if self.process.poll() is None:
                try:
                    write_message(self.process.stdin,None);self.process.wait(3)
                except (OSError,EOFError,subprocess.TimeoutExpired):
                    self.process.terminate()
                    try:self.process.wait(2)
                    except subprocess.TimeoutExpired:self.process.kill();self.process.wait(2)
            self.process.stdin.close();self.process.stdout.close()
        for memory in self.maps.values():memory.close()
        for file in self.files.values():file.close()
        self.maps.clear();self.files.clear()
        if self.directory is not None:
            target=Path(self.directory.name).resolve();expected=Path(tempfile.gettempdir()).resolve()
            if target.parent!=expected or not target.name.startswith('pcb_infer_'):raise RuntimeError('Unexpected inference temp directory')
            self.directory.cleanup()
