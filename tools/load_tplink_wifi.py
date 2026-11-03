"""Validate staged TP-Link 2357:0147 files; hardware activation requires --load."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time


def devices(vendor, product):
    found=[]
    for p in Path('/sys/bus/usb/devices').iterdir():
        try:
            if p.joinpath('idVendor').read_text().strip()==vendor and p.joinpath('idProduct').read_text().strip()==product:
                found.append(p)
        except (FileNotFoundError,NotADirectoryError):pass
    return found


def wireless_interfaces(device, net_root=Path('/sys/class/net')):
    """Drivers may attach net/ to the USB device or to an interface beneath it."""
    root=device.resolve()
    return [p.name for p in net_root.iterdir()
            if p.resolve().is_relative_to(root)
            and (p.joinpath('wireless').is_dir() or p.joinpath('phy80211').exists())]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--load',action='store_true',help='activate this USB Wi-Fi adapter only')
    args=parser.parse_args()
    base=Path(__file__).resolve().parent
    if platform.release()!='6.1.99' or platform.machine()!='aarch64':
        raise RuntimeError('This build is only for ARM64 Linux 6.1.99')
    manifest=json.loads((base/'SHA256.json').read_text())
    for name,wanted in manifest.items():
        p=(base/name).resolve()
        if not p.is_relative_to(base) or hashlib.sha256(p.read_bytes()).hexdigest()!=wanted:
            raise RuntimeError('File validation failed: '+name)
    loader=base/'modules/aic_load_fw.ko';driver=base/'modules/aic8800_fdrv.ko'
    for p in (loader,driver):
        version=subprocess.check_output(['modinfo','-F','vermagic',str(p)],text=True).strip()
        if version!='6.1.99 SMP mod_unload aarch64':raise RuntimeError('Module ABI differs: '+version)
    alias=subprocess.check_output(['modinfo','-F','alias',str(driver)],text=True)
    if 'usb:v2357p0147' not in alias:raise RuntimeError('TP-Link USB ID is missing')
    radio=devices('2357','0147');storage=devices('a69c','5721')
    candidates=radio or storage
    if len(candidates)!=1 or '/23000000.usb/' not in str(candidates[0].resolve()):
        raise RuntimeError('Expected one adapter on independent USB0/J4')
    print('File hashes, ARM64 kernel ABI, TP-Link USB ID and USB0 path verified.',flush=True)
    if not args.load:
        print('Check only: no mode switch, driver load or network change.')
        return
    if os.geteuid()!=0:raise RuntimeError('Run as root')
    if not radio:
        # User explicitly runs --load; only the identified USB0 storage device is switched.
        subprocess.run(['usb_modeswitch','-v','0xa69c','-p','0x5721','-K'],check=True)
        for _ in range(50):
            time.sleep(.1);radio=devices('2357','0147')
            if radio:break
        if len(radio)!=1:raise RuntimeError('Adapter did not return as 2357:0147; inspect lsusb')
    fw=str(base/'firmware')
    if len(fw)+len('/aic8800DC')>=200:raise RuntimeError('Firmware path too long')
    if Path('/sys/module/aic_load_fw').exists():
        current=Path('/sys/module/aic_load_fw/parameters/aic_fw_path').read_text().strip()
        if current!=fw:raise RuntimeError('Another AIC driver is loaded; do not overwrite it')
    else:subprocess.run(['insmod',str(loader),'aic_fw_path='+fw],check=True)
    if not Path('/sys/module/aic8800_fdrv').exists():
        subprocess.run(['insmod',str(driver)],check=True)
    for _ in range(100):
        for name in wireless_interfaces(radio[0]):
            print('Wi-Fi interface ready: '+name,flush=True)
            subprocess.run(['nmcli','device','status'],check=False)
            return
        time.sleep(.1)
    raise RuntimeError('No wireless interface after loading; inspect dmesg | tail -n 60')


if __name__=='__main__':
    try:main()
    except (RuntimeError,OSError,subprocess.SubprocessError) as exc:
        print('Wi-Fi setup stopped: '+str(exc),file=sys.stderr)
        sys.exit(1)
