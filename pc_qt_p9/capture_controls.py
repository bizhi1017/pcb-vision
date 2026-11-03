"""Global acquisition mode controls; network budgets remain per-camera."""
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QFrame,QHBoxLayout,QVBoxLayout,QPushButton,QLabel,QButtonGroup,QRadioButton,QCheckBox

class CaptureModePanel(QFrame):
    requested=pyqtSignal(dict)
    def __init__(self,parent=None,compact=False):
        super().__init__(parent);self.setObjectName('CaptureModePanel')
        self.setStyleSheet('QFrame#CaptureModePanel{background:white;border:1px solid #cbdcf1;border-radius:7px;}')
        self.current=dict(profile='1080p60',channels=['A','B']);self.dirty=False;self.busy=False;self.available=False
        col=QVBoxLayout(self);col.setContentsMargins(12,7,12,7);col.setSpacing(4)
        row=QHBoxLayout();col.addLayout(row);row.addWidget(QLabel('采集模式'))
        self.modes={};self.mode_group=QButtonGroup(self)
        for key,title in (('1080p60','1080P @ 60fps'),('4k30','单路 4K @ 30fps')):
            button=QPushButton(title);button.setCheckable(True);button.setProperty('role','previewMode');self.mode_group.addButton(button);row.addWidget(button)
            self.modes[key]=button;button.clicked.connect(self.edited)
        self.modes['1080p60'].setChecked(True)
        self.dual=QFrame();box=QHBoxLayout(self.dual);box.setContentsMargins(0,0,0,0);box.addWidget(QLabel('启用'))
        self.channels={}
        for c in 'AB':
            field=QCheckBox(c);field.setChecked(True);box.addWidget(field);field.clicked.connect(self.edited);self.channels[c]=field
        row.addWidget(self.dual)
        self.single=QFrame();box=QHBoxLayout(self.single);box.setContentsMargins(0,0,0,0);box.addWidget(QLabel('4K通道'))
        self.camera_group=QButtonGroup(self);self.cameras={}
        for c in 'AB':
            field=QRadioButton(c);self.camera_group.addButton(field);box.addWidget(field);self.cameras[c]=field;field.clicked.connect(self.edited)
        self.cameras['A'].setChecked(True);self.single.hide();row.addWidget(self.single)
        row.addStretch(1);self.apply=QPushButton('应用模式');self.apply.setProperty('role','primary');self.apply.clicked.connect(self.submit)
        self.status=QLabel('正在获取采集能力');self.status.setProperty('role','secondary')
        if compact:
            footer=QHBoxLayout();footer.setContentsMargins(0,0,0,0);col.addLayout(footer)
            self.status.setWordWrap(True);footer.addWidget(self.status,1);footer.addWidget(self.apply)
        else:
            row.addWidget(self.apply);col.addWidget(self.status)
        self.apply.setEnabled(False);self.modes['4k30'].setEnabled(False)

    def target(self):
        mode='4k30' if self.modes['4k30'].isChecked() else '1080p60'
        channels=[c for c in 'AB' if (self.cameras[c] if mode=='4k30' else self.channels[c]).isChecked()]
        return dict(profile=mode,channels=channels)

    def edited(self):
        self.dirty=True;is4k=self.modes['4k30'].isChecked();self.single.setVisible(is4k);self.dual.setVisible(not is4k)
        self.apply.setEnabled(self.available and not self.busy and bool(self.target()['channels']) and self.target()!=self.current)

    def submit(self):
        if not self.apply.isEnabled():return
        self.busy=True;self.apply.setEnabled(False);self.requested.emit(self.target())

    def failed(self,message):
        self.busy=False;self.status.setText('切换请求失败：'+message);self.edited()

    def update_state(self,state):
        capture=state.get('capture',{});self.current=state.get('config',{}).get('capture',dict(profile='1080p60',channels=['A','B']))
        running=capture.get('state','running')=='running';self.busy=not running;self.available=bool(capture.get('switchable'))
        profiles=capture.get('profiles',['1080p60']);reason=capture.get('reason','当前服务不支持采集档位切换')
        if self.target()==self.current:self.dirty=False
        if not self.dirty:
            mode=self.current['profile'];self.modes[mode].setChecked(True)
            if mode=='1080p60':
                for c in 'AB':self.channels[c].setChecked(c in self.current['channels'])
            if mode=='4k30':self.cameras[self.current['channels'][0]].setChecked(True)
        for mode,field in self.modes.items():field.setEnabled(running and (mode=='1080p60' or mode in profiles));field.setToolTip(reason if mode not in profiles else '切换真实传感器采集档位')
        for field in list(self.channels.values())+list(self.cameras.values()):field.setEnabled(running and self.available)
        is4k=self.modes['4k30'].isChecked();self.single.setVisible(is4k);self.dual.setVisible(not is4k)
        self.apply.setEnabled(running and self.available and bool(self.target()['channels']) and self.target()!=self.current)
        text=('切换中，请等待完整画面…' if not running and capture.get('state')!='error' else '采集异常：'+capture.get('error','') if capture.get('state')=='error' else
              ('4K · 30fps目标' if self.current['profile']=='4k30' else '1080P · 60fps目标')+' · 运行 '+ '、'.join(self.current['channels']))
        if running and capture.get('error'):text+=' · '+capture['error']
        if self.current['profile']=='4k30':text+=' · 源3840×2160 / HDMI1080P · 彩色原图'
        self.status.setText(text);self.status.setToolTip(reason)
