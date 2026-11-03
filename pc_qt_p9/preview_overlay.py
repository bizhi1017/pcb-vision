"""Project short-lived detections onto current video; never copy/draw source pixels."""
import base64,time
try:
    import cv2
    import numpy as np
except ImportError:cv2=None

CLASS_STYLES=(('缺件','#00e650'),('污渍','#ff4d4f'),('划痕','#ffd43b'))


class PreviewOverlay:
    def __init__(self):
        self.key=None;self.patches=[];self.last_gray=None;self.last_shift=None;self.reliable_at=0.
        self.boxes=[];self.size=(1,1);self.state='off';self.error=''

    def prepare(self,meta,key):
        png=base64.b64decode(meta['reference_png'],validate=True)
        reference=cv2.imdecode(np.frombuffer(png,np.uint8),cv2.IMREAD_GRAYSCALE)
        if reference is None or reference.shape!=(180,320):raise ValueError('invalid tracking reference')
        width,height=meta['width'],meta['height']
        if not (0<width<=4096 and 0<height<=4096):raise ValueError('invalid overlay dimensions')
        self.size=(width,height);self.boxes=meta.get('detections',[])[:128];self.patches=[]
        for d in self.boxes[:3]:
            box=np.asarray(d['box'],np.float64).reshape(2,2)*[320/width,180/height]
            center=box.mean(axis=0);half=np.clip((box[1]-box[0])/2+12,24,48)
            x0,y0=np.maximum(np.floor(center-half).astype(int),0)
            x1,y1=np.minimum(np.ceil(center+half).astype(int),[320,180])
            if x1-x0<24 or y1-y0<24:continue
            patch=reference[y0:y1,x0:x1].astype(np.float32)
            self.patches.append((x0,y0,x1,y1,patch,cv2.createHanningWindow((x1-x0,y1-y0),cv2.CV_32F)))
        self.key=key;self.last_gray=None;self.last_shift=None;self.reliable_at=0.

    def project(self,image,meta):
        self.state='off';self.error=''
        if cv2 is None:self.error='OpenCV unavailable for local overlay';return []
        if not meta or not meta.get('enabled'):return []
        if not 0<=meta.get('age_ms',1e9)<min(800,meta.get('max_age_ms',0)):return []
        try:
            key=(meta['epoch'],meta['result_seq'])
            if key!=self.key:self.prepare(meta,key)
            if not self.boxes:self.state='clear';return []
            pixels=getattr(image,'_preview_pixels',None)
            if pixels is None:self.state='waiting';return []
            small=cv2.resize(pixels,(320,180),interpolation=cv2.INTER_LINEAR)
            gray=cv2.GaussianBlur(cv2.cvtColor(small,cv2.COLOR_RGB2GRAY),(3,3),.6)
            shifts=[]
            for x0,y0,x1,y1,ref,window in self.patches:
                patch=gray[y0:y1,x0:x1].astype(np.float32)
                # Sensor/JPEG noise at rest must not toggle the box every frame.
                if float(cv2.absdiff(ref,patch).mean())<3:shifts.append((0.,0.));continue
                if min(float(ref.std()),float(patch.std()))<4:continue
                shift,response=cv2.phaseCorrelate(ref.copy(),patch,window)
                if response>=.4 and np.isfinite(shift).all() and max(abs(v) for v in shift)<=8:shifts.append(shift)
            required=min(2,len(self.patches));shift=None
            if required and len(shifts)>=required:
                values=np.asarray(shifts);median=np.median(values,axis=0)
                good=values[np.linalg.norm(values-median,axis=1)<=.75]
                if len(good)>=required:shift=np.median(good,axis=0)
            now=time.monotonic()
            if shift is not None:self.last_shift=shift;self.reliable_at=now;self.state='tracked'
            elif self.last_shift is not None and now-self.reliable_at<.1 and self.last_gray is not None:
                differences=[float(cv2.absdiff(gray[y0:y1,x0:x1],self.last_gray[y0:y1,x0:x1]).mean()) for x0,y0,x1,y1,_,_ in self.patches]
                if differences and max(differences)<2:shift=self.last_shift;self.state='held'
            self.last_gray=gray
            if shift is None:self.state='waiting';return []
            offset=shift/[320,180];width,height=self.size;result=[]
            for d in self.boxes:
                box=np.asarray(d['box'],np.float64).reshape(2,2)/[width,height]+offset
                box=np.clip(box,0,1)
                if np.any(box[1]<=box[0]):continue
                cid=d['class_id'];name=CLASS_STYLES[cid][0] if type(cid) is int and 0<=cid<3 else d.get('class_name','defect')
                result.append(dict(box=box.reshape(-1).tolist(),class_id=cid,label='%s %.2f'%(name,d['score'])))
            return result
        except (cv2.error,ValueError,KeyError,TypeError,IndexError) as exc:
            self.state='waiting';self.error=str(exc);return []
