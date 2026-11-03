"""P9 measurement HTTP regression with only board imports, CPU images and temp files."""
from pathlib import Path
import base64,hashlib,json,sys,tempfile,threading
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import HTTPError
root=Path(__file__).resolve().parents[1]
sys.path[:]=[str(root/'rk3576_p9')]+[p for p in sys.path if 'pc_qt' not in p and p!=str(root/'rk3576')]
import cv2,numpy as np
import board_outline
from dual_camera_server import Hub,Handler,ThreadingHTTPServer
assert Path(board_outline.__file__).resolve().parent==root/'rk3576_p9','Board import relies on Qt files'
e=root/'test/inspection/p9_measurement_fix';e.mkdir(parents=True,exist_ok=True)
checks=[]
def check(value,label):assert value,label;checks.append(label)
opener=build_opener(ProxyHandler({}))
with tempfile.TemporaryDirectory() as directory:
    directory=Path(directory);paths={}
    for camera,scale in [('A',.1),('B',.2)]:
        data=dict(camera=camera,image_size=[960,540],K=[[1000,0,480],[0,1000,270],[0,0,1]],dist=[0]*5,
                  H_undistorted_px_to_mm=[[scale,0,0],[0,scale,0],[0,0,1]])
        path=directory/('camera_'+camera+'.json');path.write_text(json.dumps(data),encoding='utf-8');paths[camera]=str(path)
    original={c:hashlib.sha256(Path(p).read_bytes()).hexdigest() for c,p in paths.items()}
    hub=Hub(calibrations=paths.copy())
    image=np.full((540,960,3),230,np.uint8);image[150:450,200:700]=35
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.hub=hub
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    def measure(camera):
        request=Request('http://127.0.0.1:%d/api/v2/camera/%s/measure'%(server.server_port,camera),data=b'{}',headers={'Content-Type':'application/json'})
        with opener.open(request,timeout=5) as response:return json.load(response)
    try:
        for camera,long_mm,short_mm in [('A',50,30),('B',100,60)]:
            hub.publish(camera,image.copy(),mode_applied=0)
            result=measure(camera)
            check(result['camera']==camera and abs(result['result']['long_mm']-long_mm)<.3 and abs(result['result']['short_mm']-short_mm)<.3,'actual HTTP endpoint uses the selected camera calibration on synthetic geometry')
            rendered=cv2.imdecode(np.frombuffer(base64.b64decode(result['png']),np.uint8),cv2.IMREAD_COLOR)
            check(rendered is not None and rendered.shape==image.shape,'measurement returns a valid complete PNG')
            check(not hub.channels[camera]['measure_busy'],'successful measurement releases busy guard')
        hub.publish('A',np.full_like(image,230),mode_applied=0)
        try:measure('A')
        except HTTPError as exc:check(exc.code==400 and 'No module named' not in exc.read().decode(),'board localization failure returns a normal operator error instead of import failure')
        else:raise AssertionError('Blank image accepted')
        check(not hub.channels['A']['measure_busy'],'failed localization releases busy guard')
        hub.publish('A',image,mode_applied=0);check(measure('A')['camera']=='A','measurement recovers after a failed request without service restart')
        hub.publish('B',image,mode_applied=3)
        try:measure('B')
        except HTTPError as exc:check(exc.code==400,'filtered image remains rejected')
        else:raise AssertionError('Sobel image accepted')
        check(not hub.channels['B']['measure_busy'],'mode error releases busy guard')
        check(all(hashlib.sha256(Path(paths[c]).read_bytes()).hexdigest()==h for c,h in original.items()),'measurements never alter calibration files')
    finally:hub.stop.set();server.shutdown();server.server_close();worker.join(2)
(e/'offline_checks.json').write_text(json.dumps(dict(scope='CPU synthetic image + localhost HTTP only; board-only import path without pc_qt; no actual cameras/accelerators/SD',module_sha256=hashlib.sha256(Path(board_outline.__file__).read_bytes()).hexdigest(),checks=checks),ensure_ascii=False,indent=2),encoding='utf-8')
print('P9 MEASUREMENT DEPENDENCY PASS:',len(checks),'checks; no Qt source path required')
