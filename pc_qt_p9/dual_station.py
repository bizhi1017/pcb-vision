"""Dual camera operator window. Separate from stable single-camera station."""
import argparse
import base64
import json
import time
import threading
from pathlib import Path
from datetime import datetime,timezone,timedelta
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler
from PyQt5.QtCore import QThread, QTimer, pyqtSignal, Qt,QSettings,QUrl,QRectF,QSize
from PyQt5.QtGui import QImage,QPixmap,QFont,QDesktopServices,QColor,QTextCursor,QTextCharFormat,QPainter,QPen
from PyQt5.QtWidgets import (QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,
                            QGroupBox,QLabel,QPushButton,QComboBox,QLineEdit,QDialog,QScrollArea,QSpinBox,QDoubleSpinBox,QFileDialog,QCheckBox,QSizePolicy,QSplitter,QPlainTextEdit)
from pcb_station import AspectLabel, MjpegParser
from station_layout import build_layout
from station_theme import set_property
from network_stream import FrameParser,AsyncAckConnection,configure_stream_socket
from preview_decode import decode_image,DECODER_BACKEND,DECODER_ID,configure_live_thread,lossless_available
from preview_pipeline import PreviewPipeline
from preview_overlay import PreviewOverlay,CLASS_STYLES
from defect_alarm import AlarmBadge
from position_guide import PositionBadge,PositionWorker,MAX_AGE
from click_controls import QComboBox,QSpinBox,QDoubleSpinBox
from transport_auto import AutoTransport
from transport_panel import sync_transport_controls
from station_telemetry import displayed_fpga_compression,fpga_compression_detail

# This operator connects directly to the board. Windows proxy bypass/PAC
# lookup can itself block for seconds before the socket timeout starts.
# Do not alter system proxy settings or urllib's global opener.
BOARD_HTTP=build_opener(ProxyHandler({}))
CHINA_TIME=timezone(timedelta(hours=8))


def open_board_url(request,timeout):
    return BOARD_HTTP.open(request,timeout=timeout)


class MjpegWorker(QThread):
    """Read available bytes, rather than waiting for a 4096-byte block."""
    imageReady=pyqtSignal(object)
    netError=pyqtSignal(str)

    def __init__(self,url,timeout=2):
        super().__init__();self.url=url;self.timeout=timeout;self.stopped=threading.Event();self.response=None;self.preview_fps=0.

    def stop(self):
        self.stopped.set()
        if self.response is not None:
            try:self.response.close()
            except OSError:pass

    def run(self):
        while not self.stopped.is_set():
            try:
                self.response=open_board_url(self.url,timeout=self.timeout);parser=MjpegParser()
                while not self.stopped.is_set():
                    chunk=self.response.read1(65536)
                    if not chunk:break
                    frames=parser.feed(chunk)
                    if frames:
                        image=decode_image(frames[-1])
                        if not image.isNull():self.imageReady.emit(image)
            except Exception as exc:
                if not self.stopped.is_set():self.netError.emit(str(exc))
            finally:
                if self.response is not None:self.response.close();self.response=None
            self.stopped.wait(.15)


class AdaptiveWorker(MjpegWorker):
    def __init__(self,url,timeout=2):
        super().__init__(url,timeout);self.paused=threading.Event();self.capture_stopped=threading.Event()
        self.fast_lossless_available=DECODER_ID=='opencv' and lossless_available()

    def set_paused(self,value):self.paused.set() if value else self.paused.clear()
    def set_capture_enabled(self,value):self.capture_stopped.clear() if value else self.capture_stopped.set()

    def run(self):
        configure_live_thread()
        base=self.url.split('/api/v2/')[0];camera=self.url.split('/')[-2]
        while not self.stopped.is_set():
            if self.paused.is_set() or self.capture_stopped.is_set():self.stopped.wait(.05);continue
            ack=AsyncAckConnection(base,camera,cancelled=lambda:self.stopped.is_set() or self.paused.is_set() or self.capture_stopped.is_set(),initialize_thread=configure_live_thread)
            pipeline=None
            try:
                request_url=self.url+('&' if '?' in self.url else '?')+'lossless=fast' if self.fast_lossless_available else self.url
                self.response=open_board_url(request_url,timeout=max(4,self.timeout))
                configure_stream_socket(self.response)
                client=self.response.headers.get('X-Client-ID');parser=FrameParser(include_metadata=True);overlay=PreviewOverlay()
                decoder=DECODER_ID if self.response.headers.get('X-Decoder-Metadata')=='1' else None
                report_overlay=self.response.headers.get('X-Client-Overlay')=='1'
                if not client:raise ValueError('missing stream session')
                def decode(job):
                    started=time.perf_counter();image=decode_image(job[1])
                    if image.isNull():raise ValueError('无法解码网络图像')
                    return image,(time.perf_counter()-started)*1000
                def deliver(job,result):
                    pipeline.check();ack.check()
                    frame_id,_,metadata=job;image,ms=result
                    overlay_started=time.perf_counter();image._preview_boxes=overlay.project(image,metadata)
                    overlay_ms=(time.perf_counter()-overlay_started)*1000
                    image._preview_boxes_until=time.monotonic()+max(0,(metadata.get('max_age_ms',0)-metadata.get('age_ms',0))/1000)
                    image._capture_generation=metadata.get('_capture_generation')
                    self.imageReady.emit(image)
                    ack.send(client,frame_id,ms,decoder,overlay_ms if report_overlay else None,self.preview_fps if report_overlay else None)
                pipeline=PreviewPipeline(decode,deliver,cancelled=lambda:self.stopped.is_set() or self.paused.is_set() or self.capture_stopped.is_set(),initialize_thread=configure_live_thread)
                while not self.stopped.is_set() and not self.paused.is_set() and not self.capture_stopped.is_set():
                    ack.check();pipeline.check()
                    data=self.response.read1(262144)
                    if not data:break
                    for job in parser.feed(data):pipeline.submit(job)
                if not self.stopped.is_set() and not self.paused.is_set() and not self.capture_stopped.is_set():pipeline.finish()
            except Exception as exc:
                if getattr(exc,'code',None)==404:
                    self.url=self.url.rsplit('/',1)[0]+'/stream.mjpg';super().run();return
                if not self.stopped.is_set() and not self.paused.is_set():self.netError.emit(str(exc))
            finally:
                ack.close()
                if pipeline is not None:pipeline.close()
                if self.response is not None:self.response.close();self.response=None
            self.stopped.wait(.1)


