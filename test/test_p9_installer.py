"""CPU tempfile-only installation verification; no board or services."""
from pathlib import Path
import sys,tempfile,json,hashlib
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'tools'))
from install_p9_candidate import install
with tempfile.TemporaryDirectory() as t:
    base=Path(t)/'base';base.mkdir();stable=base/'capture_modes_candidate';stable.mkdir();(stable/'keep').write_bytes(b'STABLE')
    package=Path(t)/'package';(package/'rk3576_p9/sub').mkdir(parents=True);(package/'rk3576_p9/sub/code.py').write_bytes(b'P9')
    manifest={'rk3576_p9/sub/code.py':hashlib.sha256(b'P9').hexdigest()};(package/'MANIFEST.json').write_text(json.dumps(manifest))
    target=install(package,base);assert (target/'sub/code.py').read_bytes()==b'P9'
    assert install(package,base)==target
    (target/'sub/code.py').write_bytes(b'USER EDIT')
    try:install(package,base)
    except ValueError:pass
    else:raise AssertionError('User files overwritten')
    assert (target/'sub/code.py').read_bytes()==b'USER EDIT' and (stable/'keep').read_bytes()==b'STABLE'
print('P9 INSTALLER PASS: isolated copy, checksum, repeat install, collision refusal, stable files unchanged')
