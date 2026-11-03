"""Bounded, low-rate PC framing check and a Qt movement badge."""
import threading
import time
from PyQt5.QtCore import QThread, QTimer, Qt, QRect, pyqtSignal
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QLabel
from pcb_position import locate_pcb, PositionConfirmation, cv2, np

MAX_AGE = 1.5


class PositionWorker(QThread):
    resultReady = pyqtSignal(str, int, object, float)

    def __init__(self, parent=None, interval=.25):
        super().__init__(parent)
        self.interval = interval
        self.condition = threading.Condition()
        self.pending = {}
        self.active = {c: False for c in 'AB'}
        self.epochs = {c: 0 for c in 'AB'}
        self.stopped = False

    def set_active(self, camera, active, reset=False):
        with self.condition:
            if self.active[camera] != active or reset:
                self.epochs[camera] += 1
                self.pending.pop(camera, None)
            self.active[camera] = active

    def accepts(self, camera, epoch):
        with self.condition:
            return self.active[camera] and self.epochs[camera] == epoch and not self.stopped

    def submit(self, camera, image):
        # Retain the immutable QImage; scaling/copying/vision happen off the GUI
        # and receiver threads. At most one pending image per camera is held.
        with self.condition:
            if self.active[camera] and not self.stopped:
                self.pending[camera] = (self.epochs[camera], image, time.monotonic())
                self.condition.notify()

    def stop(self):
        with self.condition:
            self.stopped = True
            self.pending.clear()
            self.condition.notify_all()

    @staticmethod
    def analyze(image):
        if cv2 is None:
            return locate_pcb(None)
        # Decode_image retains RGB arrays for optical flow. Resize those arrays
        # directly when present; fallback also supports the Qt JPEG decoder.
        pixels = getattr(image, '_preview_pixels', None)
        if pixels is not None:
            height, width = pixels.shape[:2]
            size = (min(480, width), max(1, round(height * min(1., 480. / width))))
            small = cv2.resize(pixels, size, interpolation=cv2.INTER_AREA)
        else:
            q = image.scaled(480, 480, Qt.KeepAspectRatio, Qt.FastTransformation).convertToFormat(QImage.Format_RGB888)
            if q.isNull():
                return locate_pcb(None)
            bits = q.constBits(); bits.setsize(q.byteCount())
            small = np.frombuffer(bits, np.uint8).reshape(q.height(), q.bytesPerLine())[:, :q.width()*3].reshape(q.height(), q.width(), 3)
        return locate_pcb(cv2.cvtColor(small, cv2.COLOR_RGB2BGR))

    def run(self):
        filters = {}
        next_at = 0.
        while True:
            with self.condition:
                while not self.stopped:
                    delay = next_at - time.monotonic()
                    if self.pending and delay <= 0:
                        break
                    self.condition.wait(max(.001, delay) if self.pending else None)
                if self.stopped:
                    return
                batch = self.pending; self.pending = {}
            next_at = time.monotonic() + self.interval
            for camera, (epoch, image, source_at) in batch.items():
                if not self.accepts(camera, epoch) or time.monotonic() - source_at > MAX_AGE:
                    continue
                old_epoch, confirmation, last_at = filters.get(camera, (None, None, 0.))
                if old_epoch != epoch or source_at-last_at > MAX_AGE:
                    confirmation = PositionConfirmation()
                filters[camera] = (epoch, confirmation, source_at)
                try:
                    result = confirmation.update(self.analyze(image))
                except Exception:
                    # An auxiliary check must not terminate the video receiver.
                    confirmation = PositionConfirmation(); filters[camera] = (epoch, confirmation, source_at)
                    result = dict(kind='unknown', text='位置识别失败，请检查画面', directions=[], edges=[])
                if self.accepts(camera, epoch) and time.monotonic() - source_at <= MAX_AGE:
                    self.resultReady.emit(camera, epoch, result, source_at)
            # Drop the last source reference while idle, including after disable.
            batch.clear(); image = None


class PositionBadge(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self.kind = 'idle'; self.message = '等待位置画面'; self.source_at = 0.; self.guide_enabled = False
        self.setObjectName('PositionGuide')
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setWordWrap(True)
        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh); self.timer.start(250)
        self.refresh()

    def reset(self, text='等待位置画面'):
        self.kind = 'idle'; self.message = text; self.source_at = 0.; self.refresh()

    def set_enabled(self, enabled):
        self.guide_enabled = bool(enabled); self.refresh()

    def set_result(self, result, source_at):
        self.kind = result['kind']; self.message = result['text']; self.source_at = source_at; self.refresh()

    def refresh(self):
        if not self.guide_enabled or getattr(self.parent(), 'closed', False):
            self.hide(); return
        fresh = self.source_at and time.monotonic() - self.source_at <= MAX_AGE
        kind = self.kind if fresh else 'idle'
        message = self.message if fresh or self.kind == 'idle' else '等待新位置画面'
        color = '#975717' if kind in ('move', 'too_large') else '#216547' if kind == 'centered' else '#3c5065'
        text = message
        style = 'background:'+color+';color:white;font-size:21px;font-weight:700;padding:7px 12px;border-radius:6px;'
        if self.text() != text: self.setText(text)
        if self.styleSheet() != style: self.setStyleSheet(style)
        self.place()
        self.show()

    def place(self):
        self.ensurePolished()
        metrics = self.fontMetrics()
        parent = self.parent(); bounds = getattr(parent, 'image_rect', None)
        rect = bounds().toRect() if callable(bounds) else parent.rect()
        width = min(max(80, rect.width()-20), metrics.horizontalAdvance(self.text())+40)
        self.setFixedWidth(width)
        height = metrics.boundingRect(QRect(0, 0, max(1, width-40), 1000), Qt.TextWordWrap, self.text()).height()+16
        self.setFixedHeight(max(28, height))
        alarm = getattr(self.parent(), 'alarm_badge', None)
        bottom = alarm.height()+20 if alarm is not None and not alarm.isHidden() else 10
        self.move(rect.left()+10, max(rect.top()+50, rect.bottom()+1-bottom-self.height()))