class VideoLabel(AspectLabel):
    """Compact 16:9 preview with UI overlays; source pixels stay untouched."""
    enlargeRequested=pyqtSignal()
    def __init__(self,text='',camera=None):
        super().__init__(text)
        policy=QSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred);policy.setHeightForWidth(True);self.setSizePolicy(policy)
        self.painted=0;self.last_painted=None;self.paint_ms=0.
        self.camera=camera;self.closed=False;self.detection_enabled=False
        if camera:
            self.alarm_badge=AlarmBadge(self)
            self.position_badge=PositionBadge(self)
            self.setMaximumWidth(1920)
            self.camera_name=QLabel('相机 '+camera,self);self.camera_name.setObjectName('CameraOverlay')
            self.status_badge=QLabel('等待图像',self);self.status_badge.setObjectName('StatusOverlay')
            for badge in (self.camera_name,self.status_badge):badge.setAttribute(Qt.WA_TransparentForMouseEvents)
            self.camera_name.setStyleSheet('background:rgba(13,33,51,210);color:white;font-size:19px;font-weight:700;padding:5px 10px;border-radius:5px;')
            self.status_badge.setStyleSheet('''QLabel{background:rgba(27,45,63,220);color:white;font-size:17px;padding:5px 10px;border-radius:5px;}
                QLabel[tone="good"]{background:rgba(13,92,65,230);color:#d5ffeb;}
                QLabel[tone="warn"]{background:rgba(121,76,17,230);color:#fff0d2;}''')
            self.layout_overlays()

    def sizeHint(self):
        width=min(960,self.maximumWidth()) if self.camera else 640
        return QSize(width,self.heightForWidth(width))

    def hasHeightForWidth(self):return True

    def heightForWidth(self,width):return max(180,round(width*9/16))

    def image_rect(self):
        if self._image is None or self._image.isNull():return QRectF(self.rect())
        scale=min(self.width()/self._image.width(),self.height()/self._image.height())
        width,height=self._image.width()*scale,self._image.height()*scale
        return QRectF((self.width()-width)/2,(self.height()-height)/2,width,height)

    def layout_overlays(self):
        if not self.camera:return
        self.camera_name.adjustSize();self.status_badge.adjustSize()
        rect=self.image_rect().toRect();left,top=rect.left()+10,rect.top()+10
        self.camera_name.move(left,top);self.status_badge.move(max(left,rect.right()+1-self.status_badge.width()-10),top)
        self.alarm_badge.place()
        self.position_badge.place()

    def mouseDoubleClickEvent(self,event):
        if self.camera and event.button()==Qt.LeftButton:
            self.enlargeRequested.emit();event.accept()
        else:super().mouseDoubleClickEvent(event)

    def set_image(self,image):
        previous=self._image.size() if self._image is not None else None
        self._image=image
        if previous!=(image.size() if image is not None else None):self.layout_overlays()
        self.update()

    def refresh(self):self.update()

    def paintEvent(self,event):
        started=time.perf_counter()
        painter=QPainter(self);painter.fillRect(self.rect(),QColor('#000000' if self.closed else '#101010'))
        if self._image is None:
            font=QFont('Microsoft YaHei UI');font.setPixelSize(38 if self.closed else 24);font.setBold(self.closed)
            painter.setFont(font);painter.setPen(QColor('#ffffff' if self.closed else '#c8d4df'))
            painter.drawText(self.rect().adjusted(12,45,-12,-10),Qt.AlignCenter|Qt.TextWordWrap,self.text())
            return
        rect=self.image_rect();width,height=rect.width(),rect.height()
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawImage(rect,self._image)
        if self.detection_enabled and time.monotonic()<getattr(self._image,'_preview_boxes_until',0):
            left,top=rect.left(),rect.top()
            font=QFont('Microsoft YaHei UI');font.setPixelSize(17);font.setBold(True);painter.setFont(font)
            for d in getattr(self._image,'_preview_boxes',[]):
                cid=d.get('class_id',-1);color=QColor(CLASS_STYLES[cid][1] if type(cid) is int and 0<=cid<3 else '#ffffff')
                pen=QPen(color);pen.setWidthF(2.5);painter.setPen(pen)
                x0,y0,x1,y1=d['box'];rect=QRectF(left+x0*width,top+y0*height,(x1-x0)*width,(y1-y0)*height)
                painter.drawRect(rect)
                label_width=painter.fontMetrics().horizontalAdvance(d['label'])+10;label_height=painter.fontMetrics().height()+4
                label=QRectF(max(left,min(rect.left(),left+width-label_width)),max(top,rect.top()-label_height-2),label_width,label_height)
                painter.fillRect(label,QColor(9,24,38,225));painter.drawText(label,Qt.AlignCenter,d['label'])
        self.paint_ms=(time.perf_counter()-started)*1000
        key=self._image.cacheKey()
        if key!=self.last_painted:self.painted+=1;self.last_painted=key

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self.layout_overlays()


def request(base,path,body=None):
    data=None if body is None else json.dumps(body).encode()
    req=Request(base+path,data=data,headers={'Content-Type':'application/json'},method='GET' if body is None else 'POST')
    try:
        with open_board_url(req,timeout=30 if '/calibration/' in path or path.endswith(('/snapshot','/defects')) else 10 if path.endswith('/measure') else 4) as response: return response.read()
    except Exception as exc:
        if hasattr(exc,'read'):
            try: raise ValueError(json.loads(exc.read()).get('error',str(exc))) from exc
            except (json.JSONDecodeError,AttributeError): pass
        raise


class Poller(QThread):
    state=pyqtSignal(object)
    image=pyqtSignal(str,object)
    error=pyqtSignal(str)

    def __init__(self,base,channel=None):
        super().__init__();self.base=base;self.channel=channel

    def run(self):
        due=0
        while not self.isInterruptionRequested():
            if self.channel is None and time.monotonic()>=due:
                try: self.state.emit(json.loads(request(self.base,'/api/v2/status')))
                except Exception as exc: self.error.emit(str(exc))
                due=time.monotonic()+.2
            for c in (() if self.channel is None else (self.channel,)):
                if self.isInterruptionRequested(): break
                try:
                    image=QImage.fromData(request(self.base,'/api/v2/camera/'+c+'/snap.jpg'))
                    if not image.isNull(): self.image.emit(c,image)
                except Exception: self.image.emit(c,None)
            self.msleep(100)


class LogPoller(QThread):
    batch=pyqtSignal(object)
    notice=pyqtSignal(str,str)

    def __init__(self,base):
        super().__init__();self.base=base;self.cursor=0;self.session=None;self.stopped=threading.Event()

    def stop(self):self.stopped.set()

    def run(self):
        error=None
        while not self.stopped.is_set():
            delay=.25
            try:
                query=dict(after=self.cursor)
                if self.session:query['session']=self.session
                result=json.loads(request(self.base,'/api/v2/logs?'+urlencode(query)))
                if error:self.notice.emit('INFO','板端日志连接已恢复')
                if result.get('reset'):self.notice.emit('INFO','板端服务日志会话已重置，继续接收新日志')
                error=None;self.cursor=result['cursor'];self.session=result['session'];self.batch.emit(result)
                if len(result['events'])==200:delay=.01
            except Exception as exc:
                detail=str(exc)
                if detail!=error:
                    self.notice.emit('WARN','板端日志不可用：'+('请重启新版板端服务' if detail=='not found' else detail));error=detail
                delay=2
            self.stopped.wait(delay)


class Command(QThread):
    done=pyqtSignal(object)
    error=pyqtSignal(str)

    def __init__(self,base,path,body):
        super().__init__();self.base,self.path,self.body=base,path,body

    def run(self):
        try:
            if self.path.endswith('/hq.jpg'):
                self.done.emit(dict(camera=self.path.split('/')[-2],jpeg=request(self.base,self.path)))
            else:self.done.emit(json.loads(request(self.base,self.path,self.body)))
        except Exception as exc: self.error.emit(str(exc))


class SnapshotWorker(QThread):
    done=pyqtSignal(object)
    error=pyqtSignal(str)

    def __init__(self,base,camera,sd,pc,directory):
        super().__init__();self.base=base;self.camera=camera;self.sd=sd;self.pc=pc;self.directory=directory

    def run(self):
        try:
            body=dict(sd=self.sd,pc=self.pc,client_time=datetime.now(timezone.utc).isoformat(timespec='milliseconds'))
            result=json.loads(request(self.base,'/api/v2/camera/'+self.camera+'/snapshot',body))
            result['pc']=dict(requested=self.pc,ok=False,path='',error='')
            if self.pc:
                try:
                    from snapshot_client import save_local_snapshot
                    result['pc'].update(ok=True,path=save_local_snapshot(self.directory,result))
                except Exception as exc:result['pc']['error']=str(exc)
            result.pop('png',None);self.done.emit(result)
        except Exception as exc:self.error.emit(str(exc))


