"""Standalone inference owner: one NPU context, no capture/display contexts."""
import sys,os,time
from hdmi_cpu_worker import read_message,write_message
protocol_out=None


def prepare_cpu():
    """Constrain only this helper, before libraries create their own threads."""
    if hasattr(os,'sched_getaffinity'):
        from cpu_layout import choose_cpus
        allowed=os.sched_getaffinity(0);preferred=choose_cpus('inference_worker',allowed)
        if preferred and preferred!=allowed:os.sched_setaffinity(0,preferred)
    return os.getpriority(os.PRIO_PROCESS,0) if hasattr(os,'getpriority') else None


def restore_priority(original):
    """RKNN may raise its caller to -19; do not outrank live video work."""
    state=dict(original_nice=original)
    if original is None:return state
    state['runtime_nice']=os.getpriority(os.PRIO_PROCESS,0)
    try:
        if state['runtime_nice']<original:os.setpriority(os.PRIO_PROCESS,0,original)
    except OSError as error:state['error']=str(error)
    state['active_nice']=os.getpriority(os.PRIO_PROCESS,0)
    return state


def tracking_reference(frame,small=None):
    import cv2
    from live_overlay import LiveOverlay
    gray=LiveOverlay.gray(frame) if small is None else cv2.GaussianBlur(cv2.cvtColor(small,cv2.COLOR_BGR2GRAY),(3,3),.6)
    return cv2.imencode('.png',gray,[
        cv2.IMWRITE_PNG_COMPRESSION,1,
        cv2.IMWRITE_PNG_STRATEGY,cv2.IMWRITE_PNG_STRATEGY_RLE])


def run():
    global protocol_out
    # RKNN native libraries can printf directly to fd 1. Keep a duplicate of
    # the private output pipe and move all diagnostics to stderr before imports.
    protocol_out=os.fdopen(os.dup(sys.stdout.fileno()),'wb',buffering=0)
    os.dup2(sys.stderr.fileno(),sys.stdout.fileno())
    original_nice=prepare_cpu()
    import ctypes,signal,mmap
    import numpy as np
    import cv2
    from contextlib import ExitStack
    from inference_model import Detector
    # Exit if the owning service dies; never leave its NPU helper orphaned.
    if sys.platform.startswith('linux'):
        parent=os.getppid();lib=ctypes.CDLL(None)
        lib.prctl(1,signal.SIGTERM,0,0,0)
        if os.getppid()!=parent:return
    cv2.setNumThreads(1)
    incoming=sys.stdin.buffer;outgoing=protocol_out;initial=read_message(incoming)
    detector=None
    with ExitStack() as resources:
        maps={}
        for name,path in initial['mailboxes'].items():
            file=resources.enter_context(open(path,'r+b',buffering=0));maps[name]=resources.enter_context(mmap.mmap(file.fileno(),0,access=mmap.ACCESS_WRITE))
        try:
            # Legacy decoder prints initialization messages: keep stdout solely
            # for the private protocol, redirect diagnostics to stderr.
            from contextlib import redirect_stdout
            with redirect_stdout(sys.stderr):detector=Detector(initial['model'],initial['classes'])
            scheduling=restore_priority(original_nice)
            compact_supported=callable(getattr(detector,'run_prepared',None))
            write_message(outgoing,dict(ready=True,pid=os.getpid(),affinity=sorted(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else [],scheduling=scheduling,npu_runtime=getattr(detector,'runtime',{}),compact_input=compact_supported))
            while True:
                job=read_message(incoming)
                if job is None:return
                shape=tuple(job['shape'])
                if len(shape)!=3 or shape[2]!=3 or min(shape)<=0 or np.prod(shape)>3840*2160*3 or (not job.get('compact_input') and np.prod(shape)>len(maps['input'])):raise ValueError('Invalid inference image dimensions')
                frame=rgb=small=output=image=result=None
                try:
                    if job.get('compact_input'):
                        from inference_prepare import MODEL_SHAPE,TRACKING_SHAPE,MODEL_BYTES,COMPACT_BYTES
                        if not compact_supported or shape not in ((1080,1920,3),(2160,3840,3)) or len(maps['input'])<COMPACT_BYTES:
                            raise ValueError('Invalid compact inference mailbox')
                        rgb=np.ndarray(MODEL_SHAPE,np.uint8,buffer=maps['input'])
                        small=np.ndarray(TRACKING_SHAPE,np.uint8,buffer=maps['input'],offset=MODEL_BYTES)
                        with redirect_stdout(sys.stderr):result=detector.run_prepared(rgb,small,shape)
                        if not result.get('deferred_annotation'):raise RuntimeError('Compact inference requires deferred annotation')
                    else:
                        frame=np.ndarray(shape,np.uint8,buffer=maps['input'])
                        with redirect_stdout(sys.stderr):result=detector.run(frame,annotate=False)
                    reference_started=time.perf_counter()
                    ok,reference=tracking_reference(frame,result.pop('tracking_bgr',small))
                    count=int(reference.nbytes) if ok and reference.nbytes<=65536 else 0
                    if count:maps['output'][1920*1080*3:1920*1080*3+count]=reference.tobytes()
                    result['tracking_reference_bytes']=count
                    result.setdefault('timings',{})['tracking_reference_ms']=round((time.perf_counter()-reference_started)*1000,2)
                    image=result.pop('image')
                    if not result.get('deferred_annotation'):
                        output=np.ndarray(shape,np.uint8,buffer=maps['output']);output[:]=image
                    result.update(serial=job['serial'],shape=shape,completed_at=time.monotonic())
                    write_message(outgoing,result)
                finally:
                    # Release all exported mmap views before shutdown or a
                    # failed job leaves the ExitStack to close the mailboxes.
                    frame=rgb=small=output=image=result=None
        finally:
            if detector is not None:detector.net.release()


if __name__=='__main__':
    try:run()
    except (EOFError,BrokenPipeError,KeyboardInterrupt):pass
    except Exception as error:
        try:write_message(protocol_out or sys.stdout.buffer,dict(fatal=repr(error)))
        except (EOFError,BrokenPipeError):pass
