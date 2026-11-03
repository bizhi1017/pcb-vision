"""Back-to-back mode epochs, reset and both camera tags on stored bytes only."""
from pathlib import Path
import os,sys,subprocess,re,json,importlib.util
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'rk3576_p9'))
import fpga_codec as codec
spec=importlib.util.spec_from_file_location('old',root/'rk3576/fpga_codec.py');old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
out=root/'test/inspection/p9_predictive';bench=out/'tb_p9.sv';rtl=root/'fpga/p9_compact/uisrc/01_rtl/codec/frame_lz77.v'
source=out/'small.raw';raw=source.read_bytes();cases=[]
for camera in (0,1):
    sim=out/f'transition{camera}.vvp';packet=out/f'transition{camera}.bin'
    subprocess.run([os.environ.get('IVERILOG','iverilog'),'-g2012','-s','tb_frame_lz77',f'-Ptb_frame_lz77.SIZE={len(raw)}',f'-Ptb_frame_lz77.CAM={camera}',
                    '-o',str(sim),str(rtl),str(bench)],check=True,cwd=root)
    p=subprocess.run([os.environ.get('VVP','vvp'),str(sim),'+input='+str(source),'+output='+str(packet),'+config=1','+frames=3','+stalls=1'],check=True,capture_output=True,text=True,cwd=root)
    lengths=list(map(int,re.findall(r'FRAME_BYTES\s+(\d+)',p.stdout)));assert len(lengths)==3
    wire=packet.read_bytes();offset=0
    for frame,n in enumerate(lengths):
        actual=wire[offset:offset+n];offset+=n;decoded,meta=codec.decode_python(actual,camera,(len(raw),))
        if frame==0:expected=old.encode_reference(raw,camera,1,0,1,0);want=raw
        else:expected=codec.encode_compact_reference(raw,camera,0,frame+1,8);want=codec.quantize(raw,3)
        assert actual==expected and decoded[:-16]==want
        cases.append(dict(camera=camera,frame=frame+1,mode=meta['mode'],epoch=meta['epoch'],golden_equal=True))
    assert offset==len(wire)
(out/'transitions.json').write_text(json.dumps(dict(scope='CPU RTL, synthetic stored bytes, mid-frame change held until next SOF, back-to-back frame reset, both camera IDs',cases=cases),indent=2),encoding='utf-8')
print('P9 TRANSITIONS PASS: 6 frames, both cameras, v1 lossless -> v2 lossy6 with correct SOF epoch')
