"""Per-camera chessboard lens calibration and board-top plane mapping."""
import json
from pathlib import Path
import cv2
import numpy as np

SIZE=(960,540)

def validate_calibration(data,camera):
    if data.get('camera')!=camera:raise ValueError('标定文件相机标识不匹配，请使用本路标定')
    if data.get('image_size')!=list(SIZE):raise ValueError('标定尺寸必须为960×540')
    for name,shape in [('K',(3,3)),('H_undistorted_px_to_mm',(3,3))]:
        a=np.asarray(data.get(name),dtype=float)
        if a.shape!=shape or not np.isfinite(a).all() or abs(np.linalg.det(a))<1e-10:raise ValueError('无效标定矩阵：'+name)
    k=np.asarray(data['K'])
    if min(k[0,0],k[1,1])<=0:raise ValueError('无效焦距')
    d=np.asarray(data.get('dist'),dtype=float)
    if d.size not in (4,5,8,12,14) or not np.isfinite(d).all():raise ValueError('无效畸变参数')
    return data

class CalibrationSession:
    def __init__(self,camera,columns=5,rows=5,square_mm=9.4,board_thickness_mm=1.2):
        if type(columns)is not int or type(rows)is not int or not 3<=columns<=15 or not 3<=rows<=15:raise ValueError('内角点行列范围3~15')
        if not np.isfinite(square_mm) or not .5<=square_mm<=100:raise ValueError('棋盘格尺寸范围0.5~100 mm')
        if not np.isfinite(board_thickness_mm) or not 0<=board_thickness_mm<=20:raise ValueError('板厚范围0~20 mm')
        self.camera=camera;self.pattern=(columns,rows);self.square=float(square_mm);self.thickness=float(board_thickness_mm)
        self.object=np.zeros((columns*rows,3),np.float32)
        self.object[:,:2]=np.mgrid[0:columns,0:rows].T.reshape(-1,2)*square_mm
        self.points=[];self.sequences=set();self.K=None;self.dist=None;self.rms=None

    def status(self):
        return dict(samples=len(self.points),minimum=8,lens_ready=self.K is not None,rms_px=self.rms)

    def corners(self,image):
        if (image.shape[1],image.shape[0])!=SIZE:raise ValueError('标定图尺寸应为960×540')
        gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY)
        ok,corners=cv2.findChessboardCornersSB(gray,self.pattern,flags=cv2.CALIB_CB_NORMALIZE_IMAGE|cv2.CALIB_CB_EXHAUSTIVE)
        if not ok:raise ValueError('未找到完整棋盘内角点，请让整个棋盘清晰入镜并避开遮挡/反光')
        return corners.astype(np.float32).reshape(-1,1,2)

    def sample(self,image,sequence):
        if len(self.points)>=20:raise ValueError('已采集20张，请计算镜头标定或重新开始')
        if sequence in self.sequences:raise ValueError('请等待新画面再采集')
        corners=self.corners(image)
        if any(np.sqrt(np.mean((p-corners)**2))<8 for p in self.points):raise ValueError('姿态过于接近，请移动或倾斜棋盘后再采集')
        self.points.append(corners);self.sequences.add(sequence);self.K=None;self.dist=None;self.rms=None
        return corners

    def solve_lens(self):
        if len(self.points)<8:raise ValueError('至少采集8张不同位置和倾角的棋盘图')
        centers=np.array([p.reshape(-1,2).mean(axis=0) for p in self.points])
        areas=[abs(cv2.contourArea(cv2.convexHull(p))) for p in self.points]
        if np.ptp(centers,axis=0).max()<30 or max(areas)<min(areas)*1.15:raise ValueError('棋盘位置或倾角变化不足，请补充不同位置/远近/倾斜姿态')
        rms,K,D,_,_=cv2.calibrateCamera([self.object]*len(self.points),self.points,SIZE,None,None)
        if (not np.isfinite(rms) or rms>1.0 or not np.isfinite(K).all() or not np.isfinite(D).all()
            or min(K[0,0],K[1,1])<150 or max(K[0,0],K[1,1])>5000):
            raise ValueError('镜头标定质量不合格，请保持焦距固定、棋盘平整并重新采集')
        self.K=K;self.dist=D;self.rms=float(rms)

    def plane(self,image):
        if self.K is None:raise ValueError('请先完成镜头标定')
        corners=self.corners(image)
        undist=cv2.undistortPoints(corners,self.K,self.dist,P=self.K).reshape(-1,2)
        target=self.object[:,:2]
        train=np.array([(i//self.pattern[0]+i%self.pattern[0])%2==0 for i in range(len(target))])
        test=~train
        check_h,_=cv2.findHomography(undist[train],target[train],0)
        if check_h is None:raise ValueError('平面标定矩阵求解失败')
        residual=np.linalg.norm(cv2.perspectiveTransform(undist[test].reshape(-1,1,2),check_h).reshape(-1,2)-target[test],axis=1)
        rms=float(np.sqrt(np.mean(residual**2)))
        if not np.isfinite(rms) or rms>.25:raise ValueError('平面检查误差过大，请平放、垫高并铺平棋盘，避免反光')
        H,_=cv2.findHomography(undist,target,0)
        data=dict(schema='board-plane-v1',camera=self.camera,image_size=list(SIZE),K=self.K.tolist(),dist=self.dist.tolist(),
                  H_undistorted_px_to_mm=H.tolist(),inner_corners=list(self.pattern),square_mm=self.square,
                  board_thickness_mm=self.thickness,plane='PCB top surface; checker raised physically to same height',
                  lens_rms_px=self.rms,held_out_rms_mm=rms,corners=len(target),held_out_points=int(test.sum()))
        return validate_calibration(data,self.camera),corners

def save_calibration(data,camera,directory):
    validate_calibration(data,camera)
    folder=Path(directory);folder.mkdir(parents=True,exist_ok=True)
    path=folder/('camera_'+camera+'.json');temporary=path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(path)
    return str(path)

def annotated(image,corners,pattern):
    out=image.copy();cv2.drawChessboardCorners(out,pattern,corners,True)
    ok,data=cv2.imencode('.png',out)
    if not ok:raise ValueError('无法生成角点预览')
    import base64
    return base64.b64encode(data).decode()
