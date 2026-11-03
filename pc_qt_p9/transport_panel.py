"""Independent per-camera FPGA and Qt transport pages; original workbench layout."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QWidget,QFrame,QLabel,QPushButton,QCheckBox,QVBoxLayout,
    QHBoxLayout,QGridLayout,QScrollArea,QSizePolicy)
from click_controls import QComboBox,QSpinBox,QDoubleSpinBox


def caption(text):
    item=QLabel(text);item.setProperty('role','secondary');item.setWordWrap(True);return item


def transport_page(name,title,c):
    area=QScrollArea();area.setWidgetResizable(True);area.setFrameShape(QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);area.setObjectName(name+c)
    content=QWidget();content.setObjectName(name+'Content'+c);area.setWidget(content)
    col=QVBoxLayout(content);col.setContentsMargins(10,8,10,8);col.setSpacing(7)
    area.setStyleSheet('QWidget{font-size:22px;}QComboBox,QSpinBox,QDoubleSpinBox,QPushButton{min-height:26px;padding:4px;}QCheckBox::indicator{width:22px;height:22px;}')
    row=QHBoxLayout();col.addLayout(row)
    heading=QLabel(title);heading.setStyleSheet('color:#28546a;font-weight:700;');row.addWidget(heading,1)
    channel=QLabel('通道 '+c);channel.setStyleSheet('background:#e5f3ed;color:#226b56;padding:3px 8px;border-radius:4px;');row.addWidget(channel)
    return area,col


def build_fpga_page(w,c):
    area,col=transport_page('FpgaTransport','FPGA → RK · 硬件压缩',c)
    col.addWidget(caption('压缩模式'))
    mode=QComboBox()
    for text,value in [('原始数据 · 不压缩','raw'),('LZ77 无损 · 完整源数据','lossless'),
                       ('有损压缩·RAW6/灰度5位','lossy6')]:mode.addItem(text,value)
    mode.setCurrentIndex(1);mode.setEnabled(False);mode.setMinimumWidth(0)
    mode.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon);mode.setMinimumContentsLength(4)
    if hasattr(mode,'setPlaceholderText'):mode.setPlaceholderText('请选择压缩模式')
    col.addWidget(mode);mode.activated.connect(lambda index:w.auto_transport.queue('fpga_codec',c) if index>=0 else None)
    mode.setToolTip('在完整帧边界生效，影响检测及所有显示。有损压缩实际采用 RAW6 / 灰度5位打包，并非LZ77；RAW10最大原始像素误差15/1023，灰度最大误差7/255。全分辨率，不抽像素。无损档逐字节保留原始数据。')
    status=caption('等待 FPGA 编码状态');col.addWidget(status);col.addStretch(1)
    if not hasattr(w,'codec_controls'):w.codec_controls={}
    w.codec_controls[c]=dict(mode=mode,status=status)
    return area


def build_network_page(w,c):
    area,col=transport_page('QtTransport','RK → Qt · 预览传输',c)
    row=QHBoxLayout();col.addLayout(row);row.addWidget(caption('传输方案'))
    preset=QComboBox()
    for text,value in [('高清流畅 · JPEG','quality'),('完整无损 · 高速传输','lossless'),
                       ('低带宽 · JPEG','low'),('自定义参数','custom')]:preset.addItem(text,value)
    preset.setMinimumWidth(0);preset.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon);preset.setMinimumContentsLength(4)
    row.addWidget(preset,2)
    summary=caption('等待传输设置');summary.setMinimumWidth(0);summary.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred);row.addWidget(summary,2)
    advanced_button=QPushButton('高级参数 ▸');advanced_button.setProperty('role','outline');advanced_button.setCheckable(True)
    advanced_button.setStyleSheet('QPushButton{background:transparent;border:none;text-align:left;padding:0;min-height:26px;color:#286cd1;}QPushButton:hover{color:#155fd6;}')
    row=QHBoxLayout();row.addWidget(advanced_button);row.addStretch(1);col.addLayout(row)
    advanced=QWidget();advanced.setObjectName('TransportAdvanced'+c)
    fields=QGridLayout(advanced);fields.setContentsMargins(0,0,0,0);fields.setHorizontalSpacing(10);fields.setVerticalSpacing(5)
    fields.addWidget(caption('发送帧率上限 / fps'),0,0);fields.addWidget(caption('带宽预算 / Mbps'),0,1)
    fps=QSpinBox();fps.setRange(1,60);fps.setValue(60);fps.setKeyboardTracking(False);fields.addWidget(fps,1,0)
    budget=QDoubleSpinBox();budget.setRange(.25,800);budget.setDecimals(2);budget.setValue(300);budget.setKeyboardTracking(False);fields.addWidget(budget,1,1)
    adaptive=QCheckBox('自适应码率');adaptive.setChecked(True);fields.addWidget(adaptive,1,2)
    fields.addWidget(caption('网络编码'),2,0);fields.addWidget(caption('预览尺寸'),2,1)
    codec=QComboBox();codec.addItem('JPEG 有损 · RK 硬件优先','lossy');codec.addItem('完整无损 · 高速传输','lossless');fields.addWidget(codec,3,0)
    width=QComboBox();width.addItem('完整 1080P',1920);width.addItem('低带宽 540P',960);fields.addWidget(width,3,1,1,2)
    for combo in (codec,width):
        combo.setMinimumWidth(0);combo.setMinimumContentsLength(4);combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    fields.setColumnStretch(0,1);fields.setColumnStretch(1,1)
    col.addWidget(advanced);advanced.hide()
    divider=QFrame();divider.setFrameShape(QFrame.HLine);divider.setStyleSheet('color:#d7e2ec;');col.addWidget(divider)
    status=caption('等待网络编码状态');col.addWidget(status);col.addStretch(1)

    def expand(checked):
        advanced.setVisible(checked);advanced_button.setText('高级参数 ▾' if checked else '高级参数 ▸')
    advanced_button.toggled.connect(expand)
    def summary_from_fields(_=None):
        size=width.currentData() or 1920;name='4K' if size==3840 else '1080P' if size==1920 else '540P'
        summary.setText('%s · %dfps目标 · %.0fMbps预算'%(name,fps.value(),budget.value()))
    def codec_changed(_):
        lossless=codec.currentData()=='lossless';width.setEnabled(not lossless)
        full=3840 if (getattr(w,'config',None) or {}).get('capture',{}).get('profile')=='4k30' else 1920
        if lossless:width.setCurrentIndex(width.findData(full))
        summary_from_fields()
    codec.currentIndexChanged.connect(codec_changed);width.currentIndexChanged.connect(summary_from_fields)
    fps.valueChanged.connect(summary_from_fields);budget.valueChanged.connect(summary_from_fields)
    codec.activated.connect(lambda _:w.auto_transport.queue('transport',c))
    width.activated.connect(lambda _:w.auto_transport.queue('transport',c))
    fps.valueChanged.connect(lambda _:w.auto_transport.queue('transport',c));budget.valueChanged.connect(lambda _:w.auto_transport.queue('transport',c))
    adaptive.clicked.connect(lambda _:w.auto_transport.queue('transport',c))
    # Keep the established channel-bound preset action references.
    low=QPushButton(area);high=QPushButton(area);fast=QPushButton(area)
    for action in (low,high,fast):action.hide()
    def apply_preset(key):
        is4k=(getattr(w,'config',None) or {}).get('capture',{}).get('profile')=='4k30'
        if key=='lossless' and is4k:return
        codec.setCurrentIndex(1 if key=='lossless' else 0)
        budget.setValue(800 if key=='lossless' else 2 if key=='low' else 600 if is4k else 300)
        fps.setValue(30 if key=='lossless' else 15 if key=='low' else 30 if is4k else 60)
        width.setCurrentIndex(width.findData(960 if key=='low' else 3840 if is4k else 1920));adaptive.setChecked(True)
        summary_from_fields();w.auto_transport.queue('transport',c)
    low.clicked.connect(lambda _:apply_preset('low'));high.clicked.connect(lambda _:apply_preset('quality'));fast.clicked.connect(lambda _:apply_preset('lossless'))
    def select_preset(index):
        key=preset.itemData(index)
        if key=='custom':advanced_button.setChecked(True)
        else:apply_preset(key)
    preset.activated.connect(select_preset)
    w.net_controls[c]=dict(mode=codec,mbps=budget,fps=fps,max_width=width,adaptive=adaptive,
        low=low,high=high,lossless30=fast,preset=preset,summary=summary,refresh_summary=summary_from_fields,
        advanced=advanced,advanced_button=advanced_button)
    w.net_labels[c]=status
    return area


def sync_transport_controls(w,c):
    """Reflect state without mutating fields or submitting a configuration."""
    fields=w.net_controls[c];selector=fields['preset'];cfg=w.config.get('transport',{}).get(c)
    if not cfg:
        selector.setEnabled(False);fields['advanced_button'].setEnabled(False);return
    is4k=w.config.get('capture',{}).get('profile')=='4k30'
    selector.setEnabled(fields['mode'].isEnabled());fields['advanced_button'].setEnabled(fields['mode'].isEnabled())
    selector.model().item(selector.findData('lossless')).setEnabled(not is4k)
    if not w.auto_transport.editing('transport',c):
        key='custom';full=3840 if is4k else 1920
        if cfg['adaptive']:
            if cfg['mode']=='lossy' and cfg['max_width']==960 and cfg['fps']==15 and cfg['mbps']==2:key='low'
            elif cfg['mode']=='lossy' and cfg['max_width']==full and cfg['fps']==(30 if is4k else 60) and cfg['mbps']==(600 if is4k else 300):key='quality'
            elif not is4k and cfg['mode']=='lossless' and cfg['max_width']==1920 and cfg['fps']==30 and cfg['mbps']==800:key='lossless'
        selector.blockSignals(True);selector.setCurrentIndex(selector.findData(key));selector.blockSignals(False)
    fields['refresh_summary']()
