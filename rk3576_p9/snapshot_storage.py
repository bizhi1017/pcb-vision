"""Verified SD-only screenshot storage. Never fall back to the system disk."""
import argparse
from datetime import datetime,timezone,timedelta
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid


class StorageError(ValueError):pass


SNAPSHOT_NAME=re.compile(r'(?P<camera>[AB])_划痕(?P<scratch>[0-9]+)\+缺失(?P<missing>[0-9]+)\+污渍(?P<stain>[0-9]+)_(?P<day>[0-9]{8})_[0-9]{6}_[0-9]{6}_f[0-9]+_[0-9a-f]{32}\.png')


def _unescape(value):
    return re.sub(r'\\([0-7]{3})',lambda m:chr(int(m[1],8)),value)


def discover_sd(sysroot=Path('/sys/class/block'),mountinfo=Path('/proc/self/mountinfo')):
    try:
        cards=[p for p in sysroot.glob('mmcblk*') if (p/'device/type').is_file() and (p/'device/type').read_text().strip()=='SD']
        if not cards:return dict(mounted=False,writable=False,error='未检测到SD卡')
        mounts={};root_devices=set()
        for line in mountinfo.read_text().splitlines():
            fields=line.split();sep=fields.index('-')
            if fields[4]=='/':root_devices.add(fields[2])
            mounts[fields[2]]=dict(mount=_unescape(fields[4]),options=fields[5].split(','),filesystem=fields[sep+1])
        candidates=[];mounted=[]
        for card in cards:
            parts=[p for p in sysroot.glob(card.name+'p*') if (p/'partition').is_file()] or [card]
            for part in parts:
                device='/dev/'+part.name;dev=(part/'dev').read_text().strip()
                if dev in root_devices:continue
                candidates.append(device)
                info=mounts.get(dev)
                if info and info['mount']!='/':mounted.append(dict(device=device,dev=dev,**info))
        if len(mounted)>1:return dict(mounted=False,writable=False,error='检测到多个已挂载SD分区，请只保留一个截图SD分区')
        if not mounted:return dict(mounted=False,writable=False,devices=candidates,error='SD卡尚未挂载，请运行新版启动脚本或 sh prepare_sd_storage.sh')
        info=mounted[0];v=os.statvfs(info['mount']);writable='rw' in info['options'] and os.access(info['mount'],os.W_OK)
        return dict(mounted=True,writable=writable,path=info['mount'],device=info['device'],dev=info['dev'],filesystem=info['filesystem'],
                    free_bytes=v.f_bavail*v.f_frsize,total_bytes=v.f_blocks*v.f_frsize,
                    snapshot_root=str(Path(info['mount'])/'PCB_Snapshots'),error='' if writable else 'SD卡已挂载为只读或不可写')
    except (OSError,ValueError) as exc:return dict(mounted=False,writable=False,error='SD卡检查失败：'+str(exc))


def prepare_sd(target=Path('/mnt/pcb_sd')):
    info=discover_sd()
    if info['mounted']:return info
    devices=info.get('devices',[])
    if not devices:raise StorageError(info['error'])
    usable=[]
    for device in devices:
        result=subprocess.run(['blkid','-s','TYPE','-o','value',device],capture_output=True,text=True,timeout=4)
        if result.returncode==0 and result.stdout.strip() in ('vfat','exfat','ext4','ext3','ext2'):usable.append(device)
    if len(usable)!=1:raise StorageError('需要一个已有文件系统的SD分区；不会格式化SD卡')
    if os.geteuid()!=0:raise StorageError('挂载SD卡需要root，请在PuTTY以root运行准备脚本')
    if target.is_symlink() or os.path.ismount(target):raise StorageError('SD挂载目录被占用：'+str(target))
    target.mkdir(parents=True,exist_ok=True)
    if any(target.iterdir()):raise StorageError('SD挂载目录非空，不覆盖已有文件：'+str(target))
    result=subprocess.run(['mount','-o','nosuid,nodev,noexec',usable[0],str(target)],capture_output=True,text=True,timeout=10)
    if result.returncode:raise StorageError('挂载SD卡失败：'+result.stderr.strip())
    info=discover_sd()
    if not info['mounted']:raise StorageError(info['error'])
    return info