class CalibrationDialog(QDialog):
    def __init__(self,station,camera):
        super().__init__(station);self.station=station;self.camera=camera;self.busy=False
        self.setWindowTitle('相机 '+camera+' · 棋盘标定');self.resize(1100,560)
        self.setWindowModality(Qt.WindowModal)
        layout=QVBoxLayout(self);settings=QHBoxLayout();layout.addLayout(settings)
        self.columns=QSpinBox();self.rows=QSpinBox()
        for field in (self.columns,self.rows):field.setRange(3,15);field.setValue(5)
        self.square=QDoubleSpinBox();self.square.setRange(.5,100);self.square.setDecimals(3);self.square.setValue(9.4)
        self.thickness=QDoubleSpinBox();self.thickness.setRange(0,20);self.thickness.setDecimals(2);self.thickness.setValue(1.2)
        for name,field in [('内角点列',self.columns),('内角点行',self.rows),('每格mm',self.square),('板厚mm',self.thickness)]:
            settings.addWidget(QLabel(name));settings.addWidget(field)
        note=QLabel('先采8~20张：棋盘放在不同位置，改变倾角/远近，保持焦距固定。计算镜头后，棋盘平放并垫到PCB顶面同高，再保存平面标定。')
        note.setWordWrap(True);layout.addWidget(note)
        previews=QHBoxLayout();layout.addLayout(previews)
        self.live=VideoLabel('等待实时画面');self.view=VideoLabel('准备棋盘后点击“开始/重置”')
        for title,view in [('实时画面 · 调整棋盘姿态',self.live),('最近采集 · 角点检查',self.view)]:
            panel=QVBoxLayout();panel.addWidget(QLabel(title));panel.addWidget(view);previews.addLayout(panel,1)
        layout.addStretch(1)
        self.message=QLabel('6×6格对应5×5内角点；标定只用于当前相机、机位、焦距和板面高度。');self.message.setWordWrap(True);layout.addWidget(self.message)
        buttons=QHBoxLayout();layout.addLayout(buttons)
        self.reset=QPushButton('开始 / 重置采集');self.sample=QPushButton('采集当前姿态 0/8')
        self.lens=QPushButton('计算镜头标定');self.plane=QPushButton('平放垫高后保存平面')
        self.controls=[self.reset,self.sample,self.lens,self.plane]
        for button in self.controls:buttons.addWidget(button)
        self.sample.setEnabled(False);self.lens.setEnabled(False);self.plane.setEnabled(False)
        self.reset.clicked.connect(lambda:self.send('reset',dict(columns=self.columns.value(),rows=self.rows.value(),square_mm=self.square.value(),board_thickness_mm=self.thickness.value())))
        self.sample.clicked.connect(lambda:self.send('sample',{}));self.lens.clicked.connect(lambda:self.send('lens',{}));self.plane.clicked.connect(lambda:self.send('plane',{}))
        self.session=None
        self.live_timer=QTimer(self);self.live_timer.timeout.connect(self.update_live);self.live_timer.start(80)
        self.finished.connect(self.live_timer.stop);self.update_live()

    def update_live(self):
        with self.station.image_lock:image=self.station.last_received.get(self.camera)
        if image is not None and image is not self.live._image:self.live.set_image(image)

    def send(self,action,body):
        if self.busy:return
        self.busy=True
        for button in self.controls:button.setEnabled(False)
        for field in (self.columns,self.rows,self.square,self.thickness):field.setEnabled(False)
        self.message.setText('正在处理，请稍候…')
        worker=Command(self.station.base,'/api/v2/camera/'+self.camera+'/calibration/'+action,body)
        self.station.commands.append(worker);worker.done.connect(self.updated);worker.error.connect(self.failed)
        worker.finished.connect(lambda:self.station.retire(worker));worker.start()

    def enable(self):
        self.busy=False;self.reset.setEnabled(True)
        self.sample.setEnabled(self.session is not None)
        self.lens.setEnabled(bool(self.session and self.session['samples']>=8))
        self.plane.setEnabled(bool(self.session and self.session['lens_ready']))
        for field in (self.columns,self.rows,self.square,self.thickness):field.setEnabled(self.session is None)

    def updated(self,result):
        self.session=result['session'];self.enable()
        self.sample.setText('采集当前姿态 %d/8'%self.session['samples'])
        if result.get('png'):self.view.set_image(QImage.fromData(base64.b64decode(result['png'])))
        action=result['action']
        if action=='reset':
            self.view.set_image(None);self.view.clear();self.view.setText('调整棋盘并放稳后，点击“采集当前姿态”')
        if action=='plane':
            self.station.measure_feedback[self.camera]='标定已保存；请移走棋盘、放好PCB，再点击“测量 '+self.camera+'”'
            self.station.calibration_labels[self.camera].setText(self.station.measure_feedback[self.camera])
            self.accept()
        elif self.session['lens_ready']:self.message.setText('镜头标定通过，RMS %.3f px。现在把棋盘平放并垫到PCB顶面同高，再保存平面。'%self.session['rms_px'])
        else:self.message.setText('已采 %d 张；移动或倾斜棋盘并放稳后再采，至少8张。'%self.session['samples'])

    def failed(self,error):
        self.enable();self.message.setText(error);self.station.add_log('棋盘标定失败：'+error,'ERROR',camera=self.camera)


