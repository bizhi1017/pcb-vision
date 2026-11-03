"""Native Qt styling and a small vector mark for the inspection workstation."""
from PyQt5.QtCore import Qt,QRectF
from PyQt5.QtGui import QPixmap,QPainter,QPen,QColor,QIcon,QPainterPath


STYLE='''
QMainWindow,QWidget#Workspace{background:#edf2f7;}
QDialog{background:#edf2f7;}
QWidget{font-family:"Microsoft YaHei UI";font-size:18px;color:#233a51;}
QLabel{background:transparent;border:none;}
QFrame#AppHeader{background:#11283f;border-radius:12px;}
QLabel#HeaderTitle{color:#f4f8fc;font-size:24px;font-weight:700;}
QLabel#HeaderSub{color:#a2bbd1;font-size:16px;}
QLabel#HeaderAddress{color:#c2d3e2;font-size:17px;}
QLabel#ConnectionBadge{background:#233e55;color:#c6dae9;border-radius:12px;padding:5px 12px;}
QLabel#ConnectionBadge[tone="good"]{background:#214d48;color:#a8efd0;}
QLabel#ConnectionBadge[tone="warn"]{background:#58482c;color:#f9dda8;}
QFrame[role="metric"],QFrame[role="cameraCard"]{background:white;border:1px solid #dce5ee;border-radius:10px;}
QLabel[role="metricTitle"]{color:#718399;font-size:17px;}
QLabel[role="metricValue"]{color:#19374f;font-size:23px;font-weight:700;}
QLabel[role="metricHint"]{color:#7d90a2;font-size:16px;}
QLabel[role="trendTitle"]{color:#385571;font-size:20px;font-weight:600;}
QLabel[role="trendValue"]{font-size:23px;font-weight:700;}
QLabel[role="trendHint"]{color:#718399;font-size:17px;}
QLabel[role="cameraStats"]{color:#385571;font-size:21px;}
QLabel[role="detectionStats"]{background:#f1f6fb;border-radius:5px;padding:7px;color:#486783;font-size:20px;}
QLabel[role="detectionStats"][tone="good"]{background:#eaf7f2;color:#20785d;}
QLabel[role="detectionStats"][tone="warn"]{background:#fff0e8;color:#b56834;}
QLabel[role="channelCode"]{background:#e9f1ff;color:#1e6cde;border-radius:9px;font-size:21px;font-weight:700;}
QLabel[role="channelCode"][channel="B"]{background:#e4f5f1;color:#188c7c;}
QLabel[role="channelTitle"]{font-size:20px;font-weight:700;color:#203d56;}
QLabel[role="caption"]{color:#7c8fa1;font-size:16px;}
QLabel[role="secondary"]{color:#536d83;font-size:17px;}
QLabel[role="ribbon"]{background:#f1f6fb;border-radius:5px;padding:5px 8px;color:#486783;font-size:17px;}
QLabel[role="ribbon"][tone="good"]{background:#eaf7f2;color:#20785d;}
QLabel[role="ribbon"][tone="warn"]{background:#fff0e8;color:#b56834;}
QLabel[role="pill"]{background:#eef2f7;color:#758797;border-radius:10px;padding:4px 9px;font-size:16px;}
QLabel[role="pill"][tone="good"]{background:#e5f5ed;color:#238660;}
QLabel[role="pill"][tone="warn"]{background:#fff2dc;color:#b07921;}
QPushButton{background:#edf4ff;border:1px solid #cbdcf1;border-radius:6px;padding:6px 10px;min-height:19px;color:#385571;}
QPushButton:hover{background:#e7f0fa;border-color:#aac5e4;}
QPushButton:pressed{background:#dce9f6;}
QFrame[role="cameraCard"] QPushButton{font-size:22px;min-height:26px;padding:6px 4px;}
QPushButton[role="primary"]{background:#236ee8;border-color:#236ee8;color:white;font-weight:600;}
QPushButton[role="primary"]:hover{background:#155fd6;}
QPushButton[role="accent"]{background:#168879;border-color:#168879;color:white;font-weight:600;}
QPushButton[role="accent"]:hover{background:#0c7669;}
QPushButton[role="outline"]{background:#edf4ff;color:#286cd1;border-color:#c7dbf9;}

QPushButton:checked{background:#e7f0fc;color:#285a98;border-color:#b7cfe9;}
QPushButton[role="previewMode"]{background:#e7f0fc;color:#245c9f;border-color:#b7cfe9;}
QPushButton[role="previewMode"]:checked{background:#236ee8;color:white;border-color:#236ee8;font-weight:700;}
QPushButton[role="previewMode"]:checked:hover{background:#155fd6;}
QPushButton[role="previewToggle"]{background:#117b68;color:white;border-color:#117b68;font-weight:600;}
QPushButton[role="previewToggle"]:hover{background:#0b6958;}
QPushButton[role="previewToggle"]:checked{background:#e5f3ed;color:#226b56;border-color:#aed3c3;font-weight:600;}
QPushButton[role="previewToggle"]:checked:hover{background:#d7ebdf;}
QPushButton[role="stateToggle"]{background:#e7f0fc;color:#245c9f;border-color:#b7cfe9;font-weight:600;}
QPushButton[role="stateToggle"][active="true"]{background:#2366c2;color:white;border-color:#2366c2;}
QPushButton[role="stateToggle"][tint="green"]{background:#e5f3ed;color:#226b56;border-color:#aed3c3;}
QPushButton[role="stateToggle"][tint="green"][active="true"]{background:#117b68;color:white;border-color:#117b68;}
QPushButton:disabled,QPushButton[role="primary"]:disabled,QPushButton[role="accent"]:disabled,QPushButton[role="outline"]:disabled{background:#f1f4f7;border-color:#e3e9ef;color:#a4b0bc;}
QComboBox,QLineEdit,QSpinBox,QDoubleSpinBox{background:white;border:1px solid #d7e2ec;border-radius:5px;padding:5px 7px;min-height:19px;selection-background-color:#d4e6ff;}
QComboBox:focus,QLineEdit:focus,QSpinBox:focus,QDoubleSpinBox:focus{border-color:#77aaf0;}
QComboBox:disabled,QLineEdit:disabled,QSpinBox:disabled,QDoubleSpinBox:disabled{background:#f4f7fa;color:#a5b2c0;}
QComboBox::drop-down{border:none;width:22px;}
QTabWidget::pane{background:#f8fafc;border:1px solid #e3ebf2;border-radius:7px;top:-1px;}
QTabBar::tab{background:transparent;color:#7b8fa3;padding:7px 14px;border-bottom:2px solid transparent;}
QTabBar::tab:selected{color:#2166c6;border-bottom:2px solid #3680e9;font-weight:600;}
QTabBar::tab:hover{color:#2166c6;background:#edf4fc;}
QTabWidget#CameraSettings QWidget{font-size:22px;}
QTabWidget#CameraSettings QComboBox,QTabWidget#CameraSettings QLineEdit,QTabWidget#CameraSettings QSpinBox,QTabWidget#CameraSettings QDoubleSpinBox{min-height:26px;}
QTabWidget#CameraSettings QCheckBox::indicator{width:22px;height:22px;}
QCheckBox{spacing:6px;color:#536d83;}
QScrollArea{border:none;background:transparent;}
QScrollBar:vertical{background:#edf2f7;width:8px;margin:2px;}
QScrollBar::handle:vertical{background:#c5d3df;border-radius:3px;min-height:24px;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}
QSplitter::handle{background:#bdcddd;height:8px;}
QSplitter::handle:hover{background:#7da5ca;}
QFrame#PreviewWell{background:transparent;border:none;border-radius:0;}
QWidget#Console,QWidget#OutputControls{background:#edf2f7;}
QWidget#OutputControls QTabBar{font-size:22px;}
QWidget#SnapshotStorage QWidget{font-size:22px;}
QWidget#SnapshotStorage QPushButton,QWidget#SnapshotStorage QLineEdit{min-height:26px;}
QWidget#SnapshotStorage QCheckBox::indicator{width:22px;height:22px;}
QWidget#OutputControls QLabel[role="caption"]{font-size:22px;}
QPlainTextEdit#LogView{background:#12283d;color:#c5d6e7;border:1px solid #243e56;border-radius:8px;padding:10px;font-family:Consolas,"Microsoft YaHei UI";font-size:15px;}
QWidget#Console QWidget{font-size:22px;}
QWidget#Console QLabel[role="channelTitle"]{font-size:22px;}
QWidget#Console QLabel[role="pill"]{font-size:22px;}
QWidget#Console QPushButton{min-height:26px;}
QWidget#Console QCheckBox::indicator{width:22px;height:22px;}
QWidget#Console QPlainTextEdit#LogView{font-size:15px;}
QToolTip{background:#17344e;color:white;border:0;padding:6px;}
'''