def snapshot_name(camera,sequence,counts,client_time=None):
    if camera not in ('A','B'):raise ValueError('invalid camera')
    if type(sequence)is not int or sequence<0:raise ValueError('invalid snapshot sequence')
    if (not isinstance(counts,dict) or set(counts)!={'scratch','missing','stain'}
        or any(type(value)is not int or value<0 for value in counts.values())):raise ValueError('invalid defect counts')
    if client_time is not None:
        if not isinstance(client_time,str) or len(client_time)>64:raise ValueError('invalid client time')
        stamp=datetime.fromisoformat(client_time.replace('Z','+00:00'))
        if stamp.tzinfo is None:raise ValueError('client time requires timezone')
    else:stamp=datetime.now(timezone.utc)
    stamp=stamp.astimezone(timezone(timedelta(hours=8)))
    day=stamp.strftime('%Y%m%d')
    return day,'%s_划痕%d+缺失%d+污渍%d_%s_f%d_%s.png'%(camera,counts['scratch'],counts['missing'],counts['stain'],stamp.strftime('%Y%m%d_%H%M%S_%f'),sequence,uuid.uuid4().hex)


class SnapshotStore:
    def __init__(self,probe=discover_sd):
        self.probe=probe;self.lock=threading.Lock();self.cached=None;self.checked=0.

    def status(self):
        with self.lock:
            if self.cached is None or time.monotonic()-self.checked>1:
                self.cached=self.probe();self.checked=time.monotonic()
            return dict(self.cached)

    def save(self,day,filename,png):
        match=SNAPSHOT_NAME.fullmatch(filename) if isinstance(filename,str) else None
        if not isinstance(day,str) or not match or match['day']!=day:raise StorageError('无效截图文件名或日期')
        try:datetime.strptime(day,'%Y%m%d')
        except ValueError as exc:raise StorageError('无效截图日期') from exc
        info=self.probe()
        if not info.get('mounted') or not info.get('writable'):raise StorageError(info.get('error') or 'SD卡未就绪')
        major,minor=map(int,info['dev'].split(':'));expected=os.makedev(major,minor)
        flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
        descriptors=[];created=False;file_fd=None
        try:
            fd=os.open(info['path'],flags);descriptors.append(fd)
            if os.fstat(fd).st_dev!=expected:raise StorageError('SD卡挂载已变化，请重新检查')
            camera=match['camera']
            for component in ('PCB_Snapshots',day):
                try:os.mkdir(component,dir_fd=fd)
                except FileExistsError:pass
                fd=os.open(component,flags,dir_fd=fd);descriptors.append(fd)
                if os.fstat(fd).st_dev!=expected:raise StorageError('SD截图目录指向了其他磁盘')
            for channel in ('A','B'):
                try:os.mkdir(channel,dir_fd=fd)
                except FileExistsError:pass
            fd=os.open(camera,flags,dir_fd=fd);descriptors.append(fd)
            if os.fstat(fd).st_dev!=expected:raise StorageError('SD截图目录指向了其他磁盘')
            space=os.fstatvfs(fd)
            if space.f_bavail*space.f_frsize<len(png)+16*1024*1024:raise StorageError('SD卡空间不足，至少保留16MB余量')
            file_fd=os.open(filename,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644,dir_fd=fd);created=True
            with os.fdopen(file_fd,'wb') as output:
                file_fd=None;output.write(png);output.flush();os.fsync(output.fileno())
            os.fsync(fd)
            return str(Path(info['path'])/'PCB_Snapshots'/day/camera/filename)
        except (OSError,StorageError) as exc:
            if file_fd is not None:os.close(file_fd)
            if created:
                try:os.unlink(filename,dir_fd=fd)
                except OSError:pass
            raise StorageError('SD卡保存失败：'+str(exc)) from exc
        finally:
            for descriptor in reversed(descriptors):os.close(descriptor)
            with self.lock:self.checked=0.


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--prepare',action='store_true');args=p.parse_args()
    try:
        state=prepare_sd() if args.prepare else discover_sd()
        print('SD:',state.get('device',''),state.get('path',''),state.get('error',''))
        if not state.get('mounted'):raise SystemExit(1)
    except (StorageError,subprocess.TimeoutExpired) as exc:
        print('SD:',exc);raise SystemExit(1)
