"""Private HDMI renderer entry point; no camera, NPU or JPEG contexts."""
import os,signal,sys,time
from collections import deque,OrderedDict
from contextlib import ExitStack
import mmap
import numpy as np
from cpu_layout import choose_cpus
from hdmi_cpu_worker import read_message,write_message
from hdmi_annotations import Annotations
from hdmi_renderer import NativeRenderer,OpenCvRenderer
from hdmi_display_process import MAX_BUFFER


class CpuRenderer:
    backend='CPU-only display test';timings={}
    def __init__(self,*args):pass
    def present(self,*args):pass
    def poll(self):return 0
    def close(self):pass


def rate(times,now):
    while len(times)>2 and times[0]<now-2:times.popleft()
    return round((len(times)-1)/(times[-1]-times[0]),2) if len(times)>1 and now-times[-1]<2 and times[-1]>times[0] else 0.


def run():
    import cv2
    cv2.setNumThreads(1)
    if hasattr(os,'sched_getaffinity'):
        try:os.sched_setaffinity(0,choose_cpus('hdmi',os.sched_getaffinity(0)))
        except OSError:pass
    def terminate(_signum,_frame):raise SystemExit()
    signal.signal(signal.SIGTERM,terminate)
    renderer=None;maps={};borrowed=OrderedDict();video_times=deque(maxlen=240);submit_times=deque(maxlen=240)
    frames=0;submits=0;video_key=None;error=''
    try:
        with ExitStack() as resources:
            while True:
                job=read_message(sys.stdin.buffer)
                if job is None:return
                if not maps:
                    for key,path in job['mailboxes'].items():
                        file=resources.enter_context(open(path,'r+b',buffering=0));maps[key]=resources.enter_context(mmap.mmap(file.fileno(),MAX_BUFFER,access=mmap.ACCESS_READ))
                if renderer is None:
                    if '--cpu-test' in sys.argv:renderer=CpuRenderer(*job['geometry'])
                    else:
                        try:renderer=NativeRenderer(*job['geometry'])
                        except (OSError,RuntimeError) as e:error=str(e);renderer=OpenCvRenderer(*job['geometry'])
                sources={};annotations={};now=time.monotonic()
                for c,item in job['sources'].items():
                    shape=item['shape'];image=None
                    if shape is not None:
                        if len(shape)!=3 or shape[2]!=3 or min(shape)<=0 or int(np.prod(shape))>MAX_BUFFER:raise ValueError('Invalid shared frame dimensions')
                        shared=item.get('shared')
                        if shared is not None:
                            path=shared['path'];parts=path.split('/')
                            if len(parts)!=5 or parts[1]!='proc' or not parts[2].isdigit() or parts[3]!='fd' or not parts[4].isdigit():raise ValueError('Invalid private HQ buffer path')
                            key=(path,shared['inode'],shared['size'])
                            if key not in borrowed:
                                file=open(path,'rb',buffering=0)
                                if os.fstat(file.fileno()).st_ino!=shared['inode']:file.close();raise ValueError('HQ buffer descriptor changed')
                                memory=mmap.mmap(file.fileno(),shared['size'],access=mmap.ACCESS_READ);borrowed[key]=(file,memory)
                            borrowed.move_to_end(key);file,memory=borrowed[key]
                            if shared['offset']<0 or shared['offset']+int(np.prod(shape))>shared['size']:raise ValueError('HQ buffer view exceeds storage')
                            image=np.ndarray(shape,np.uint8,buffer=memory,offset=shared['offset'])
                            while len(borrowed)>16:
                                _,(old_file,old_memory)=borrowed.popitem(last=False);old_memory.close();old_file.close()
                        else:image=np.ndarray(shape,np.uint8,buffer=maps['frame'+c])
                        image.setflags(write=False)
                    sources[c]=dict(image=image,seq=item['seq'],mode=item['mode'])
                    atlas=item['atlas'];prepared=None
                    if atlas:
                        shape=atlas['shape']
                        if len(shape)!=3 or shape[2]!=4 or min(shape)<=0 or int(np.prod(shape))>MAX_BUFFER:raise ValueError('Invalid shared atlas dimensions')
                        # Tokens identify bytes that may remain in the same mapping
                        # until the next handshake; no queued render retains them.
                        pixels=np.ndarray(shape,np.uint8,buffer=maps['atlas'+c]);pixels.setflags(write=False)
                        prepared=dict(token=atlas['token'],pixels=pixels,quads=atlas['quads'])
                    boxes=item['annotations'] if item['active'] and 0<=now-item['result_at']<.8 else []
                    annotations[c]=Annotations(boxes,prepared if boxes else dict(token=0,pixels=None,quads=[]))
                started=time.monotonic()
                if isinstance(renderer,OpenCvRenderer):
                    for source in sources.values():
                        if source['image'] is not None:source['image']=source['image'].copy()
                try:renderer.present(job['mode'],sources,annotations)
                except (RuntimeError,OSError) as e:
                    if not isinstance(renderer,NativeRenderer):raise
                    error=str(e);renderer.close();renderer=OpenCvRenderer(*job['geometry'])
                    for source in sources.values():
                        if source['image'] is not None:source['image']=source['image'].copy()
                    renderer.present(job['mode'],sources,annotations)
                key=renderer.poll();finished=time.monotonic();submits+=1;submit_times.append(finished)
                current=(job['mode'],tuple((c,s['seq'],s['mode'],s['image'] is not None) for c,s in sources.items()))
                if current!=video_key and any(s['image'] is not None for s in sources.values()):video_key=current;frames+=1;video_times.append(finished)
                stats=dict(backend=renderer.backend+' / isolated display',backend_error=error,video_fps=rate(video_times,finished),submit_fps=rate(submit_times,finished),
                    video_frames=frames,submitted_frames=submits,last_submit_at=finished,present_ms=round((finished-started)*1000,2),
                    source_seq={c:item['seq'] for c,item in job['sources'].items()},overlay_box_count={c:len(a) for c,a in annotations.items()},
                    renderer_timings=getattr(renderer,'timings',{}),display_cpu_affinity=sorted(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else [],
                    source_age_ms={c:round((finished-item['source_at'])*1000,2) if item['shape'] else None for c,item in job['sources'].items()})
                if '--cpu-test' in sys.argv:
                    import hashlib
                    stats['test_pixel_sha256']={c:hashlib.sha256(s['image'].tobytes()).hexdigest() if s['image'] is not None else None for c,s in sources.items()}
                    stats['test_boxes']={c:list(a) for c,a in annotations.items()}
                # Never let NumPy views outlive this ACK. The parent owns the
                # next write only after this response has been read.
                sources.clear();annotations.clear();image=prepared=pixels=None
                write_message(sys.stdout.buffer,dict(stats=stats,key=key,forbidden_modules=[n for n in ('dual_camera_server','vid008_pcie','rknnlite','mpp_jpeg') if n in sys.modules]))
    finally:
        if renderer is not None:renderer.close()
        for file,memory in borrowed.values():memory.close();file.close()


if __name__=='__main__':
    try:run()
    except (SystemExit,EOFError,BrokenPipeError):pass
    except Exception as error:
        try:write_message(sys.stdout.buffer,dict(fatal=repr(error)))
        except (EOFError,BrokenPipeError):pass