class DualWindow(QMainWindow):
    frameAvailable=pyqtSignal()
    def __init__(self,base,settings=None):
        super().__init__();self.base=base.rstrip('/');self.commands=[];self.dialogs=[]
        self.setFont(QFont('Microsoft YaHei UI',12))
        scheduling=configure_live_thread()
        self.setWindowTitle('PCB VISION · 1080P / 单路4K · transport-v3.13-ui40-p9 · '+base);self.resize(1440,1000);self.setMinimumSize(1320,720)
        self.settings=settings if settings is not None else QSettings('PCBInspection','DualStationP9')
        self.inspect={};self.inspect_busy=set();self.browser_push_busy=set();self.views={};self.labels={};self.detect={};self.push={};self.measure={};self.previews={}
        self.pre_modes={};self.thresholds={};self.gain_controls={}
        self.detection_labels={}
        self.calibration_labels={};self.calibrate={};self.import_calibration={}
        self.measure_busy=set();self.measure_feedback={}
        self.snapshot_channels={};self.snapshot_available=dict(A=False,B=False);self.snapshot_labels={};self.snapshot_busy=set();self.last_ui_error=None;self.last_ui_error_at=0.
        self.pre_pending={};self.detect_pending={};self.pre_supported=False;self.pre_busy=False;self.pre_initialized=set();self.closing=False
        self.pre_timer=QTimer(self);self.pre_timer.setSingleShot(True);self.pre_timer.timeout.connect(self.flush_pre)
        self.image_lock=threading.Lock()
        self.render_pending=False;self.frameAvailable.connect(self.render_latest,Qt.QueuedConnection)
        self.latest_images={};self.last_received={};self.preview_counts=dict(A=0,B=0);self.preview_fps=dict(A=0.,B=0.)
        self.capture_enabled=dict(A=True,B=True);self.capture_generation_seen=None;self.capture_session_seen=None;self.capture_retired_sessions=set()
        self.preview_at=time.monotonic()
        self.last_paint_count=dict(A=0,B=0)
        self.live_viewer=None
        self.position_checks={};self.position_rows={}
        self.position_worker=PositionWorker(self)
        self.position_worker.resultReady.connect(self.position_result)
        self.auto_transport=AutoTransport(self)
        build_layout(self,VideoLabel)
        self.browser_address_timer=QTimer(self);self.browser_address_timer.timeout.connect(self.refresh_browser_address);self.browser_address_timer.start(10000)
        self.add_log('预览解码：'+DECODER_BACKEND+' · 两帧有序流水')
        self.add_log('实时预览线程调度：'+scheduling)
        self.add_log('Qt Python线程切换周期：1.0 ms')
        self.config=None
        self.position_worker.start()
        self.poller=Poller(self.base);self.poller.state.connect(self.state)
        self.poller.image.connect(self.image);self.poller.error.connect(self.connection_failed);self.poller.start()
        self.camera_pollers=[]
        for c in ('A','B'):
            worker=AdaptiveWorker(self.base+'/api/v2/camera/'+c+'/stream?overlay=client',timeout=2)
            worker.imageReady.connect(lambda image,c=c:self.queue_image(c,image),Qt.DirectConnection)
            worker.netError.connect(lambda message,c=c:self.image(c,None))
            worker.start();self.camera_pollers.append(worker)
        # Frames schedule one coalesced GUI event immediately. This timer only
        # refreshes counters/expiry; it no longer samples video at a fixed rate.
        self.render_timer=QTimer(self);self.render_timer.setTimerType(Qt.PreciseTimer);self.render_timer.timeout.connect(self.render_latest);self.render_timer.start(50)
        self.log_poller=LogPoller(self.base);self.log_poller.batch.connect(self.receive_logs)
        self.log_poller.notice.connect(self.log_notice);self.log_poller.start()

    def add_log(self,message,level='INFO',source='上位机',camera=None,stamp=None):
        stamp=stamp or datetime.now(CHINA_TIME)
        text='[%s] [%s] [%s%s] %s'%(stamp.strftime('%H:%M:%S'),level,source,' '+camera if camera else '',str(message).replace('\r',' ').replace('\n',' | '))
        bar=self.log_view.verticalScrollBar();position=bar.value()
        self.log_view.appendPlainText(text)
        cursor=QTextCursor(self.log_view.document());cursor.movePosition(QTextCursor.End);cursor.select(QTextCursor.BlockUnderCursor)
        style=QTextCharFormat();style.setForeground(QColor({'ERROR':'#ffadad','WARN':'#f2cf88'}.get(level,'#c5d6e7')));cursor.mergeCharFormat(style)
        bar.setValue(bar.maximum() if self.log_scroll.isChecked() else position)

    def receive_logs(self,result):
        self.log_link.setText('网线日志：已连接')
        set_property(self.log_link,'tone','good')
        if result.get('dropped'):self.add_log('板端历史缓存已覆盖%d条较早日志'%result['dropped'],'WARN')
        now=datetime.now(CHINA_TIME)
        for item in result['events']:
            age=max(0,result['server_uptime']-item['at'])
            self.add_log(item['message'],item['level'],'板端',item.get('camera'),now-timedelta(seconds=age))

    def log_notice(self,level,message):
        self.log_link.setText('网线日志：重连中' if level=='WARN' else '网线日志：已连接')
        set_property(self.log_link,'tone','warn' if level=='WARN' else 'good')
        self.add_log(message,level)

    def export_logs(self):
        default=Path(self.snapshot_dir.text())/('PCB_log_'+datetime.now(CHINA_TIME).strftime('%Y%m%d_%H%M%S')+'.txt')
        path,_=QFileDialog.getSaveFileName(self,'保存当前日志',str(default),'文本文件 (*.txt)')
        if path:
            try:
                output=Path(path);output.parent.mkdir(parents=True,exist_ok=True)
                output.write_text(self.log_view.toPlainText(),encoding='utf-8');self.add_log('日志已保存：'+str(output))
            except OSError as exc:self.add_log('日志保存失败：'+str(exc),'ERROR')

    def queue_image(self,c,image):
        if not getattr(self,'capture_enabled',{}).get(c,True):return
        if getattr(image,'_capture_generation',None) is not None and image._capture_generation!=getattr(self,'capture_generation_seen',None):return
        # Runs in the receiver thread: replace pending frames before the GUI queue.
        with self.image_lock:
            self.latest_images[c]=image;self.last_received[c]=image
            notify=not self.render_pending;self.render_pending=True
        self.position_worker.submit(c,image)
        if notify:self.frameAvailable.emit()

    def render_latest(self):
        # Keep only the latest pending image; never replay an old GUI frame backlog.
        with self.image_lock:pending=self.latest_images;self.latest_images={};self.render_pending=False
        for c,image in pending.items():
            if not self.previews[c].isChecked():
                self.image(c,image);self.preview_counts[c]+=1
        for view in self.views.values():
            if view._image is not None and getattr(view._image,'_preview_boxes',[]) and time.monotonic()>=getattr(view._image,'_preview_boxes_until',0):
                view._image._preview_boxes=[];view.update()
        now=time.monotonic();dt=now-self.preview_at
        if dt>=1:
            self.preview_fps={c:(self.views[c].painted-self.last_paint_count[c])/dt for c in ('A','B')}
            self.last_paint_count={c:self.views[c].painted for c in ('A','B')}
            for c,worker in zip(('A','B'),getattr(self,'camera_pollers',[])):worker.preview_fps=self.preview_fps[c]
            self.preview_counts=dict(A=0,B=0);self.preview_at=now

    def preview(self,c,paused):
        worker=self.camera_pollers[('A','B').index(c)]
        if hasattr(worker,'set_paused'):worker.set_paused(paused)
        self.previews[c].setText('恢复本路预览' if paused else '暂停本路预览')
        self.views[c].closed=paused
        self.views[c].alarm_badge.refresh(False)
        self.channel_badges[c].setText('预览已关闭' if paused else '恢复连接中')
        set_property(self.channel_badges[c],'tone','quiet');self.views[c].layout_overlays()
        if paused:
            self.show_closed(c)
        else:
            with self.image_lock:image=self.last_received.get(c)
            if image is not None:self.views[c].set_image(image)
            else:self.views[c].clear();self.views[c].setText('等待相机 '+c)
        self.add_log('本路Qt预览已关闭' if paused else '本路Qt预览已恢复',camera=c)
        self.update_position_guide(c,reset=True)

    def update_position_guide(self,c,reset=False):
        row=self.position_rows.get(c,{})
        enabled=self.position_checks[c].isChecked()
        self.views[c].position_badge.set_enabled(enabled)
        set_property(self.position_checks[c],'active',enabled)
        color=(self.config is not None and row.get('mode_applied',0)==0
               and self.config.get('pre',{}).get(c,0)==0 and self.pre_pending.get(c,(0,))[0]==0
               and not (self.pre_busy and self.pre_modes[c].currentIndex()!=0))
        active=enabled and bool(row.get('online')) and color and not self.previews[c].isChecked()
        self.position_worker.set_active(c,active,reset=reset)
        if not active or reset:
            message='彩色原图下启用位置提醒' if not color else '等待位置画面'
            self.views[c].position_badge.reset(message)
        self.views[c].position_badge.refresh()

    def toggle_position_guide(self,c):
        self.settings.setValue('position_guide/'+c,self.position_checks[c].isChecked())
        self.update_position_guide(c,reset=True)

    def position_result(self,c,epoch,result,source_at):
        if self.closing or not self.position_worker.accepts(c,epoch) or time.monotonic()-source_at>MAX_AGE:return
        self.views[c].position_badge.set_result(result,source_at)

    def show_closed(self,c):
        view=self.views[c];view.set_image(None);view.clear()
        view.closed=True
        view.setText('通道 '+c+' 已关闭')

    def select_preview_mode(self,c,mode):
        self.command('/api/v2/config',{'preview':{c:mode}})

    def enlarge_preview(self,c):
        if self.closing:return
        if self.live_viewer is not None:
            if self.live_viewer.camera==c:self.live_viewer.close()
            else:
                self.live_viewer.select_camera(c);self.live_viewer.show();self.live_viewer.raise_();self.live_viewer.activateWindow()
            return
        from live_viewer import LivePreviewDialog
        dialog=LivePreviewDialog(self,c);self.live_viewer=dialog
        def finished(_):
            if self.live_viewer is dialog:self.live_viewer=None
            dialog.deleteLater()
        dialog.finished.connect(finished);dialog.showMaximized();dialog.raise_();dialog.activateWindow()
        self.add_log('打开实时放大画面',camera=c)

    def image(self,c,image):
        if self.previews[c].isChecked():
            self.show_closed(c)
            return
        if image is None:
            self.views[c].alarm_badge.set_result({},self.views[c].alarm_badge.inspection_enabled)
            self.position_worker.set_active(c,False,reset=True)
            self.views[c].position_badge.reset('等待位置画面')
            self.views[c]._image=None;self.views[c].clear();self.views[c].setText(c+' 无信号')
        else: self.views[c].set_image(image)

    def state(self,state):
        incoming=state.get('capture',{});session=incoming.get('session_id');generation=incoming.get('generation')
        if session!=self.capture_session_seen:
            if session in self.capture_retired_sessions:return
            if self.capture_session_seen is not None:self.capture_retired_sessions.add(self.capture_session_seen)
            self.capture_session_seen=session;self.capture_generation_seen=None
        if isinstance(generation,int) and isinstance(self.capture_generation_seen,int) and generation<self.capture_generation_seen:return
        self.config=state['config']
        self.capture_panel.update_state(state)
        capture=state.get('capture',{});generation=capture.get('generation')
        if generation!=self.capture_generation_seen:
            with self.image_lock:self.latest_images.clear();self.last_received.clear()
            self.pre_initialized.clear()
            self.pre_pending.clear();self.detect_pending.clear();self.auto_transport.close()
            self.capture_generation_seen=generation
            for view in self.views.values():view.set_image(None)
        capture_running=capture.get('state','running')=='running'
        for c,worker in zip(('A','B'),self.camera_pollers):
            enabled=state['channels'][c].get('enabled',True) and capture_running
            self.capture_enabled[c]=enabled
            if hasattr(worker,'set_capture_enabled'):worker.set_capture_enabled(enabled)
            if not enabled:
                with self.image_lock:self.latest_images.pop(c,None);self.last_received.pop(c,None)
                self.views[c].set_image(None);self.views[c].setText('相机 '+c+' 已停止' if capture_running else '采集模式切换中…')
        capabilities=state.get('capabilities',{})
        self.mobile_refresh.setEnabled(bool(capabilities.get('browser_preview')) and not self.browser_address_busy)
        if capabilities.get('browser_preview') and not self.browser_network_loaded and not self.browser_address_busy:self.refresh_browser_address()
        self.pre_supported=bool(capabilities.get('preprocess'))
        self.connection_badge.setText('设备已连接');set_property(self.connection_badge,'tone','good')
        sd=state.get('storage',{}).get('sd',{})
        if sd.get('mounted'):
            self.sd_status.setText('SD卡：%s · 可用 %.2f GB · %s'%(sd.get('snapshot_root',sd.get('path','')),sd.get('free_bytes',0)/1024**3,'可写' if sd.get('writable') else sd.get('error','只读')))
        else:self.sd_status.setText('SD卡：'+sd.get('error','请重启新版板端服务以启用截图存储'))
        self.apply_hdmi.setEnabled(bool(capabilities.get('hdmi')) and capture_running)
        self.hdmi.setEnabled(capture_running)
        def hdmi_allowed(value):return value=='off' or value=='split' and all(self.capture_enabled.values()) or value in self.capture_enabled and self.capture_enabled[value]
        for index in range(self.hdmi.count()):self.hdmi.model().item(index).setEnabled(hdmi_allowed(self.hdmi.itemData(index)))
        if not hdmi_allowed(self.hdmi.currentData()):self.hdmi.setCurrentIndex(self.hdmi.findData(self.config['hdmi']))
        self.apply_hdmi.setToolTip('板端已启用 HDMI 窗口' if capabilities.get('hdmi') else '板端需用 sh start_dual_hdmi.sh 启动')
        for c,s in state['channels'].items():
            self.views[c].alarm_badge.set_result(s,self.config['detect'][c])
            self.position_rows[c]=s
            hw=self.codec_controls[c];supported=bool(capabilities.get('fpga_codec')) and state.get('capture',{}).get('build_id','').lower()=='0x5630503b'
            hw['mode'].setEnabled(supported)
            wanted=self.config.get('fpga_codec',{}).get(c)
            if wanted and hw.get('seen')!=wanted and not self.auto_transport.editing('fpga_codec',c):
                hw['mode'].blockSignals(True);hw['mode'].setCurrentIndex(hw['mode'].findData(wanted));hw['mode'].blockSignals(False);hw['seen']=wanted
            actual=s.get('fpga_codec',{})
            if supported and actual:
                mode_name={'raw':'原始块封装','lossless':'LZ77 无损','lossy9':'旧档位（请选择其他模式）','lossy6':'有损压缩·RAW6/灰度5位'}.get(actual.get('mode'),actual.get('mode'))
                value=displayed_fpga_compression(s)
                percent='—' if value is None else '%.1f%%'%value
                label='无损显示 '+percent if actual.get('mode')=='lossless' else percent
                text='当前：%s · 版本 %s\n%s · RK 解码 %.2f ms'%(mode_name,actual.get('epoch'),label,actual.get('decode_ms',0))
                hw['status'].setToolTip(fpga_compression_detail(s))
                if actual.get('mode')!=wanted:text+=' · 等待帧边界切换'
                hw['status'].setText(text)
            else:
                hw['status'].setText('需 P9 位流 0x5630503B 与 3.13.0-p9 服务' if not supported else '等待完整编码帧')
                hw['status'].setToolTip('')
            cfg=self.config.get('transport',{}).get(c);controls=self.net_controls[c]
            is4k=self.config.get('capture',{}).get('profile')=='4k30';full_width=3840 if is4k else 1920
            widths=controls['max_width'];widths.blockSignals(True)
            if is4k and widths.findData(3840)<0:widths.addItem('完整 4K',3840)
            if not is4k and widths.findData(3840)>=0:widths.removeItem(widths.findData(3840))
            widths.setItemText(widths.findData(1920),'1080P 预览' if is4k else '完整 1080P');widths.blockSignals(False)
            controls['fps'].blockSignals(True);controls['fps'].setMaximum(30 if is4k else 60);controls['fps'].blockSignals(False)
            controls['high'].setText('高清 600Mbps' if is4k else '高清 300Mbps')
            selected=('detail' if cfg['max_width']==full_width else 'fast') if cfg else self.config.get('preview',{}).get(c)
            for mode,option in self.preview_modes[c].items():option.setChecked(mode==selected)
            if cfg and cfg!=self.net_seen.get(c) and not self.auto_transport.editing('transport',c):
                for field in ('mode','mbps','fps','max_width','adaptive'):controls[field].blockSignals(True)
                controls['mode'].setCurrentIndex(controls['mode'].findData(cfg['mode']))
                controls['mbps'].setValue(cfg['mbps']);controls['fps'].setValue(cfg['fps'])
                controls['max_width'].setCurrentIndex(controls['max_width'].findData(cfg['max_width']));controls['adaptive'].setChecked(cfg['adaptive'])
                self.net_initialized.add(c)
                self.net_seen[c]=dict(cfg)
                for field in ('mode','mbps','fps','max_width','adaptive'):controls[field].blockSignals(False)
                controls['max_width'].setEnabled(cfg['mode']!='lossless')
            for key in ('low','high','lossless30'):controls[key].setEnabled(bool(capabilities.get('adaptive_transport')) and (key!='lossless30' or self.config.get('capture',{}).get('profile')=='1080p60'))
            for key in ('mode','mbps','fps','max_width','adaptive'):
                controls[key].setEnabled(bool(capabilities.get('adaptive_transport')) and (key!='max_width' or controls['mode'].currentData()!='lossless'))
            net=s.get('transport',{})
            if net.get('codec'):
                quality='Q%d'%net['quality'] if net['codec']=='jpeg' else '像素无损'
                self.net_labels[c].setText('实际：%s %d×%d · %s\n接收 %.1f fps · 带宽 %.1f Mbps · 编码 %.1f ms\n网络数据减少 %.1f%%'%
                    ('高速无损' if net['codec']=='pcz' else net['codec'].upper(),net['width'],net['height'],quality,net.get('delivered_fps',0),net.get('wire_mbps',0),net['encode_ms'],net['saving_pct']))
            phone_enabled=self.config.get('browser_push',{}).get(c,True)
            self.push[c].setText('Wi-Fi 推送: '+('开' if phone_enabled else '关'))
            self.push[c].setEnabled(bool(capabilities.get('browser_push')) and c not in self.browser_push_busy)
            self.push[c].setToolTip('控制手机网页画面；不影响Qt/HDMI/检测' if capabilities.get('browser_push') else '板端需启动v3.10新版服务后生效')
            set_property(self.push[c],'active',phone_enabled)
            self.inspect[c].setEnabled(bool(capabilities.get('defect_crops')) and self.config['detect'][c] and s.get('client_detection_fresh',False) and bool(s.get('client_detections')) and c not in self.inspect_busy)
            paused=self.previews[c].isChecked()
            self.channel_badges[c].setText('预览已关闭' if paused else '实时在线' if s['online'] else '无信号')
            set_property(self.channel_badges[c],'tone','quiet' if paused else 'good' if s['online'] else 'warn')
            self.views[c].layout_overlays()
            self.snapshot_available[c]=s['online'] and bool(capabilities.get('snapshots'))
            supported=bool(capabilities.get('preprocess'))
            if supported and c not in self.pre_initialized:
                for control,value in ((self.pre_modes[c],self.config['pre'][c]),(self.thresholds[c],self.config['threshold'][c])):
                    control.blockSignals(True)
                    if control is self.pre_modes[c]:control.setCurrentIndex(value)
                    else:control.setValue(value)
                    control.blockSignals(False)
                self.pre_initialized.add(c)
            for control in (self.pre_modes[c],self.thresholds[c]):control.setEnabled(supported)
            applied=s.get('mode_applied',0)
            names=('彩色原图','灰度','高斯滤波','Sobel 边缘','直方图均衡')
            requested=self.config.get('pre',{}).get(c,0)
            self.pre_modes[c].setToolTip(('当前处理：'+names[applied]+(' · 等待下一帧切换' if requested!=applied else '')) if supported else '当前 FPGA 未启用预处理，请加载双路预处理位流')
            for control in self.gain_controls[c]:
                control.setEnabled(applied==0)
                control.setToolTip('仅用于彩色原图；FPGA 灰度结果已完成亮度处理')
            mode=('%s %dp'%(net['codec'].upper(),net['height'])) if net.get('codec') else '高清1080P' if self.config.get('preview',{}).get(c)=='detail' else '流畅540P'
            text='采集 %.1f fps\n预览 %.1f fps'%(s['fps'],self.preview_fps[c])
            error=s['error'] or s['push_error']
            self.labels[c].setText(text+('\n'+error if error else ''))
            for kind,buttons in [('detect',self.detect)]:
                buttons[c].setText(('检测' if kind=='detect' else 'Wi-Fi 推送')+(': 开' if self.config[kind][c] else ': 关'))
                set_property(buttons[c],'active',self.config[kind][c])
                buttons[c].setEnabled(kind=='push' or bool(capabilities.get('detect')))
                if kind=='detect':buttons[c].setToolTip('' if capabilities.get('detect') else '板端尚未加载 RKNN 模型')
            if s.get('infer_error'):text='检测错误：'+s['infer_error']
            elif not capabilities.get('detect'):text='三类检测：请用 start_dual_detect.sh 启动板端服务'
            elif not self.config['detect'][c]:text='三类检测：关闭\n缺件\n污渍\n划痕'
            elif not s.get('client_detection_fresh' if net.get('overlay_backend')=='PC local' else 'detection_fresh'):text='三类检测：等待新的彩色检测帧…'
            else:
                current=s.get('client_detections',[]) if net.get('overlay_backend')=='PC local' else s.get('detections',[])
                counts=[sum(d['class_id']==i for d in current) for i in range(3)]
                text='缺件  %d\n污渍  %d\n划痕  %d\n推理 %.1f ms'%(*counts,s.get('infer_ms') or 0)
                if s.get('inference_source_width'):text+='\n检测源 %d×%d'%(s['inference_source_width'],s['inference_source_height'])
            if self.views[c].detection_enabled!=self.config['detect'][c]:
                self.views[c].detection_enabled=self.config['detect'][c];self.views[c].update()
            if self.config['detect'][c] and net.get('overlay_backend')=='PC local':
                self.detection_labels[c].setToolTip('上位机绘制检测框')
            elif self.config['detect'][c]:
                overlay=s.get('preview_overlay')
                if overlay=='tracked':text+='\n实时预览 · 框为短时跟踪'
                elif overlay=='waiting':text+='\n实时预览 · 等待可靠检测框'
            self.detection_labels[c].setText(text)
            set_property(self.detection_labels[c],'tone','warn' if s.get('infer_error') else 'good' if self.config['detect'][c] else 'quiet')
            self.calibrate[c].setEnabled(s['online'] and not self.pre_busy)
            self.measure[c].setEnabled(s['online'] and s['calibrated'] and applied==0 and c not in self.measure_busy)
            self.measure[c].setText('测量中…' if c in self.measure_busy else '测量 '+c)
            self.measure[c].setToolTip('只测板材外框；需固定相机/焦距/PCB顶面高度，并使用彩色原图')
            self.calibration_labels[c].setWordWrap(True)
            self.calibration_labels[c].setText(self.measure_feedback.get(c) or ('板框测量：已标定 · 保持机位/焦距/板面高度' if s['calibrated'] else '板框测量：本路未标定，请先进行棋盘标定'))
            self.update_position_guide(c)
        version=state.get('software_revision','旧服务进程：请Ctrl+C后重启板端服务')
        self.telemetry.update_state(state,self.preview_fps,{c:self.previews[c].isChecked() for c in ('A','B')})
        self.refresh_snapshot_button()
        for c in ('A','B'):
            self.previews[c].setEnabled(self.capture_enabled[c]);self.position_checks[c].setEnabled(self.capture_enabled[c])
            if not self.capture_enabled[c]:
                for fields in (self.detect,self.measure,self.calibrate,self.inspect,self.push):fields[c].setEnabled(False)
                for key in ('low','high','lossless30','mode','mbps','fps','max_width','adaptive'):self.net_controls[c][key].setEnabled(False)
                self.codec_controls[c]['mode'].setEnabled(False);self.pre_modes[c].setEnabled(False);self.thresholds[c].setEnabled(False)
                for control in self.gain_controls[c]:control.setEnabled(False)
                self.position_worker.set_active(c,False,reset=True)
                self.views[c].alarm_badge.set_enabled(False) if hasattr(self.views[c].alarm_badge,'set_enabled') else self.views[c].alarm_badge.set_result({},False)
                self.views[c].position_badge.set_enabled(False);self.channel_badges[c].setText('已停止' if capture_running else '切换中')
            if self.config.get('capture',{}).get('profile')=='4k30':self.pre_modes[c].setEnabled(False);self.thresholds[c].setEnabled(False)
        for c in ('A','B'):sync_transport_controls(self,c)
        self.message.setText('Qt transport-v3.13-ui40-p9  |  板端：'+version+'  |  HDMI：'+self.config['hdmi'])

    def refresh_browser_address(self):
        if self.browser_address_busy:return
        self.browser_address_busy=True;self.mobile_refresh.setEnabled(False)
        worker=Command(self.base,'/api/v2/net',None);self.commands.append(worker)
        def completed(result):
            previous=self.mobile_address.currentData();self.mobile_address.clear()
            for row in result.get('interfaces',[]):self.mobile_address.addItem(('Wi-Fi ' if row['wireless'] else '有线 ')+row['url'],row['url'])
            if not self.mobile_address.count():self.mobile_address.addItem('尚无可用IPv4地址','')
            wireless=[row['url'] for row in result.get('interfaces',[]) if row.get('wireless')]
            preferred=previous if previous in wireless or not wireless else wireless[0]
            index=self.mobile_address.findData(preferred)
            if index>=0:self.mobile_address.setCurrentIndex(index)
            self.mobile_qr.set_network(result)
            self.mobile_open.setEnabled(bool(self.mobile_address.currentData()));self.browser_network_loaded=True
            self.mobile_address.setToolTip('手机与板卡连接同一热点后，请使用Wi-Fi地址；点击刷新可更新。')
        def rejected(error):
            self.mobile_address.setToolTip('获取手机地址失败：'+error);self.mobile_open.setEnabled(False);self.mobile_qr.set_network({})
        def finished():
            self.browser_address_busy=False;self.mobile_refresh.setEnabled(True);self.retire(worker)
        worker.done.connect(completed);worker.error.connect(rejected);worker.finished.connect(finished);worker.start()

    def apply_transport(self,c):
        fields=self.net_controls[c]
        config=dict(mode=fields['mode'].currentData(),mbps=fields['mbps'].value(),fps=fields['fps'].value(),
                    max_width=fields['max_width'].currentData(),adaptive=fields['adaptive'].isChecked())
        self.command('/api/v2/config',{'transport':{c:config}})

    def apply_fpga_codec(self,c):
        self.command('/api/v2/config',{'fpga_codec':{c:self.codec_controls[c]['mode'].currentData()}})

    def toggle_browser_push(self,c):
        if not self.config or c in self.browser_push_busy:return
        self.browser_push_busy.add(c);self.push[c].setEnabled(False)
        def done(state):self.browser_push_busy.discard(c);self.state(state)
        def error(message):self.browser_push_busy.discard(c);self.failed(message)
        self.command('/api/v2/config',{'browser_push':{c:not self.config.get('browser_push',{}).get(c,True)}},done,error)

    def inspect_defects(self,c):
        if c in self.inspect_busy:return
        self.inspect_busy.add(c);self.inspect[c].setEnabled(False);self.inspect[c].setText('获取中…')
        def done(result):
            try:
                from defect_viewer import DefectViewer
                dialog=DefectViewer(result,self);self.dialogs.append(dialog)
                dialog.finished.connect(lambda:self.dialogs.remove(dialog));dialog.finished.connect(dialog.deleteLater);dialog.show()
                self.add_log('已打开同帧高清缺陷截图，检测帧 '+str(result['seq']),camera=c)
            except Exception as exc:self.failed(str(exc))
            finally:finish()
        def finish():self.inspect_busy.discard(c);self.inspect[c].setText('缺陷放大')
        def error(message):finish();self.failed(message)
        self.command('/api/v2/camera/'+c+'/defects',{},done,error)

    def select_snapshot_dir(self):
        directory=QFileDialog.getExistingDirectory(self,'选择上位机截图目录',self.snapshot_dir.text())
        if directory:
            self.snapshot_dir.setText(directory);self.settings.setValue('snapshot/directory',directory)

    def open_snapshot_dir(self):
        try:
            folder=Path(self.snapshot_dir.text()).expanduser().resolve();folder.mkdir(parents=True,exist_ok=True)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):raise OSError('无法打开目录')
        except OSError as exc:self.sd_status.setText('本地目录错误：'+str(exc))

    def snapshot_selection_changed(self,c):
        self.settings.setValue('snapshot/camera_'+c,self.snapshot_channels[c].isChecked())
        self.refresh_snapshot_button()

    def refresh_snapshot_button(self):
        enabled=getattr(self,'capture_enabled',dict(A=True,B=True))
        for c in ('A','B'):self.snapshot_channels[c].setEnabled(enabled[c])
        selected=[c for c in ('A','B') if self.snapshot_channels[c].isChecked() and enabled[c]]
        missing=[c for c in selected if not self.snapshot_available[c]]
        reason=('正在保存 '+ '、'.join(sorted(self.snapshot_busy))+' 的截图' if self.snapshot_busy else
                '请至少勾选 A 或 B 一个截图画面' if not selected else
                '请至少勾选SD卡或上位机一个保存位置' if not (self.save_sd.isChecked() or self.save_pc.isChecked()) else
                '相机 '+ '、'.join(missing)+' 暂不可截图，请等待连接或取消勾选' if missing else '')
        self.snapshot_button.setEnabled(not reason)
        self.snapshot_button.setToolTip(reason or ('保存画面 '+ '、'.join(selected)+' 的带框检测PNG，需开启本路检测；按日期/A或B归档，文件名包含划痕、缺失、污渍数量'))
        return selected,reason

    def take_selected_snapshots(self):
        selected,reason=self.refresh_snapshot_button()
        if reason:
            self.message.setText(reason);return
        for c in selected:self.take_snapshot(c)

    def take_snapshot(self,c):
        if not getattr(self,'capture_enabled',dict(A=True,B=True))[c]:return
        if c in self.snapshot_busy:return
        self.output_tabs.setCurrentWidget(self.storage_page)
        sd,pc=self.save_sd.isChecked(),self.save_pc.isChecked()
        label=self.snapshot_labels[c];label.show();label.setStyleSheet('color:#32465e');label.setToolTip('')
        if not (sd or pc):label.setText(c+'：请至少勾选SD卡或上位机一个保存位置');return
        self.snapshot_busy.add(c);self.refresh_snapshot_button()
        self.add_log('请求截图：'+('SD卡 ' if sd else '')+('上位机' if pc else ''),camera=c)
        label.setText(c+'：正在截图并保存，请稍候…')
        worker=SnapshotWorker(self.base,c,sd,pc,self.snapshot_dir.text());self.commands.append(worker)
        def completed(result):
            self.snapshot_busy.discard(c);parts=[];details=[];failed=False
            for key,title in [('sd','SD卡'),('pc','上位机')]:
                status=result[key]
                if status['requested']:
                    failed|=not status['ok']
                    parts.append(title+'：'+('已保存' if status['ok'] else '保存失败，'+status['error']))
                    details.append(title+'：'+(status['path'] if status['ok'] else status['error']))
            label.setText(c+' · '+'；'.join(parts));label.setStyleSheet('color:#b42318' if failed else 'color:#166534')
            label.setToolTip('\n'.join(details))
            self.add_log('；'.join(details),'WARN' if failed else 'INFO',camera=c)
            self.refresh_snapshot_button()
        def rejected(error):
            self.snapshot_busy.discard(c);label.setText(c+'：截图请求失败：'+error);label.setStyleSheet('color:#b42318')
            self.add_log('截图请求失败：'+error,'ERROR',camera=c)
            self.refresh_snapshot_button()
        worker.done.connect(completed);worker.error.connect(rejected)
        worker.finished.connect(lambda:self.retire(worker));worker.start()

    def failed(self,error):
        self.message.setText('请求失败：'+error)
        if error!=self.last_ui_error or time.monotonic()-self.last_ui_error_at>=5:
            self.add_log('请求失败：'+error,'ERROR');self.last_ui_error=error;self.last_ui_error_at=time.monotonic()

    def connection_failed(self,error):
        self.snapshot_available=dict(A=False,B=False);self.refresh_snapshot_button()
        for view in self.views.values():view.alarm_badge.set_result({},view.alarm_badge.inspection_enabled)
        for c in ('A','B'):
            self.position_rows[c]={}
            self.update_position_guide(c,reset=True)
        self.connection_badge.setText('设备连接中断');set_property(self.connection_badge,'tone','warn');self.failed(error)
        self.telemetry.update_state(None,self.preview_fps,{c:True for c in ('A','B')})

    def toggle(self,kind,c):
        if self.config:
            body={kind:{c:not self.config[kind][c]}}
            if kind=='detect':
                enabled=not self.detect_pending.get(c,self.config['detect'][c])
                self.detect_pending[c]=enabled
                if enabled:
                    self.pre_pending.pop(c,None)
                    if self.pre_supported:self.pre_pending[c]=(0,self.thresholds[c].value())
                    self.pre_modes[c].blockSignals(True);self.pre_modes[c].setCurrentIndex(0);self.pre_modes[c].blockSignals(False)
                self.pre_timer.stop();self.flush_pre();return
            self.command('/api/v2/config',body)

    def schedule_pre(self,c,immediate):
        if self.closing or not self.pre_modes[c].isEnabled():return
        if immediate:self.detect_pending.pop(c,None)
        self.pre_pending[c]=(self.pre_modes[c].currentIndex(),self.thresholds[c].value())
        self.update_position_guide(c,reset=True)
        self.pre_modes[c].setToolTip('正在切换：'+self.pre_modes[c].currentText())
        # Modes apply immediately; repeated threshold edits coalesce for 40 ms.
        if immediate:
            self.pre_timer.stop();self.flush_pre()
        else:self.pre_timer.start(40)

    def flush_pre(self):
        if self.closing or self.pre_busy or not (self.pre_pending or self.detect_pending):return
        pending=self.pre_pending;self.pre_pending={};self.pre_busy=True
        body={}
        if pending:body.update(pre={c:v[0] for c,v in pending.items()},threshold={c:v[1] for c,v in pending.items()})
        if self.detect_pending:body['detect']=self.detect_pending;self.detect_pending={}
        worker=Command(self.base,'/api/v2/config',body);self.commands.append(worker)
        worker.done.connect(self.state);worker.error.connect(self.failed)
        worker.finished.connect(lambda:self.pre_finished(worker));worker.start()

    def pre_finished(self,worker):
        self.pre_busy=False;self.retire(worker)
        if not self.pre_timer.isActive():self.flush_pre()

    def command(self,path,body,callback=None,error_callback=None):
        worker=Command(self.base,path,body);self.commands.append(worker)
        worker.done.connect(callback or self.state);worker.error.connect(error_callback or self.failed)
        worker.finished.connect(lambda:self.retire(worker));worker.start()

    def retire(self,worker):
        self.commands.remove(worker);worker.deleteLater()

    def calibration(self,c):
        if self.pre_busy:
            self.message.setText('请等待当前图像算法切换完成，再开始标定');return
        body={'detect':{c:False}}
        if self.pre_supported:body['pre']={c:0}
        self.pre_pending.pop(c,None);self.detect_pending.pop(c,None)
        def opened(state):
            self.measure_feedback.pop(c,None)
            self.calibration_labels[c].setStyleSheet('')
            self.state(state)
            dialog=CalibrationDialog(self,c);self.dialogs.append(dialog)
            dialog.finished.connect(lambda:self.dialogs.remove(dialog));dialog.finished.connect(dialog.deleteLater);dialog.show()
        self.command('/api/v2/config',body,opened)

    def load_calibration(self,c):
        path,_=QFileDialog.getOpenFileName(self,'导入相机 '+c+' 当前机位标定','','JSON (*.json)')
        if not path:return
        try:
            from pathlib import Path
            data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        except Exception as exc:self.failed(str(exc));return
        self.command('/api/v2/camera/'+c+'/calibration/import',data,lambda _:self.message.setText('相机 '+c+' 标定已导入'))

    def start_measurement(self,c):
        if c in self.measure_busy:return
        self.channel_tabs[c].setCurrentIndex(1)
        self.measure_busy.add(c)
        self.measure[c].setEnabled(False);self.measure[c].setText('测量中…')
        self.measure_feedback[c]='正在测量板框，请稍候…'
        self.add_log('请求板框测量',camera=c)
        self.calibration_labels[c].setStyleSheet('color:#32465e')
        self.calibration_labels[c].setText(self.measure_feedback[c])
        def completed(result):
            self.measure_busy.discard(c)
            self.measure[c].setText('测量 '+c)
            r=result['result']
            self.measure_feedback[c]='板框 %.2f × %.2f mm（初测），已打开绿色框线定格图'%(r['long_mm'],r['short_mm'])
            self.calibration_labels[c].setStyleSheet('color:#166534')
            self.calibration_labels[c].setText(self.measure_feedback[c])
            self.measured(result)
            self.add_log('绿色板框测量定格图已显示',camera=c)
        def rejected(error):
            self.measure_busy.discard(c)
            self.measure[c].setText('测量 '+c)
            self.measure_feedback[c]='板框测量失败：'+error
            self.add_log(self.measure_feedback[c],'ERROR',camera=c)
            self.calibration_labels[c].setStyleSheet('color:#b42318')
            self.calibration_labels[c].setText(self.measure_feedback[c])
        self.command('/api/v2/camera/'+c+'/measure',{},completed,rejected)

    def measured(self,result):
        r=result['result'];dialog=QDialog(self);dialog.resize(1100,720)
        dialog.setWindowTitle('相机 '+result['camera']+' 测量定格图')
        layout=QVBoxLayout(dialog)
        layout.addWidget(QLabel('板框 %.2f × %.2f mm（初测）'%(r['long_mm'],r['short_mm'])))
        view=AspectLabel();view.set_image(QImage.fromData(base64.b64decode(result['png'])));layout.addWidget(view,1)
        self.dialogs.append(dialog);dialog.finished.connect(lambda:self.dialogs.remove(dialog));dialog.finished.connect(dialog.deleteLater);dialog.show()

    def photographed(self,result):
        dialog=QDialog(self);dialog.resize(1200,800)
        dialog.setWindowTitle('相机 '+result['camera']+' · 1920×1080 原尺寸（滚动查看）')
        layout=QVBoxLayout(dialog);scroll=QScrollArea(dialog)
        label=QLabel();label.setPixmap(QPixmap.fromImage(QImage.fromData(result['jpeg'])))
        scroll.setWidget(label);layout.addWidget(scroll)
        self.dialogs.append(dialog);dialog.finished.connect(lambda:self.dialogs.remove(dialog));dialog.finished.connect(dialog.deleteLater);dialog.show()

    def closeEvent(self,event):
        self.closing=True;self.browser_address_timer.stop();self.pre_timer.stop();self.pre_pending.clear();self.detect_pending.clear()
        if self.live_viewer is not None:self.live_viewer.close()
        if self.mobile_qr.popup is not None:self.mobile_qr.popup.close()
        self.auto_transport.close()
        self.render_timer.stop()
        self.position_worker.stop()
        self.poller.requestInterruption()
        self.log_poller.stop()
        for worker in self.camera_pollers:worker.stop()
        for worker in [self.poller,self.log_poller,self.position_worker]+self.camera_pollers:worker.wait()
        for w in list(self.commands): w.wait()
        event.accept()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--base',default='http://192.168.10.50:8001')
    args=parser.parse_args();app=QApplication([]);window=DualWindow(args.base);window.show();app.exec_()


if __name__=='__main__': main()
