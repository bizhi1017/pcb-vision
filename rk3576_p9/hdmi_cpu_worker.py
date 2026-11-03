"""Private pipe CPU worker. Never import/open camera, NPU, RGA, X11 or EGL."""
import os,pickle,struct,sys,time

MAX_MESSAGE=4*1024*1024
MAX_ATLAS=32*1024*1024


def read_message(stream):
    def exact(size):
        chunks=[]
        while size:
            chunk=stream.read(size)
            if not chunk:raise EOFError('HDMI CPU pipe closed')
            chunks.append(chunk);size-=len(chunk)
        return b''.join(chunks)
    size=struct.unpack('!I',exact(4))[0]
    if not 0<size<=MAX_MESSAGE:raise ValueError('HDMI CPU message exceeds bound')
    return pickle.loads(exact(size))


def write_message(stream,message):
    payload=pickle.dumps(message,protocol=5)
    if not 0<len(payload)<=MAX_MESSAGE:raise ValueError('HDMI CPU message exceeds bound')
    data=memoryview(struct.pack('!I',len(payload))+payload)
    while data:
        count=stream.write(data)
        if not count:raise EOFError('HDMI CPU pipe closed')
        data=data[count:]
    stream.flush()


def run():
    # Imports stay inside this standalone entry point: no parent service/main
    # module is executed, and no live hardware context is forked/inherited.
    import base64
    import atexit,mmap
    from contextlib import ExitStack
    from types import SimpleNamespace
    import cv2
    from station_preview_overlay import PreviewOverlay
    from hdmi_annotations import OpenCvAtlas
    cv2.setNumThreads(1)
    if hasattr(os,'sched_getaffinity'):
        allowed=os.sched_getaffinity(0);preferred=allowed.intersection({1,3})
        if preferred:os.sched_setaffinity(0,preferred)
    trackers={c:PreviewOverlay() for c in ('A','B')};atlases={c:OpenCvAtlas() for c in ('A','B')}
    input_stream=sys.stdin.buffer;output_stream=sys.stdout.buffer;serial=0;mailboxes={};references={};resources=ExitStack();atexit.register(resources.close)
    while True:
        job=read_message(input_stream)
        if job is None:return
        if not mailboxes:
            for c,path in job['mailboxes'].items():
                file=resources.enter_context(open(path,'r+b',buffering=0))
                mailboxes[c]=resources.enter_context(mmap.mmap(file.fileno(),MAX_ATLAS,access=mmap.ACCESS_WRITE))
        started=time.monotonic();results={};serial+=1
        for c,s in job['sources'].items():
            try:
                meta=dict(enabled=True,epoch=s['epoch'],result_seq=s['processed_seq'],age_ms=0,max_age_ms=800,
                          width=s['reference_width'],height=s['reference_height'],detections=s['detections'])
                key=(s['epoch'],s['processed_seq']);cached=references.get(c)
                if cached and cached[0]==key:png=cached[1]
                else:
                    png=s.get('reference_png')
                    if png is None:
                        small=s['reference_small'];gray=cv2.GaussianBlur(cv2.cvtColor(small,cv2.COLOR_BGR2GRAY),(3,3),.6)
                        ok,data=cv2.imencode('.png',gray,[cv2.IMWRITE_PNG_COMPRESSION,1])
                        if not ok:raise ValueError('CPU reference encoding failed')
                        png=base64.b64encode(data).decode('ascii')
                    references[c]=(key,png)
                meta['reference_png']=png
                rgb=cv2.cvtColor(s['small'],cv2.COLOR_BGR2RGB)
                boxes=trackers[c].project(SimpleNamespace(_preview_pixels=rgb),meta)
                annotations=atlases[c].prepare(boxes,s['viewport']);atlas=annotations.atlas
                token=(os.getpid(),atlas['token'])
                pixels=atlas['pixels'] if job.get('acks',{}).get(c)!=token else None;shape=None
                if pixels is not None:
                    import numpy as np
                    if pixels.nbytes>MAX_ATLAS:raise ValueError('CPU overlay atlas exceeds mailbox')
                    mapped=np.ndarray(pixels.shape,np.uint8,buffer=mailboxes[c]);mapped[:]=pixels;del mapped
                    shape=pixels.shape
                results[c]=dict(mode=job['mode'],epoch=s['epoch'],source_at=s['source_at'],result_at=s['result_at'],seq=s['seq'],
                    viewport=s['viewport'],boxes=boxes,state=trackers[c].state,error=trackers[c].error,
                    atlas=dict(token=token,shape=shape,quads=atlas['quads']))
            except Exception as exc:
                results[c]=dict(mode=job['mode'],epoch=s['epoch'],source_at=s['source_at'],result_at=s['result_at'],seq=s['seq'],
                    viewport=s['viewport'],boxes=[],state='waiting',error=str(exc),atlas=dict(token=(os.getpid(),0),shape=None,quads=[]))
        finished=time.monotonic()
        for result in results.values():result.update(serial=serial,work_ms=round((finished-started)*1000,2),completed_at=finished)
        write_message(output_stream,dict(results=results,pid=os.getpid(),affinity=sorted(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else [],
            hardware_modules_loaded=[name for name in ('dual_camera_server','vid008_pcie','rknnlite','hdmi_renderer') if name in sys.modules]))


if __name__=='__main__':
    try:run()
    except (EOFError,BrokenPipeError):pass
    except Exception as error:
        try:write_message(sys.stdout.buffer,dict(fatal=repr(error)))
        except (EOFError,BrokenPipeError):pass
