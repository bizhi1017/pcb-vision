"""Identical raw YOLOv5 filtering, decoding boxes only for retained candidates."""
import numpy as np


def postprocess(outputs,ratio,dw,dh,src_w,src_h,legacy):
    if len(outputs)!=3 or not all(o is not None and np.asarray(o).ndim==4 for o in outputs):
        return legacy.postprocess_auto(outputs,ratio,dw,dh,src_w,src_h)
    arrays=[np.asarray(o,dtype=np.float32) for o in outputs]
    if not all(a.shape[0]==1 and a.shape[1]%3==0 and a.shape[1]//3>=6 and a.shape[2]==a.shape[3] for a in arrays):
        return legacy.postprocess_auto(outputs,ratio,dw,dh,src_w,src_h)
    lo=min(float(a.min()) for a in arrays);hi=max(float(a.max()) for a in arrays)
    if not (lo<-.5 or hi>1.5):return legacy.postprocess_auto(outputs,ratio,dw,dh,src_w,src_h)
    boxes=[];classes=[];scores=[]
    for out,mask in zip(arrays,legacy.MASKS):
        h,w=out.shape[-2:];raw=out.reshape(3,-1,h,w).transpose(2,3,0,1)
        # A loose raw-logit gate avoids sigmoid on obvious background. The
        # gates have sigmoid < .019/.269, safely below their threshold ranges.
        # Keep the original float32 sigmoid and >= comparison for survivors;
        # an exact logit threshold could change rounding at the boundary.
        eligible=None
        if .05<=legacy.OBJ_THRESH<=.95:
            gate=-1. if legacy.OBJ_THRESH>=.35 else -4.
            eligible=raw[...,4]>=gate
        # Dense scenes use the original vector path rather than many gathered
        # array copies. This gate changes work only, never the kept candidates.
        sparse=eligible is not None and np.count_nonzero(eligible)<=eligible.size//8
        if sparse:
            iy,ix,ia=np.where(eligible)
            obj=legacy._sig(raw[iy,ix,ia,4]);keep=obj>=legacy.OBJ_THRESH
            iy=iy[keep];ix=ix[keep];ia=ia[keep];obj=obj[keep]
        else:
            obj=legacy._sig(raw[...,4]);iy,ix,ia=np.where(obj>=legacy.OBJ_THRESH)
        if not len(iy):continue
        values=raw[iy,ix,ia];prob=legacy._sig(values[:,5:]);best=prob.max(axis=1)
        keep=best>=legacy.OBJ_THRESH
        if not keep.any():continue
        values=values[keep];iy=iy[keep];ix=ix[keep];ia=ia[keep]
        coords=legacy._sig(values[:,:4]);xy=coords[:,:2]*2-.5
        xy+=np.stack((ix,iy),axis=1);xy*=int(legacy.IMG_SIZE/h)
        anchors=np.asarray([legacy.ANCHORS[i] for i in mask])[ia]
        wh=np.power(coords[:,2:4]*2,2)*anchors
        boxes.append(np.concatenate((xy,wh),axis=1));classes.append(prob.argmax(axis=1)[keep]);scores.append(best[keep]*(obj[keep] if sparse else obj[iy,ix,ia]))
    if not boxes:return legacy._empty_det()
    boxes=legacy.xywh2xyxy(np.concatenate(boxes));classes=np.concatenate(classes);scores=np.concatenate(scores)
    nb=[];ns=[];nc=[]
    for cid in set(classes.tolist()):
        indexes=np.where(classes==cid);b=boxes[indexes];s=scores[indexes];keep=legacy.nms_boxes(b,s)
        nb.append(b[keep]);ns.append(s[keep]);nc.append(classes[indexes][keep])
    boxes=np.concatenate(nb);scores=np.concatenate(ns);classes=np.concatenate(nc)
    boxes[:,[0,2]]=(boxes[:,[0,2]]-dw)/ratio;boxes[:,[1,3]]=(boxes[:,[1,3]]-dh)/ratio
    boxes[:,[0,2]]=np.clip(boxes[:,[0,2]],0,src_w);boxes[:,[1,3]]=np.clip(boxes[:,[1,3]],0,src_h)
    return boxes.astype(np.int32),scores,classes.astype(np.int32)
