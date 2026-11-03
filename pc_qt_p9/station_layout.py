"""Presentation only: workstation layout, existing actions remain in DualWindow."""
from pathlib import Path
from PyQt5.QtCore import Qt,QSize,QTimer,QUrl
from PyQt5.QtGui import QIcon,QFont,QDesktopServices
from PyQt5.QtWidgets import (QWidget,QFrame,QLabel,QPushButton,QComboBox,QLineEdit,QSpinBox,QCheckBox,
    QVBoxLayout,QHBoxLayout,QGridLayout,QTabWidget,QScrollArea,QSplitter,QPlainTextEdit,QSizePolicy,QDoubleSpinBox,QButtonGroup)
from station_theme import STYLE,mark_pixmap,set_property,camera_icon
from station_telemetry import TelemetryPanel
from mobile_qr import MobileQrPanel
from click_controls import QComboBox,QSpinBox,QDoubleSpinBox


def label(text,role=None):
    result=QLabel(text)
    if role:result.setProperty('role',role)
    return result


def button(text,role=None):
    result=QPushButton(text)
    if role:result.setProperty('role',role)
    return result


def page():
    widget=QWidget();layout=QVBoxLayout(widget);layout.setContentsMargins(10,9,10,9);layout.setSpacing(6)
    return widget,layout


def settings_page():
    """Keep controls scrollable inside a camera's fixed-height settings area."""
    area=QScrollArea();area.setWidgetResizable(True);area.setFrameShape(QScrollArea.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    content,layout=page();area.setWidget(content)
    return area,layout


class PreviewWell(QFrame):
    """Keep the preview at its image aspect; extra card height goes to controls."""
    def __init__(self,view):
        super().__init__();self.view=view;self.setObjectName('PreviewWell')
        policy=QSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed);policy.setHeightForWidth(True);self.setSizePolicy(policy)
        col=QVBoxLayout(self);col.setContentsMargins(0,0,0,0);col.addWidget(view,1)
        self.placeholder=None;self.height_update_pending=False

    def resizeEvent(self,event):
        super().resizeEvent(event)
        height=self.heightForWidth(self.width())
        if (self.minimumHeight()!=height or self.maximumHeight()!=height) and not self.height_update_pending:
            self.height_update_pending=True;QTimer.singleShot(0,self.sync_height)

    def sync_height(self):
        self.height_update_pending=False;height=self.heightForWidth(self.width())
        if self.minimumHeight()!=height or self.maximumHeight()!=height:self.setFixedHeight(height)

    def take_view(self):
        if self.placeholder is None:
            self.placeholder=QPushButton('相机 '+self.view.camera+' 正在放大查看\n点击返回双路预览',self)
            self.placeholder.setMinimumHeight(180)
            self.placeholder.setStyleSheet('background:#e4ebf2;color:#385571;border:none;font-size:19px;')
            self.placeholder.clicked.connect(lambda:self.view.enlargeRequested.emit())
        self.layout().removeWidget(self.view)
        self.layout().addWidget(self.placeholder,1);self.placeholder.show()
        return self.view

    def restore_view(self):
        if self.placeholder is not None:
            self.layout().removeWidget(self.placeholder);self.placeholder.hide()
        self.layout().addWidget(self.view,1);self.view.show()

    def hasHeightForWidth(self):return True

    def heightForWidth(self,width):return self.view.heightForWidth(min(width,self.view.maximumWidth()))

    def sizeHint(self):return self.view.sizeHint()

    def minimumSizeHint(self):return QSize(320,180)


