"""Short-lived local translation checks; never substitute an old image."""
import threading
import cv2
import numpy as np


class LiveOverlay:
    def __init__(self):
        self.lock=threading.Lock();self.key=None;self.reference=None;self.patches=[]

    @staticmethod
    def gray(image):
        small=cv2.resize(image,(320,180),interpolation=cv2.INTER_LINEAR)
        return cv2.GaussianBlur(cv2.cvtColor(small,cv2.COLOR_BGR2GRAY),(3,3),.6)

    def prepare(self,reference,detections,key):
        self.key=key;self.reference=self.gray(reference);self.patches=[]
        factors=np.array([320/reference.shape[1],180/reference.shape[0]])
        # Anchor on detection neighborhoods rather than a textured background.
        for detection in detections[:3]:
            box=np.asarray(detection['box'],np.float64).reshape(2,2)*factors
            center=box.mean(axis=0);half=np.clip((box[1]-box[0])/2+12,24,48)
            start=np.maximum(np.floor(center-half).astype(int),0)
            end=np.minimum(np.ceil(center+half).astype(int),[320,180])
            x0,y0=map(int,start);x1,y1=map(int,end)
            if x1-x0<24 or y1-y0<24:continue
            patch=self.reference[y0:y1,x0:x1].astype(np.float32)
            window=cv2.createHanningWindow((x1-x0,y1-y0),cv2.CV_32F)
            self.patches.append((x0,y0,x1,y1,patch,window))

    def translation(self,current):
        shifts=[]
        for x0,y0,x1,y1,reference,window in self.patches:
            patch=current[y0:y1,x0:x1].astype(np.float32)
            if float(cv2.absdiff(reference,patch).mean())<.35:
                shifts.append((0.,0.));continue
            if min(float(reference.std()),float(patch.std()))<4:continue
            # Some OpenCV builds apply the window in-place; protect the cache.
            shift,response=cv2.phaseCorrelate(reference.copy(),patch,window)
            if response>=.4 and np.isfinite(shift).all() and max(abs(v) for v in shift)<=8:shifts.append(shift)
        required=min(2,len(self.patches))
        if not required or len(shifts)<required:return None
        shifts=np.asarray(shifts);median=np.median(shifts,axis=0)
        good=shifts[np.linalg.norm(shifts-median,axis=1)<=.75]
        if len(good)<required:return None
        return np.median(good,axis=0)

    def render(self,image,reference,detections,key,exact=False):
        if not detections:return image,'clear'
        with self.lock:
            shift=np.zeros(2)
            if not exact:
                if self.key!=key:self.prepare(reference,detections,key)
                shift=self.translation(self.gray(image))
                if shift is None:return image,'waiting'
            h,w=image.shape[:2];offset=shift*np.array([w/320.,h/180.]);output=image.copy()
            for d in detections:
                x0,y0,x1,y1=d['box']
                start=np.maximum(np.floor(np.array([x0,y0])+offset).astype(int),0)
                end=np.minimum(np.ceil(np.array([x1,y1])+offset).astype(int),[w-1,h-1])
                if np.any(end<=start):continue
                cv2.rectangle(output,tuple(map(int,start)),tuple(map(int,end)),(0,230,80),2)
                text='%s %.2f'%(d.get('class_name',('missing','stain','scratch')[d['class_id']]),d['score'])
                cv2.putText(output,text,(int(start[0]),max(18,int(start[1])-5)),cv2.FONT_HERSHEY_SIMPLEX,.6,(0,230,80),2,cv2.LINE_AA)
            return output,'detected' if exact else 'tracked'
