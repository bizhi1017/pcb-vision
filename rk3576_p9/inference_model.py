"""One RKNN context with byte-identical full-resolution model preprocessing."""
import time
from pathlib import Path
import numpy as np
import cv2
import threading
PCB_CLASSES=('missing','stain','scratch')
PCB_LABELS=('缺件','污渍','划痕')
_draw_lock=threading.Lock()


def render_annotations(frame,data):
    import rk_pcb_view as legacy
    output=frame.copy()
    with _draw_lock:
        legacy.set_class_names(PCB_CLASSES)
        return legacy.draw_results(output,data['boxes'],data['scores'],data['classes'])

class Detector:
    def __init__(self, model, classes):
        import rk_pcb_view as legacy
        names,error=legacy.load_classes(classes)
        if error or tuple(names)!=PCB_CLASSES:raise ValueError('PCB classes must be missing, stain, scratch in that order: '+str(error or names))
        if not Path(model).is_file():raise ValueError('model missing: '+model)
        if legacy.RKNNLite is None: raise RuntimeError('RKNNLite unavailable')
        self.legacy = legacy
        legacy.set_class_names(names)
        from npu_runtime import open_runtime
        self.net,self.runtime=open_runtime(model,legacy.RKNNLite)
        from inference_prepare import InputPreparer
        self.preparer=InputPreparer()

    def run(self, frame, annotate=True):
        l = self.legacy
        started=time.perf_counter()
        # Resize the original full-resolution source before swapping channels.
        # Channel permutation commutes with letterbox's interpolation/padding;
        # the model sees identical RGB bytes, without a 1080P RGB intermediate.
        image,ratio,dw,dh,tracking_bgr=self.preparer.prepare(frame,l)
        prepared=time.perf_counter()
        return self._run_input(image,ratio,dw,dh,frame.shape,tracking_bgr,frame,annotate,started,prepared,self.preparer.backend)

    def run_prepared(self,image,tracking_bgr,source_shape):
        from inference_prepare import MODEL_SHAPE,TRACKING_SHAPE
        if (tuple(source_shape) not in ((1080,1920,3),(2160,3840,3)) or image.dtype!=np.uint8 or image.shape!=MODEL_SHAPE
                or tracking_bgr.dtype!=np.uint8 or tracking_bgr.shape!=TRACKING_SHAPE
                or not image.flags.c_contiguous or not tracking_bgr.flags.c_contiguous):
            raise ValueError('Invalid compact inference input')
        started=time.perf_counter()
        return self._run_input(image,640/source_shape[1],0.,140.,source_shape,tracking_bgr,None,False,started,started,'parent exact input')

    def _run_input(self,image,ratio,dw,dh,source_shape,tracking_bgr,frame,annotate,started,prepared,preprocess_backend):
        l=self.legacy;src_h,src_w=source_shape[:2]
        outputs = self.net.inference(inputs=[image[None]])
        inferred=time.perf_counter()
        if outputs is None: raise RuntimeError('empty RKNN inference')
        from fast_yolo import postprocess
        args=(ratio,dw,dh,src_w,src_h)
        if getattr(self,'fast_compatible',True):
            candidate=postprocess(outputs,*args,l)
            if not hasattr(self,'fast_compatible'):
                reference=l.postprocess_auto(outputs,*args)
                self.fast_compatible=all(np.array_equal(a,b) for a,b in zip(candidate,reference))
                if not self.fast_compatible:candidate=reference
            boxes,scores,ids=candidate
        else:boxes,scores,ids=l.postprocess_auto(outputs,*args)
        detections=[];clean_boxes=[];clean_scores=[];clean_ids=[]
        for box,score,cid in zip(boxes,scores,ids):
            cid=int(cid);score=float(score)
            if not 0<=cid<3:raise ValueError('model returned non-PCB class id')
            if not np.isfinite(score) or not np.isfinite(box).all():continue
            x1,y1,x2,y2=[int(v) for v in box]
            x1,x2=max(0,x1),min(src_w-1,x2)
            y1,y2=max(0,y1),min(src_h-1,y2)
            if x2<=x1 or y2<=y1:continue
            clean_boxes.append([x1,y1,x2,y2]);clean_scores.append(score);clean_ids.append(cid)
            detections.append(dict(class_id=cid,class_name=PCB_CLASSES[cid],label=PCB_LABELS[cid],score=round(score,4),box=[x1,y1,x2,y2]))
        annotation_data=dict(boxes=clean_boxes,scores=clean_scores,classes=clean_ids)
        decoded=time.perf_counter();image=render_annotations(frame,annotation_data) if annotate else frame
        finished=time.perf_counter()
        timings=dict(preprocess_ms=round((prepared-started)*1000,2),npu_call_ms=round((inferred-prepared)*1000,2),
                     postprocess_ms=round((decoded-inferred)*1000,2),draw_ms=round((finished-decoded)*1000,2),total_ms=round((finished-started)*1000,2))
        timings['postprocess_backend']='sparse validated' if self.fast_compatible else 'legacy fallback'
        timings['preprocess_backend']=preprocess_backend
        return dict(image=image,detections=detections,timings=timings,annotation_data=annotation_data,deferred_annotation=not annotate,tracking_bgr=tracking_bgr)

