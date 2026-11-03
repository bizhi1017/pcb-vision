"""Independent P9 RTL/golden tests. Stored/synthetic bytes; no hardware access."""
from pathlib import Path
import os,sys,subprocess,random,re,json,hashlib,importlib.util,argparse
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'rk3576_p9'))
import fpga_codec as codec
spec=importlib.util.spec_from_file_location('legacy_codec',root/'rk3576/fpga_codec.py')
legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
parser=argparse.ArgumentParser();parser.add_argument('--full',action='store_true');args=parser.parse_args()
out=root/'test/inspection/p9_predictive';out.mkdir(parents=True,exist_ok=True)
rtl=root/'fpga/p9_compact/uisrc/01_rtl/codec/frame_lz77.v'
bench=out/'tb_p9.sv'
s=(root/'test/tb_frame_lz77.sv').read_text(encoding='utf-8')
s=s.replace('parameter CAM=0,PRE=0;','parameter CAM=0,PRE=0,FOURK=0;')
s=s.replace('dut(.clk',"dut(.I_4k(1'(FOURK)),.clk")
s=s.replace('i%152==151','i%(FOURK?302:152)==(FOURK?301:151)').replace('repeat(1600)','repeat(FOURK?440:1600)')
s=s.replace('#200000000;','#500000000;')
bench.write_text(s,encoding='utf-8')
def run(cmd):
    p=subprocess.run(cmd,cwd=root,capture_output=True,text=True)
    if p.returncode:raise RuntimeError(p.stdout+p.stderr)
    return p.stdout
r=random.Random(903);records=[]
if args.full:
    cases=[('full1080',2667904,0,0,3,1),('full4k',10591744,0,1,3,1),
           ('gray1080',2073600,1,0,3,1)]
else:
    cases=[('small',16400,0,0,m,1) for m in range(4)]
    cases += [('gray',16400,2,0,3,1),('zero',16400,0,0,3,1),('fault',16400,0,0,3,2)]
    cases += [('pattern',16400,0,0,1,1),('pattern',16400,0,0,2,1),('zeros_lz',16400,0,0,1,1)]
for name,size,pre,fourk,mode,stalls in cases:
    raw=bytes(size) if name in ('zero','zeros_lz') else (r.randbytes(256)*64+bytes(16) if name=='pattern' else r.randbytes(size))
    src=out/(name+'.raw');src.write_bytes(raw)
    stem=f'{name}_{mode}';sim=out/(stem+'.vvp');packet=out/(stem+'.bin')
    run([os.environ.get('IVERILOG','iverilog'),'-g2012','-s','tb_frame_lz77',f'-Ptb_frame_lz77.SIZE={size}',
         f'-Ptb_frame_lz77.CAM={1 if pre else 0}',f'-Ptb_frame_lz77.PRE={pre}',f'-Ptb_frame_lz77.FOURK={fourk}',
         '-o',str(sim),str(rtl),str(bench)])
    stdout=run([os.environ.get('VVP','vvp'),str(sim),'+input='+str(src),'+output='+str(packet),
                f'+config={mode}','+frames=1',f'+stalls={stalls}'])
    data=packet.read_bytes()
    if name=='fault':
        try:codec.decode_python(data,0,(size,))
        except ValueError:print(stem,'invalid congestion frame correctly rejected',flush=True);records.append(dict(case=stem,fault_rejected=True));continue
        raise AssertionError('congested frame accepted')
    decoded,meta=codec.decode_python(data,1 if pre else 0,(size,))
    stride,line_bytes=(4832,4830) if fourk else (2432,2430)
    expected=codec.quantize(raw,mode,pre,stride,line_bytes) if mode==3 else legacy.quantize(raw,mode,pre,stride,line_bytes)
    assert decoded[:-16]==expected,(stem,'decoded bytes differ')
    golden=(codec.encode_compact_reference(raw,1 if pre else 0,pre,1,0,stride,line_bytes) if mode==3
            else legacy.encode_reference(raw,0,mode,pre,1,0))
    assert data==golden,(stem,'wire bytes differ',len(data),len(golden))
    rejected=0
    for pos in [0,7,28,36,40,45,len(data)//2,len(data)-20,len(data)-4]:
        bad=bytearray(data);bad[pos]^=1
        try:codec.decode_python(bad,1 if pre else 0,(size,))
        except ValueError:rejected+=1
        else:raise AssertionError(('corrupt frame accepted',stem,pos))
    saving=100*(1-len(data)/size)
    if mode==3:assert saving>30,(stem,saving)
    row=dict(case=stem,raw_bytes=size,wire_bytes=len(data),saving_pct=saving,
             meta=meta,source_sha256=hashlib.sha256(raw).hexdigest(),packet_sha256=hashlib.sha256(data).hexdigest(),
             wire_golden_equal=True,reconstruction_equal=True,corrupt_rejected=rejected,stdout=stdout)
    records.append(row);print(stem,round(saving,3),'%',flush=True)
(out/('rtl_full.json' if args.full else 'rtl_small.json')).write_text(json.dumps(dict(scope='CPU RTL simulation on SYNTHETIC/random stress data; byte-count guarantee only, not live sensor fps or detection accuracy',frames=records),indent=2),encoding='utf-8')
