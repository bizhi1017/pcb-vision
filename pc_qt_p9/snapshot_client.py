"""Local screenshot writing; same verified PNG bytes as the board SD copy."""
import base64
import hashlib
import os
from pathlib import Path
import re
from datetime import datetime


SNAPSHOT_NAME=re.compile(r'(?P<camera>[AB])_划痕(?P<scratch>[0-9]+)\+缺失(?P<missing>[0-9]+)\+污渍(?P<stain>[0-9]+)_(?P<day>[0-9]{8})_[0-9]{6}_[0-9]{6}_f[0-9]+_[0-9a-f]{32}\.png')


def save_local_snapshot(directory,result):
    name=result.get('filename','');day=result.get('date','')
    match=SNAPSHOT_NAME.fullmatch(name) if isinstance(name,str) else None
    if not match:raise ValueError('截图文件名不符合缺陷数量规范，请更新并重启板端截图服务')
    if not isinstance(day,str) or day!=match['day']:raise ValueError('截图日期无效')
    datetime.strptime(day,'%Y%m%d')
    if result.get('camera')!=match['camera'] or result.get('kind')!='detection':raise ValueError('截图相机或检测结果无效')
    counts=result.get('defect_counts')
    if (not isinstance(counts,dict) or set(counts)!={'scratch','missing','stain'}
        or any(type(counts[key])is not int or counts[key]!=int(match[key]) for key in counts)):
        raise ValueError('截图文件名与缺陷数量不一致，未保存')
    data=base64.b64decode(result.get('png') or '',validate=True)
    if not data.startswith(b'\x89PNG\r\n\x1a\n') or hashlib.sha256(data).hexdigest()!=result.get('sha256'):raise ValueError('截图数据校验失败，未保存')
    date_folder=Path(directory).expanduser().resolve()/day
    for camera in ('A','B'):(date_folder/camera).mkdir(parents=True,exist_ok=True)
    folder=date_folder/match['camera']
    path=folder/name;created=False
    try:
        with path.open('xb') as output:
            created=True;output.write(data);output.flush();os.fsync(output.fileno())
    except Exception:
        if created:path.unlink(missing_ok=True)
        raise
    return str(path)