class WorkspaceSplitter(QSplitter):
    """Keep the output/log dock within one fifth, including after dragging."""
    def __init__(self):
        super().__init__(Qt.Vertical);self.limit_pending=False

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if not self.limit_pending:
            self.limit_pending=True;QTimer.singleShot(0,self.limit_bottom)

    def limit_bottom(self):
        self.limit_pending=False
        if self.count()<2:return
        cap=max(1,self.height()//5);bottom=self.widget(1)
        if bottom.maximumHeight()!=cap:bottom.setMaximumHeight(cap)
        if self.sizes()[1]>cap:self.setSizes([self.height()-cap-self.handleWidth(),cap])

    def initial_sizes(self):
        height=self.height();dock=min(160,max(100,height//7))
        self.setSizes([max(1,height-dock-self.handleWidth()),dock]);self.limit_bottom()


def build_layout(w,video_class):
    w.setStyleSheet(STYLE);w.setWindowIcon(QIcon(mark_pixmap()))
    splitter=WorkspaceSplitter();w.setCentralWidget(splitter);w.workspace_splitter=splitter;splitter.setChildrenCollapsible(False)
    scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QScrollArea.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);scroll.setMinimumHeight(300);splitter.addWidget(scroll)
    workspace=QWidget();workspace.setObjectName('Workspace');scroll.setWidget(workspace)
    col=QVBoxLayout(workspace);col.setContentsMargins(14,12,14,10);col.setSpacing(8)

    header=QFrame();header.setObjectName('AppHeader');head=QHBoxLayout(header);head.setContentsMargins(18,8,18,8);head.setSpacing(13)
    logo=QLabel();logo.setPixmap(mark_pixmap());logo.setFixedSize(44,44);head.addWidget(logo)
    titles=QVBoxLayout();titles.setSpacing(3);head.addLayout(titles)
    title=QLabel('PCB 双路视觉检测台');title.setObjectName('HeaderTitle');titles.addWidget(title)
    subtitle=QLabel('PCB VISION  /  DUAL CAMERA INSPECTION');subtitle.setObjectName('HeaderSub');titles.addWidget(subtitle)
    head.addStretch(1)
    address=QLabel(w.base.replace('http://','').replace('https://',''));address.setObjectName('HeaderAddress');head.addWidget(address)
    w.connection_badge=QLabel('正在连接设备');w.connection_badge.setObjectName('ConnectionBadge');head.addWidget(w.connection_badge)
    col.addWidget(header)

    w.telemetry=TelemetryPanel();col.addWidget(w.telemetry)

    cameras=QHBoxLayout();cameras.setSpacing(12);col.addLayout(cameras,1);w.preview_wells={};w.channel_badges={};w.channel_tabs={};w.net_controls={};w.net_labels={};w.net_initialized=set();w.net_seen={};w.preview_modes={}
    for c in ('A','B'):
        card=QFrame();card.setProperty('role','cameraCard');layout=QVBoxLayout(card);layout.setContentsMargins(12,12,12,12);layout.setSpacing(7);cameras.addWidget(card,1)
        live=QHBoxLayout();live.setSpacing(10);layout.addLayout(live)
        view=video_class('等待相机 '+c,camera=c);well=PreviewWell(view);live.addWidget(well,4);w.views[c]=view
        w.preview_wells[c]=well;view.enlargeRequested.connect(lambda c=c:w.enlarge_preview(c))
        w.channel_badges[c]=view.status_badge
        aside=QWidget();aside.setFixedWidth(210);side=QVBoxLayout(aside);side.setContentsMargins(0,0,0,0);side.setSpacing(10);live.addWidget(aside)
        stats=label('设备连接中…','cameraStats');stats.setWordWrap(True);side.addWidget(stats);w.labels[c]=stats
        detection=label('三类检测：等待设备','detectionStats');detection.setWordWrap(True);side.addWidget(detection);w.detection_labels[c]=detection
        side.addStretch(1)

        actions=QHBoxLayout();actions.setSpacing(5);layout.addLayout(actions)
        preview=button('暂停本路预览','previewToggle');preview.setCheckable(True);preview.setToolTip('仅控制本路Qt预览；关闭时黑底白字，采集、检测与HDMI按当前配置运行')
        preview.toggled.connect(lambda paused,c=c:w.preview(c,paused));actions.addWidget(preview,1);w.previews[c]=preview
        detect=button('检测: --','stateToggle');detect.setEnabled(False);detect.clicked.connect(lambda _,c=c:w.toggle('detect',c));actions.addWidget(detect,1);w.detect[c]=detect
        position=button('位置提醒','stateToggle');position.setProperty('tint','green');position.setCheckable(True)
        position.setChecked(w.settings.value('position_guide/'+c,True,type=bool));set_property(position,'active',position.isChecked())
        position.setAccessibleName('PCB 位置提醒 '+c)
        position.setToolTip('PCB位置提醒开关：浅色背景上的深色PCB，按画面提示上下左右移动；与缺陷检测开关独立')
        position.toggled.connect(lambda _,c=c:w.toggle_position_guide(c));actions.addWidget(position,1);w.position_checks[c]=position
        measure=button('测量 '+c,'outline');measure.setEnabled(False);measure.clicked.connect(lambda _,c=c:w.start_measurement(c));actions.addWidget(measure,1);w.measure[c]=measure
        push=button('Wi-Fi 推送: --','stateToggle');push.setProperty('tint','green');push.setEnabled(False);push.clicked.connect(lambda _,c=c:w.toggle_browser_push(c));actions.addWidget(push,1);w.push[c]=push
        push.setToolTip('控制本路向所有手机网页提供画面；不影响Qt、HDMI和检测')
        inspect=button('缺陷放大','outline');inspect.setEnabled(False);inspect.clicked.connect(lambda _,c=c:w.inspect_defects(c));actions.addWidget(inspect,1);w.inspect[c]=inspect

        tabs=QTabWidget();tabs.setObjectName('CameraSettings');tabs.setDocumentMode(True);tabs.setMinimumHeight(160)
        tabs.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding);layout.addWidget(tabs,1);w.channel_tabs[c]=tabs
        image,image_col=settings_page();tabs.addTab(image,'图像调节')
        pre=QHBoxLayout();image_col.addLayout(pre);pre.addWidget(label('FPGA 预处理','secondary'))
        modes=QComboBox();modes.addItems(['彩色原图','灰度','高斯滤波','Sobel 边缘','直方图均衡']);pre.addWidget(modes,1);w.pre_modes[c]=modes
        pre.addWidget(label('阈值','secondary'));threshold=QSpinBox();threshold.setRange(0,2040);threshold.setValue(200);threshold.setSingleStep(20)
        threshold.setKeyboardTracking(True);threshold.setFixedWidth(100);pre.addWidget(threshold);w.thresholds[c]=threshold
        modes.setEnabled(False);threshold.setEnabled(False)
        modes.currentIndexChanged.connect(lambda _,c=c:w.schedule_pre(c,True));threshold.valueChanged.connect(lambda _,c=c:w.schedule_pre(c,False))
        display=QHBoxLayout();image_col.addLayout(display)
        w.preview_modes[c]={}
        tone=display;tone.addWidget(label('亮度补偿','secondary'))
        gain=QComboBox()
        for value in (.5,.65,.8,1.,1.25):gain.addItem(str(value),value)
        gain.setCurrentIndex(3);tone.addWidget(gain,1)
        apply=button('应用亮度');apply.clicked.connect(lambda _,c=c,gain=gain:w.command('/api/v2/config',{'image':{c:{'gain':gain.currentData()}}}));tone.addWidget(apply)
        w.gain_controls[c]=[gain,apply]
        # Keep both rows at the top; extra settings height stays below them.
        image_col.addStretch(1)

        records,records_col=settings_page();tabs.addTab(records,'标定与记录')
        calibration=QHBoxLayout();records_col.addLayout(calibration)
        calibrate=button('棋盘标定 '+c,'outline');calibrate.setEnabled(False);calibrate.clicked.connect(lambda _,c=c:w.calibration(c));calibration.addWidget(calibrate,1);w.calibrate[c]=calibrate
        load=button('导入本路标定');load.clicked.connect(lambda _,c=c:w.load_calibration(c));calibration.addWidget(load,1);w.import_calibration[c]=load
        calibration_label=label('板框测量：等待标定信息','secondary');calibration_label.setWordWrap(True);records_col.addWidget(calibration_label);w.calibration_labels[c]=calibration_label
        records_col.addStretch(1)

        from transport_panel import build_fpga_page,build_network_page
        tabs.insertTab(1,build_fpga_page(w,c),'FPGA 压缩')
        tabs.insertTab(2,build_network_page(w,c),'Qt 图像传输')

    bottom=QSplitter(Qt.Horizontal);bottom.setChildrenCollapsible(False);w.bottom_splitter=bottom;splitter.addWidget(bottom)
    controls=QWidget();controls.setObjectName('OutputControls');controls_col=QVBoxLayout(controls);controls_col.setContentsMargins(14,5,6,10);controls_col.setSpacing(6)
    control_area=QScrollArea();control_area.setWidgetResizable(True);control_area.setFrameShape(QScrollArea.NoFrame)
    control_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);control_area.setMinimumSize(640,0);control_area.setWidget(controls);bottom.addWidget(control_area)
    w.output_tabs=QTabWidget();w.output_tabs.setDocumentMode(True);controls_col.addWidget(w.output_tabs)
    outputs,outputs_col=page();w.output_tabs.addTab(outputs,'显示模式')
    from capture_controls import CaptureModePanel
    w.capture_panel=CaptureModePanel(compact=True);outputs_col.addWidget(w.capture_panel)
    w.capture_panel.requested.connect(lambda target:w.command('/api/v2/config',{'capture':target},error_callback=w.capture_panel.failed))
    row=QHBoxLayout();outputs_col.addLayout(row);row.addWidget(label('HDMI 输出','secondary'))
    w.hdmi=QComboBox()
    for title,value in [('关闭','off'),('相机 A','A'),('相机 B','B'),('双路分屏','split')]:w.hdmi.addItem(title,value)
    row.addWidget(w.hdmi)
    w.apply_hdmi=button('应用 HDMI','outline');w.apply_hdmi.setEnabled(False);w.apply_hdmi.clicked.connect(lambda:w.command('/api/v2/config',{'hdmi':w.hdmi.currentData()}));row.addWidget(w.apply_hdmi)
    row.addStretch(1)
    outputs_col.addStretch(1)

    remote,remote_col=page();w.output_tabs.addTab(remote,'远程推送')
    output_row=QHBoxLayout();output_row.setSpacing(10);remote_col.addLayout(output_row)
    output_fields=QWidget();output_col=QVBoxLayout(output_fields);output_col.setContentsMargins(0,0,0,0);output_col.setSpacing(6)
    output_row.addWidget(output_fields,1)
    w.mobile_qr=MobileQrPanel();output_row.addWidget(w.mobile_qr,0,Qt.AlignTop)
    row=QHBoxLayout();output_col.addLayout(row);row.addWidget(label('远程地址','secondary'));w.url=QLineEdit();w.url.setPlaceholderText('http://接收端IP:9001/upload');row.addWidget(w.url,1)
    save=button('保存地址');save.clicked.connect(lambda:w.command('/api/v2/config',{'push_url':w.url.text().strip()}));row.addWidget(save)
    row=QHBoxLayout();output_col.addLayout(row);row.addWidget(label('手机预览','secondary'))
    w.mobile_address=QComboBox();w.mobile_address.addItem('等待浏览器服务','');row.addWidget(w.mobile_address,1)
    w.mobile_refresh=button('刷新地址');w.mobile_refresh.clicked.connect(w.refresh_browser_address);row.addWidget(w.mobile_refresh)
    w.mobile_open=button('打开网页','outline');w.mobile_open.setEnabled(False)
    w.mobile_open.clicked.connect(lambda:QDesktopServices.openUrl(QUrl(w.mobile_address.currentData())))
    row.addWidget(w.mobile_open);w.browser_network_loaded=False;w.browser_address_busy=False
    output_col.addStretch(1)

    storage,storage_col=page();storage.setObjectName('SnapshotStorage');w.output_tabs.addTab(storage,'截图与存储');w.storage_page=storage
    row=QHBoxLayout();row.setSpacing(8);storage_col.addLayout(row)
    w.snapshot_button=button('','accent');w.snapshot_button.setObjectName('SnapshotCapture')
    w.snapshot_button.setIcon(camera_icon());w.snapshot_button.setIconSize(QSize(28,28));w.snapshot_button.setFixedSize(52,44)
    w.snapshot_button.setAccessibleName('截取所选画面');w.snapshot_button.setEnabled(False)
    w.snapshot_button.clicked.connect(w.take_selected_snapshots);row.addWidget(w.snapshot_button)
    row.addWidget(label('画面','secondary'))
    for c in ('A','B'):
        field=QCheckBox(c);field.setChecked(w.settings.value('snapshot/camera_'+c,True,type=bool))
        field.setAccessibleName('截图画面 '+c);field.setToolTip('勾选后截取相机 '+c+' 的当前高清画面')
        w.snapshot_channels[c]=field;field.toggled.connect(lambda _,c=c:w.snapshot_selection_changed(c));row.addWidget(field)
    row.addStretch(1);row.addWidget(label('保存位置','secondary'))
    w.save_sd=QCheckBox('SD卡');w.save_pc=QCheckBox('上位机')
    for field,key in [(w.save_sd,'sd'),(w.save_pc,'pc')]:
        field.setChecked(w.settings.value('snapshot/'+key,True,type=bool));row.addWidget(field)
        field.toggled.connect(lambda enabled,key=key:w.settings.setValue('snapshot/'+key,enabled))
        field.toggled.connect(w.refresh_snapshot_button)
    row=QHBoxLayout();storage_col.addLayout(row);row.addWidget(label('本地目录','secondary'))
    w.snapshot_dir=QLineEdit(str(w.settings.value('snapshot/directory',str(Path.home()/'Pictures'/'PCB_Snapshots'))))
    w.snapshot_dir.setReadOnly(True);row.addWidget(w.snapshot_dir,1)
    choose=button('选择目录');choose.clicked.connect(w.select_snapshot_dir);row.addWidget(choose)
    reveal=button('打开目录');reveal.clicked.connect(w.open_snapshot_dir);row.addWidget(reveal)
    w.sd_status=label('SD卡：等待板端状态','secondary');w.sd_status.setWordWrap(True);storage_col.addWidget(w.sd_status)
    feedback_row=QHBoxLayout();storage_col.addLayout(feedback_row)
    for c in ('A','B'):
        feedback=label('','secondary');feedback.setWordWrap(True);feedback.setMinimumWidth(0);feedback.hide()
        feedback.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred);feedback.setTextInteractionFlags(Qt.TextSelectableByMouse)
        feedback_row.addWidget(feedback,1);w.snapshot_labels[c]=feedback
    storage_col.addStretch(1)
    w.message=label('正在连接双路设备…','caption');w.message.setWordWrap(True);controls_col.addWidget(w.message)

    console=QWidget();console.setObjectName('Console');console_col=QVBoxLayout(console);console_col.setContentsMargins(14,5,14,10);console_col.setSpacing(7)
    toolbar=QHBoxLayout();console_col.addLayout(toolbar);toolbar.addWidget(label('运行日志','channelTitle'))
    w.log_link=label('网线日志：连接中…','pill');toolbar.addWidget(w.log_link);toolbar.addStretch(1)
    w.log_scroll=QCheckBox('自动滚动');w.log_scroll.setChecked(True);toolbar.addWidget(w.log_scroll)
    clear=button('清空');toolbar.addWidget(clear);export=button('保存日志','outline');export.clicked.connect(w.export_logs);toolbar.addWidget(export)
    w.log_view=QPlainTextEdit();w.log_view.setObjectName('LogView');w.log_view.setReadOnly(True);w.log_view.setMaximumBlockCount(2000)
    w.log_view.setFont(QFont('Consolas',11));w.log_view.setMinimumHeight(40);console_col.addWidget(w.log_view);clear.clicked.connect(w.log_view.clear)
    bottom.addWidget(console);bottom.setSizes([680,860]);bottom.setStretchFactor(0,2);bottom.setStretchFactor(1,3)
    splitter.setSizes([840,160]);splitter.setStretchFactor(0,1);splitter.setStretchFactor(1,0)
    QTimer.singleShot(0,splitter.initial_sizes)
    w.add_log('上位机已启动，正在连接双路服务')
