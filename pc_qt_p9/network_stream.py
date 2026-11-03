"""Length-delimited JPEG/PNG/negotiated PCZ1 multipart parser."""
import http.client
import json
import socket
import base64
import threading
from collections import deque
from urllib.parse import urlsplit


def configure_stream_socket(response):
    """Per-connection receive window, no system TCP settings or replay queue."""
    sock=getattr(getattr(getattr(response,'fp',None),'raw',None),'_sock',None)
    if sock is None:return
    sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024*1024)
    sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)


class AckConnection:
    """One direct persistent ACK connection per stream, never a frame queue."""
    def __init__(self,base,camera):
        parts=urlsplit(base);kind=http.client.HTTPSConnection if parts.scheme=='https' else http.client.HTTPConnection
        self.connection=kind(parts.hostname,parts.port,timeout=3)
        self.path=parts.path.rstrip('/')+'/api/v2/camera/'+camera+'/transport_ack?quiet=1'

    def send(self,client,frame_id,decode_ms,decoder=None,overlay_ms=None,preview_fps=None):
        if self.connection.sock is None:
            self.connection.connect();self.connection.sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        body=dict(client_id=client,frame_id=frame_id,decode_ms=decode_ms)
        if decoder is not None:body['decoder']=decoder
        if overlay_ms is not None:body['overlay_ms']=overlay_ms
        if preview_fps is not None:body['preview_fps']=preview_fps
        data=json.dumps(body).encode()
        self.connection.request('POST',self.path,data,{'Content-Type':'application/json'})
        response=self.connection.getresponse();response.read()
        if response.status!=204:raise ValueError('stream ACK failed: '+str(response.status))

    def close(self):self.connection.close()


class AsyncAckConnection:
    """One ACK worker, at most two metadata records including the active POST.

    Decode need not wait for each HTTP response. A slow/failing receiver still
    applies bounded backpressure; acknowledgements are never silently dropped.
    """
    def __init__(self,base,camera,cancelled=None,initialize_thread=None,sender=None):
        self.sender=sender if sender is not None else AckConnection(base,camera)
        self.cancelled=cancelled or (lambda:False);self.initialize_thread=initialize_thread
        self.condition=threading.Condition();self.queue=deque();self.pending=0;self.peak=0
        self.closed=False;self.error=None
        self.worker=threading.Thread(target=self.run,name='preview-ack-'+camera,daemon=True);self.worker.start()

    def _check(self):
        if self.error is not None:raise self.error
        if self.closed or self.cancelled():raise ConnectionAbortedError('preview ACK sender stopped')

    def check(self):
        with self.condition:self._check()

    def send(self,client,frame_id,decode_ms,decoder=None,overlay_ms=None,preview_fps=None):
        with self.condition:
            self._check()
            while self.pending>=2:
                self.condition.wait(.025);self._check()
            self.queue.append((client,frame_id,decode_ms,decoder,overlay_ms,preview_fps))
            self.pending+=1;self.peak=max(self.peak,self.pending);self.condition.notify_all()

    def run(self):
        try:
            if self.initialize_thread is not None:self.initialize_thread()
            while True:
                with self.condition:
                    while not self.queue and not self.closed:self.condition.wait()
                    if self.closed:return
                    item=self.queue.popleft()
                try:self.sender.send(*item)
                except Exception as exc:
                    with self.condition:self.error=exc;self.closed=True
                finally:
                    with self.condition:
                        self.pending-=1
                        if self.closed:
                            self.pending-=len(self.queue);self.queue.clear()
                        self.condition.notify_all()
        except Exception as exc:
            with self.condition:
                self.error=exc;self.closed=True;self.pending-=len(self.queue);self.queue.clear();self.condition.notify_all()
        finally:
            try:self.sender.close()
            except OSError:pass

    def close(self):
        with self.condition:
            self.closed=True;self.pending-=len(self.queue);self.queue.clear();self.condition.notify_all()
        # Interrupt an active socket read; the worker alone owns HTTP close().
        connection=getattr(self.sender,'connection',None);sock=getattr(connection,'sock',None)
        if sock is not None:
            try:sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        self.worker.join(4)


class FrameParser:
    def __init__(self,include_metadata=False):self.buffer=bytearray();self.pending=None;self.include_metadata=include_metadata

    def feed(self,data):
        self.buffer.extend(data);out=[]
        while True:
            if self.pending is None:
                end=self.buffer.find(b'\r\n\r\n')
                if end<0:
                    if len(self.buffer)>(196608 if self.include_metadata else 8192):raise ValueError('stream header too long')
                    break
                if end>(196608 if self.include_metadata else 8192):raise ValueError('stream header too long')
                lines=bytes(self.buffer[:end]).split(b'\r\n');headers={}
                for line in lines:
                    if b':' in line:
                        key,value=line.split(b':',1);headers[key.strip().lower()]=value.strip()
                length=int(headers.get(b'content-length',b'0'))
                if length==0 and headers.get(b'content-type')==b'application/x-heartbeat':
                    del self.buffer[:end+4];continue
                mime=headers.get(b'content-type')
                if not 0<length<=32*1024*1024 or mime not in (b'image/jpeg',b'image/png',b'application/x-pcb-zstd'):raise ValueError('invalid image frame header')
                meta={}
                if self.include_metadata and b'x-overlay' in headers:
                    meta=json.loads(base64.b64decode(headers[b'x-overlay'],validate=True))
                    if not isinstance(meta,dict):raise ValueError('invalid overlay metadata')
                if self.include_metadata and b'x-capture-generation' in headers:meta['_capture_generation']=int(headers[b'x-capture-generation'])
                self.pending=(length,int(headers[b'x-frame-id']),meta,mime);del self.buffer[:end+4]
            length,frame_id,meta,mime=self.pending
            if len(self.buffer)<length:break
            item=(frame_id,bytes(self.buffer[:length]))
            if mime==b'application/x-pcb-zstd' and not item[1].startswith(b'PCZ1'):raise ValueError('invalid lossless frame magic')
            out.append((*item,meta) if self.include_metadata else item);del self.buffer[:length];self.pending=None
        return out
