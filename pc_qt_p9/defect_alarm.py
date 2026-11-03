"""Inspection state indicator; never treats missing inference as a pass."""
import time
from PyQt5.QtCore import QTimer,Qt
from PyQt5.QtWidgets import QLabel


def alarm_state(row, enabled):
    if not enabled:return 'off'
    if not row.get('online') or row.get('infer_error') or row.get('mode_applied',0):return 'unknown'
    if not row.get('client_detection_fresh') or not row.get('detection_frame_seq'):return 'unknown'
    return 'defect' if any(type(d.get('class_id')) is int and 0<=d['class_id']<3 for d in row.get('client_detections',[])) else 'clear'


class AlarmBadge(QLabel):
    def __init__(self,parent):
        super().__init__(parent)
        self.kind='unknown';self.seen=0.;self.phase=False;self.inspection_enabled=False
        self.setObjectName('DefectAlarm')
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.timer=QTimer(self);self.timer.timeout.connect(self.refresh);self.timer.start(500)
        self.refresh()

    def set_result(self,row,enabled):
        self.inspection_enabled=bool(enabled)
        self.kind=alarm_state(row,enabled);self.seen=time.monotonic();self.refresh(False)

    def refresh(self,blink=True):
        position=getattr(self.parent(),'position_badge',None)
        if not self.inspection_enabled or getattr(self.parent(),'closed',False):
            self.hide()
            if position is not None:position.place()
            return
        if blink:self.phase=not self.phase
        kind=self.kind if time.monotonic()-self.seen<2.5 else 'unknown'
        text={'defect':'⚠ 检测到缺陷','clear':'● 未发现缺陷','off':'● 检测未开启','unknown':'● 等待有效检测'}[kind]
        color={'defect':'#dc2626' if self.phase else '#831b1b','clear':'#167347','off':'#43566a','unknown':'#8a5b17'}[kind]
        self.setText(text)
        self.setStyleSheet('background:'+color+';color:white;font-size:21px;font-weight:700;padding:7px 12px;border-radius:6px;')
        self.adjustSize();self.place()
        self.show()
        if position is not None:position.place()

    def place(self):
        parent=self.parent();bounds=getattr(parent,'image_rect',None)
        rect=bounds().toRect() if callable(bounds) else parent.rect()
        self.move(rect.left()+10,max(rect.top()+10,rect.bottom()+1-self.height()-10))
