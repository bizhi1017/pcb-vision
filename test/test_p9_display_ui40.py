"""CPU/offscreen checks of display convention and menu/physical mode separation."""
from pathlib import Path
import os,sys,copy,json,tempfile,time
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen';os.environ['QT_QPA_FONTDIR']='C:/Windows/Fonts'
root=Path(__file__).resolve().parents[1];sys.path[:0]=[str(root/'pc_qt_p9'),str(root/'rk3576_p9')]
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QSettings,QThread
from PyQt5.QtGui import QFontDatabase,QImage
from station_telemetry import fpga_compression,displayed_fpga_compression,measurements,TelemetryPanel
from station_theme import STYLE
from dual_station import DualWindow
from dual_camera_server import Hub
from fpga_codec import MODES
checks=[]
def check(ok,name):assert ok,name;checks.append(name)
row=dict(online=True,fpga_codec=dict(mode='lossless',raw_bytes=1000,wire_bytes=1015,saving_pct=-1.5))
check(abs(fpga_compression(row)+1.5)<1e-9 and displayed_fpga_compression(row)==0,'fixed lossless display preserves actual negative byte reduction')
positive=copy.deepcopy(row);positive['fpga_codec']['wire_bytes']=600
check(fpga_compression(positive)==40 and displayed_fpga_compression(positive)==0,'lossless convention is fixed even for positive actual savings')
for p in (dict(online=False),dict(enabled=False),dict(error='capture error'),dict(fpga_codec=dict(mode='lossless'))):
    check(displayed_fpga_compression(dict(row,**p)) is None,'invalid/offline data stays missing, never a fabricated fixed-zero sample')
mixed=dict(config=dict(detect={}),channels=dict(A=copy.deepcopy(row),B=dict(online=True,fpga_codec=dict(mode='lossy6',raw_bytes=1000,wire_bytes=625))))
before=copy.deepcopy(mixed)
check(measurements(mixed,{},dict(A=True))['compression']==[0,37.5],'actual modes drive independent display and preview pause does not alter capture telemetry')
check(mixed==before,'telemetry does not overwrite incoming actual mode, bytes or saving_pct')
check(MODES.index('lossy6')==3,'backend physical register mode remains 3 despite GUI item becoming index 2')
app=QApplication([]);QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
e=root/'test/inspection/ui40_display';e.mkdir(parents=True,exist_ok=True)
panel=TelemetryPanel();panel.setStyleSheet(STYLE);panel.resize(1440,170);panel.show();panel.update_state(mixed,{},{});app.processEvents()
metric=panel.cards['compression']
check('0.0' in metric.value.text() and '37.5' in metric.value.text(),'zero and actual lossy reduction render together')
check('显示口径见提示' in metric.hint.text() and '显示基准' in metric.toolTip(),'visible hint points to the fully disclosed display convention')
check('1015 B' in metric.toolTip() and '-1.50%' in metric.toolTip() and '展示' in metric.toolTip(),'tooltip retains actual counters and negative savings alongside zero convention')
metric.grab().save(str(e/'compression_card.png'));panel.close()
hub=Hub(model=object())
class Pre:
    codec_enabled=True
    def apply_codec(self,*args):pass
hub.pre_control=Pre()
def state(mode='lossless'):
    s=hub.status();s['capture'].update(build_id='0x5630503b')
    for c in 'AB':
        s['config']['fpga_codec'][c]=mode
        s['channels'][c].update(online=True,fpga_codec=dict(mode=mode,epoch=1,raw_bytes=1000,wire_bytes=1015 if mode=='lossless' else 625,saving_pct=-1.5 if mode=='lossless' else 37.5,decode_ms=2.))
    return s
def settle():
    until=time.monotonic()+.22
    while time.monotonic()<until:app.processEvents();time.sleep(.005)
with tempfile.TemporaryDirectory() as temp,patch.object(QThread,'start'):
    w=DualWindow('http://127.0.0.1:1',QSettings(str(Path(temp)/'settings.ini'),QSettings.IniFormat));w.refresh_browser_address=lambda:None;requests=[]
    def command(path,body,callback=None,error_callback=None):requests.append(copy.deepcopy(body))
    w.command=command;w.show();w.state(state());settle()
    for c in 'AB':
        combo=w.codec_controls[c]['mode']
        check([combo.itemData(i) for i in range(combo.count())]==['raw','lossless','lossy6'],'only original, lossless and lossy6 appear; routing IDs preserved')
        check(combo.itemText(2)=='有损压缩·RAW6/灰度5位' and '并非LZ77' in combo.toolTip(),'lossy label exposes RAW6/GRAY5 precision and identifies bit-packing in tooltip')
        check('无损显示 0.0%' in w.codec_controls[c]['status'].text() and '固定' not in w.codec_controls[c]['status'].text(),'per-channel lossless display matches top card without the removed wording')
        check('-1.50%' in w.codec_controls[c]['status'].toolTip(),'per-channel tooltip preserves true savings')
    w.state(state('lossy9'));settle()
    check(not requests and all(v['mode'].currentIndex()==-1 for v in w.codec_controls.values()),'removed active old mode produces placeholder without silently changing hardware')
    check(all('旧档位' in v['status'].text() for v in w.codec_controls.values()),'operator can see that the removed old mode is still active')
    combo=w.codec_controls['B']['mode'];combo.activated.emit(-1);settle();check(not requests,'placeholder cannot submit an empty FPGA mode')
    combo.setCurrentIndex(2);combo.activated.emit(2);settle()
    check(requests==[{'fpga_codec':{'B':'lossy6'}}],'new visible index 2 still sends lossy6 only to B')
    w.state(state('lossy6'));settle();w.resize(1440,1000)
    image=QImage(1920,1080,QImage.Format_RGB888);image.fill(0xff557788)
    for c in 'AB':w.views[c].set_image(image);w.channel_tabs[c].setCurrentIndex(1)
    settle();w.grab().save(str(e/'lossy_menu_workbench.png'));w.close();settle()
hub.stop.set()
(e/'display_checks.json').write_text(json.dumps(dict(scope='CPU and Qt offscreen with synthetic states only, no board/network control/SD/hardware operations',checks=checks),ensure_ascii=False,indent=2),encoding='utf-8')
print('P9 UI40 DISPLAY PASS:',len(checks),'checks')
