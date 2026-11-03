"""On-demand browser frames; no device access, network changes or Qt ACK feedback."""
from dataclasses import dataclass
from pathlib import Path
import ipaddress,socket,struct,threading,time
import cv2
import numpy as np
from live_overlay import LiveOverlay

PROFILES={'smooth':dict(width=640,quality=72,fps=8,mbps=2),
          'source':dict(width=None,quality=None,fps=5,mbps=12)}


def network_info(port,enumerate_ifaces=None):
    rows=[]
    if enumerate_ifaces is not None:raw=enumerate_ifaces()
    else:
        raw=[]
        try:
            import fcntl
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
                for _,name in socket.if_nameindex():
                    try:
                        reply=fcntl.ioctl(sock.fileno(),0x8915,struct.pack('256s',name[:15].encode()))
                        raw.append(dict(name=name,addr=socket.inet_ntoa(reply[20:24])))
                    except OSError:continue
        except (ImportError,OSError):pass
    seen=set()
    for row in raw:
        name=str(row.get('name',''));addr=str(row.get('addr',''))
        try:ip=ipaddress.IPv4Address(addr)
        except ipaddress.AddressValueError:continue
        if ip.is_loopback or ip.is_unspecified or ip.is_multicast or (name,addr) in seen:continue
        seen.add((name,addr));wireless=name.lower().startswith(('wl','wlan'))
        rows.append(dict(name=name,addr=addr,wireless=wireless,url='http://%s:%d/m'%(addr,port)))
    rows.sort(key=lambda x:(not x['wireless'],x['name'],x['addr']))
    return dict(interfaces=rows,mobile_urls=[x['url'] for x in rows if x['wireless']],
                urls=[x['url'] for x in rows],wifi_ready=any(x['wireless'] for x in rows),port=port)


@dataclass(frozen=True)
class BrowserFrame:
    id:int
    data:bytes
    codec:str
    source_at:float
    meta:dict


class BrowserPreview:
    def __init__(self,hub):
        self.hub=hub
        self.slots={(c,p):dict(lock=threading.Lock(),at=0.,frame=None,token=None,counter=0,tracker=LiveOverlay())
                    for c in ('A','B') for p in PROFILES}

    def annotations(self,c,token,image,tracker):
        with self.hub.lock:
            s=self.hub.channels[c];age=(time.monotonic()-s.get('processed_source_at',0))*1000
            enabled=self.hub.config['detect'][c]
            active=(enabled and token[1]==0 and token[3]==s['detect_epoch'] and not s['infer_error']
                    and s['processed_reference'] is not None and 0<=age<800 and s['processed_seq']<=token[0])
            if not active:return dict(boxes=[],remaining_ms=0,detect_enabled=enabled)
            reference=s['processed_reference'];detections=list(s['detections'][:128]);seq=s['processed_seq'];epoch=s['detect_epoch'];expires_at=s['processed_source_at']+.8
        shift=np.zeros(2)
        if seq!=token[0]:
            with tracker.lock:
                if tracker.key!=(epoch,seq):tracker.prepare(reference,detections,(epoch,seq))
                shift=tracker.translation(tracker.gray(image))
            if shift is None:return dict(boxes=[],remaining_ms=0,detect_enabled=True)
        offset=np.array([shift[0]/320,shift[1]/180]*2);scale=np.array([reference.shape[1],reference.shape[0]]*2)
        boxes=[]
        for d in detections:
            cid=d.get('class_id');score=d.get('score');box=np.asarray(d.get('box',[]),np.float64)
            if type(cid) is not int or not 0<=cid<3 or box.shape!=(4,) or not np.isfinite(box).all():continue
            coords=np.clip(box/scale+offset,0,1)
            if coords[2]<=coords[0] or coords[3]<=coords[1]:continue
            boxes.append(dict(class_id=cid,score=float(score),box=coords.tolist()))
        return dict(boxes=boxes,remaining_ms=max(0,800-age),expires_at=expires_at,detect_enabled=True)

    def get(self,c,profile,after=0):
        if c not in ('A','B') or profile not in PROFILES:raise ValueError('invalid browser camera/profile')
        slot=self.slots[c,profile];cfg=PROFILES[profile]
        with slot['lock']:
            with self.hub.lock:
                if self.hub.capture_switching or c not in self.hub.capture_channels:raise ValueError('本路采集已停止或正在切换')
                if not self.hub.config.get('browser_push',{}).get(c,True):raise ValueError('本路手机推送已关闭')
                state=self.hub.channels[c]
                if state['frame'] is None or time.monotonic()-state['at']>=2:raise ValueError('camera has no fresh frame')
                epoch=state['detect_epoch'];generation=self.hub.capture_generation
            cached=slot['frame'];now=time.monotonic()
            if cached and slot['token'][3]==epoch and now-slot['at']<1/cfg['fps']:
                return cached if cached.id!=after else None
            if profile=='source':
                packet=self.hub.client_network[c].get(timeout=.1,consumer='browser-source-'+c,max_fps=cfg['fps'])
                if packet is None:return None
                token=tuple(packet.token)+(packet.codec,packet.width,packet.height,getattr(packet,'quality',0),getattr(packet,'version',None))
                data=packet.data;codec=packet.codec;width,height=packet.width,packet.height
                source_at=packet.source_at;image=None
            else:
                image,token,source_at=self.hub.clean_network_frame(c)
                if image is None:return None
                width=min(cfg['width'],image.shape[1]);height=round(image.shape[0]*width/image.shape[1])
                image=cv2.resize(image,(width,height),interpolation=cv2.INTER_AREA) if width!=image.shape[1] else image
                data=None;codec='jpeg'
            if cached and token==slot['token'] and now-cached.source_at<2:return cached if cached.id!=after else None
            with self.hub.lock:enabled=self.hub.config['detect'][c]
            if image is None and enabled:image=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
            overlay=self.annotations(c,token,image,slot['tracker']) if image is not None else dict(boxes=[],remaining_ms=0,detect_enabled=enabled)
            # Do not publish boxes from an epoch that was disabled mid-encode.
            with self.hub.lock:
                if generation!=self.hub.capture_generation:raise ValueError('采集模式已改变，请重试')
                if epoch!=self.hub.channels[c]['detect_epoch']:overlay=dict(boxes=[],remaining_ms=0,detect_enabled=False)
            if data is None:
                ok,jpg=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,cfg['quality']])
                if not ok:raise ValueError('browser JPEG encoding failed')
                data=jpg.tobytes()
            slot['counter']+=1
            meta=dict(camera=c,profile=profile,seq=token[0],width=width,height=height,capture_generation=generation,**overlay)
            item=BrowserFrame(slot['counter'],data,codec,source_at,meta)
            slot.update(frame=item,token=token,at=time.monotonic())
            return item if item.id!=after else None


def asset(name):
    if name not in ('index.html','app.js','style.css'):raise ValueError('unknown browser asset')
    return Path(__file__).with_name('web').joinpath(name).read_bytes()
