"""Install files into a NEW candidate directory. Never operates hardware."""
from pathlib import Path,PurePosixPath
import argparse,hashlib,json,shutil
def install(package,base):
    package=Path(package).resolve();base=Path(base).resolve()
    target=base/'p9_compact_candidate'
    manifest=json.loads((package/'MANIFEST.json').read_text(encoding='utf-8'))
    prefix='rk3576_p9/'
    entries={name[len(prefix):]:digest for name,digest in manifest.items() if name.startswith(prefix)}
    if not entries:raise ValueError('No board files in manifest')
    for name,digest in entries.items():
        rel=PurePosixPath(name)
        if rel.is_absolute() or '..' in rel.parts or '\\' in name:raise ValueError('Unsafe package path')
        source=package/'rk3576_p9'/name
        if hashlib.sha256(source.read_bytes()).hexdigest()!=digest:raise ValueError('Package checksum mismatch: '+name)
    if target.exists():
        # Idempotent only when this exact candidate is already installed.
        for name,digest in entries.items():
            path=target/name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                raise ValueError('Existing candidate differs; nothing overwritten: '+str(target))
        print('Exact P9 candidate already installed. No files changed.');return target
    target.mkdir(parents=True)
    for name in entries:
        dest=target/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(package/'rk3576_p9'/name,dest)
    for name,digest in entries.items():assert hashlib.sha256((target/name).read_bytes()).hexdigest()==digest
    print('P9 software installed and verified: '+str(target))
    print('No service/driver/PCIe/FPGA/SD operations performed. Follow doc/99 for manual activation.')
    return target
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base',type=Path,default=Path('/home/rock/dual_camera'));args=p.parse_args()
    install(Path(__file__).resolve().parents[1],args.base)