def mark_pixmap():
    mark=QPixmap(44,44);mark.fill(Qt.transparent)
    p=QPainter(mark);p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen);p.setBrush(QColor('#1c465e'));p.drawRoundedRect(QRectF(0,0,44,44),10,10)
    p.setPen(QPen(QColor('#54d3c0'),2));p.setBrush(Qt.NoBrush);p.drawRoundedRect(QRectF(12,12,20,20),4,4)
    for v in (16,22,28):
        p.drawLine(v,7,v,11);p.drawLine(v,33,v,37);p.drawLine(7,v,11,v);p.drawLine(33,v,37,v)
    p.setPen(QPen(QColor('#f1fcff'),2));p.drawLine(17,26,17,18);p.drawLine(17,18,25,18);p.drawLine(25,18,25,24);p.drawLine(25,24,29,24)
    p.end();return mark


def set_property(widget,name,value):
    if widget.property(name)!=value:
        widget.setProperty(name,value);widget.style().unpolish(widget);widget.style().polish(widget);widget.update()


def camera_icon():
    icon=QIcon()
    for mode,color in ((QIcon.Normal,'#ffffff'),(QIcon.Disabled,'#a4b0bc')):
        pixmap=QPixmap(48,48);pixmap.fill(Qt.transparent)
        painter=QPainter(pixmap);painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(color),2.5,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin));painter.setBrush(Qt.NoBrush)
        body=QPainterPath();body.moveTo(7,13);body.lineTo(15,13);body.lineTo(19,7);body.lineTo(29,7);body.lineTo(33,13);body.lineTo(41,13)
        body.quadTo(44,13,44,17);body.lineTo(44,37);body.quadTo(44,41,40,41);body.lineTo(8,41);body.quadTo(4,41,4,37);body.lineTo(4,17);body.quadTo(4,13,7,13)
        painter.drawPath(body);painter.drawEllipse(QRectF(15,18,18,18));painter.drawLine(36,18,39,18)
        painter.end();icon.addPixmap(pixmap,mode)
    return icon
