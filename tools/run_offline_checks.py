"""CPU-only checks, no hardware or real board access."""
from pathlib import Path
import ast,json,os,re,sys,subprocess
r=Path(__file__).resolve().parents[1]
for p in r.rglob('*.py'):
    if '__pycache__' not in p.parts:ast.parse(p.read_text(encoding='utf-8-sig'),filename=str(p))
project=r/'fpga/p9_compact/afc03_compact_p9.al'
missing=[v for v in re.findall(r'<File Path="([^"]+)"',project.read_text(encoding='utf-8')) if not (project.parent/v).is_file()]
external=json.loads((r/'fpga/external_files.json').read_text(encoding='utf-8'))
declared={v['path'].removeprefix('fpga/p9_compact/') for v in external['files']}
assert set(missing)<=declared,('undeclared missing FPGA files',set(missing)-declared)
sys.path.insert(0,str(r/'rk3576_p9'))
import fpga_codec as codec
raw=(bytes(range(256))*65)[:16400]
packet=codec.encode_compact_reference(raw,0,0,1,0,2432,2430)
decoded,meta=codec.decode_python(packet,0,(len(raw),))
assert decoded[:-16]==codec.quantize(raw,3,0,2432,2430)
env=dict(os.environ,QT_QPA_PLATFORM='offscreen',PYTHONUTF8='1')
for n in ('test_p9_installer.py','test_p9_measurement_dependency.py','test_p9_display_ui40.py'):
    p=subprocess.run([sys.executable,str(r/'test'/n)],cwd=r,env=env,timeout=60)
    if p.returncode:raise SystemExit(p.returncode)
print('OFFLINE PASS: Python syntax, declared FPGA dependencies, compact codec roundtrip, isolated installer, measurement HTTP and offscreen ui40. Missing licensed FPGA dependencies:',len(missing))
