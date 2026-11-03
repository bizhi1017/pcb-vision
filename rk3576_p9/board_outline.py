"""Fixed-camera measurement with automatic dark PCB localization on a light background."""
import json
from pathlib import Path
import cv2
import numpy as np

def measure_outline(payload, profile="rotated", calibration_path=None):
    if profile not in ("original", "rotated"):
        raise ValueError("未知摆放方向")
    d=json.loads((Path(calibration_path) if calibration_path else Path(__file__).with_name("board_plane_calibration.json")).read_text(encoding="utf-8"))
    K=np.array(d["K"]); D=np.array(d["dist"]); H=np.array(d["H_undistorted_px_to_mm"])
    im=payload.copy() if isinstance(payload,np.ndarray) else cv2.imdecode(np.frombuffer(payload,np.uint8),cv2.IMREAD_COLOR)
    if im is None: raise ValueError("无法解码板卡图像")
    if [im.shape[1],im.shape[0]] != d["image_size"]:
        raise ValueError("画面分辨率与标定不一致，需要重新标定")
    # Outside the lens remap is background, not a dark PCB. Black padding can
    # join protruding components and dominate the connected-component search.
    mx,my=cv2.initUndistortRectifyMap(K,D,None,K,(im.shape[1],im.shape[0]),cv2.CV_32FC1)
    im=cv2.remap(im,mx,my,cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT,borderValue=(255,255,255))
    g=cv2.cvtColor(im,cv2.COLOR_BGR2GRAY).astype(float)
    g=cv2.GaussianBlur(g,(5,5),0.8)
    gx=cv2.Sobel(g,cv2.CV_64F,1,0,ksize=3); gy=cv2.Sobel(g,cv2.CV_64F,0,1,ksize=3)
    # Locate the dominant dark PCB body; component protrusions occupy fewer scan lines.
    mask=(g<125).astype(np.uint8)
    mask[:90,:]=0
    mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((15,15),np.uint8))
    count, labels, stats, _=cv2.connectedComponentsWithStats(mask,8)
    if count<2: raise ValueError('未找到 PCB，请检查板框是否完整入镜')
    label=1+int(np.argmax(stats[1:,cv2.CC_STAT_AREA]))
    if stats[label,cv2.CC_STAT_AREA]<10000: raise ValueError('未找到足够大的 PCB 区域')
    ys,xs=np.where(labels==label)
    def dominant(values,limit):
        hist=np.bincount(np.array(values,dtype=int),minlength=limit).astype(np.float32)
        return int(np.argmax(cv2.GaussianBlur(hist.reshape(1,-1),(9,1),1.5)))
    left=[];right=[];top=[];bottom=[]
    for y in range(int(ys.min()),int(ys.max())+1):
        hits=np.flatnonzero(labels[y]==label)
        if len(hits)>100: left.append(hits[0]);right.append(hits[-1])
    for x in range(int(xs.min()),int(xs.max())+1):
        hits=np.flatnonzero(labels[:,x]==label)
        if len(hits)>80: top.append(hits[0]);bottom.append(hits[-1])
    if not all((left,right,top,bottom)): raise ValueError('板框区域不完整')
    x0=dominant(left,im.shape[1]);x1=dominant(right,im.shape[1])
    y0=dominant(top,im.shape[0]);y1=dominant(bottom,im.shape[0])
    if x1-x0<150 or y1-y0<80: raise ValueError('板框定位失败，请将板子大致水平放置')
    def band(v,limit): return range(max(1,v-14),min(limit-1,v+15))
    xx=range(x0+int((x1-x0)*.08),x1-int((x1-x0)*.08))
    yy=range(y0+int((y1-y0)*.08),y1-int((y1-y0)*.08))
    spec=[('top',0,xx,band(y0,im.shape[0]),1),('right',1,yy,band(x1,im.shape[1]),-1),('bottom',0,xx,band(y1,im.shape[0]),-1),('left',1,yy,band(x0,im.shape[1]),1)]
    lines=[]; details=[]
    for name,axis,scans,search,sign in spec:
        pts=[]; ss=np.array(list(search))
        for s in scans:
            values=(-sign*gy[ss,s]) if axis==0 else (-sign*gx[s,ss])
            k=int(np.argmax(values)); loc=float(ss[k])
            if 0<k<len(ss)-1:
                a,b,c=values[k-1:k+2]; den=a-2*b+c
                if abs(den)>1e-9: loc+=float(np.clip(0.5*(a-c)/den,-0.5,0.5))
            if values[k]<30: continue
            pts.append((s,loc) if axis==0 else (loc,s))
        if len(pts)<10: raise ValueError("有效板边点不足，请恢复已标定机位和摆放位置")
        pts=np.array(pts,np.float32)
        v=cv2.fitLine(pts,cv2.DIST_HUBER,0,0.01,0.01).ravel()
        for _ in range(4):
            vx,vy,x,y=map(float,v)
            distance=np.abs((pts[:,0]-x)*(-vy)+(pts[:,1]-y)*vx)
            good=distance<2.5
            if good.sum()<15: raise ValueError('板边被遮挡或反光过强，请露出更多板边')
            pts=pts[good]
            v=cv2.fitLine(pts,cv2.DIST_HUBER,0,0.01,0.01).ravel()
        vx,vy,x,y=map(float,v); line=np.array([-vy,vx,vy*x-vx*y]); lines.append(line)
        residual=abs(pts@line[:2]+line[2]); details.append(dict(edge=name,points=len(pts),residual_rms_px=float(np.sqrt(np.mean(residual**2)))))
        for p in pts[::4]: cv2.circle(im,tuple(np.round(p).astype(int)),1,(0,255,255),-1)
    def cross(a,b):
        p=np.cross(a,b); return p[:2]/p[2]
    quad=np.array([cross(lines[3],lines[0]),cross(lines[0],lines[1]),cross(lines[1],lines[2]),cross(lines[2],lines[3])])
    mm=cv2.perspectiveTransform(quad.reshape(-1,1,2),H).reshape(-1,2)
    lengths=np.linalg.norm(np.roll(mm,-1,axis=0)-mm,axis=1)
    out=dict(method='Automatic PCB region plus robust edge fits; preliminary',long_mm=float((lengths[0]+lengths[2])/2),short_mm=float((lengths[1]+lengths[3])/2),sides_mm=lengths.tolist(),corners_undistorted_px=quad.tolist(),edge_fits=details)
    # Presentation only: source HUD/watermark occupies this excluded top band.
    # Replace it with an explicit title strip, without modifying measurement pixels.
    im[:80, :] = (28,28,28)
    cv2.polylines(im,[np.round(quad).astype(np.int32)],True,(0,255,0),2)
    cv2.putText(im,f"PCB  {out['long_mm']:.2f} x {out['short_mm']:.2f} mm (PRELIMINARY)",(20,48),cv2.FONT_HERSHEY_SIMPLEX,0.75,(80,230,110),2,cv2.LINE_AA)
    ok, encoded=cv2.imencode(".png",im)
    if not ok: raise ValueError("测量图生成失败")
    return out, encoded.tobytes()
