"""Local QR rendering for the board's discovered wireless browser address."""
import ipaddress
from urllib.parse import urlsplit
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtWidgets import QFrame, QVBoxLayout, QPushButton, QLabel, QDialog
from pcb_station import qr_png_bytes


def wireless_url(info):
    for row in info.get('interfaces', []):
        if not row.get('wireless'):
            continue
        try:
            url = row['url']
            parsed = urlsplit(url)
            addr = ipaddress.IPv4Address(row['addr'])
            if (parsed.scheme == 'http' and parsed.hostname == str(addr)
                    and parsed.port and parsed.path == '/m' and not parsed.query
                    and not parsed.fragment and not parsed.username
                    and not (addr.is_loopback or addr.is_unspecified or addr.is_multicast)):
                return url
        except (KeyError, ValueError, TypeError):
            continue
    return ''


def qr_pixmap(url, size):
    data = qr_png_bytes(url, scale=1, border=4)
    original = QPixmap()
    if not data or not original.loadFromData(data, 'PNG'):
        return QPixmap()
    factor = size // original.width()
    if factor < 1:
        return QPixmap()
    # Whole module pixels and a four-module quiet zone keep the small QR crisp.
    edge = original.width() * factor
    return original.scaled(edge, edge, Qt.KeepAspectRatio, Qt.FastTransformation)


class MobileQrPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.url = ''
        self.popup = None
        self.setFixedWidth(132)
        col = QVBoxLayout(self)
        col.setContentsMargins(2, 0, 2, 0)
        col.setSpacing(3)
        self.code = QPushButton()
        self.code.setObjectName('MobileQrCode')
        self.code.setFixedSize(124, 124)
        self.code.setStyleSheet('QPushButton {background:white;color:#627a90;border:1px solid #dbe5ef;border-radius:6px;padding:0;font-size:16px;}')
        self.code.setAccessibleName('手机操作台二维码，点击放大')
        self.code.clicked.connect(self.open_large)
        col.addWidget(self.code, 0, Qt.AlignHCenter)
        self.caption = QLabel()
        self.caption.setAlignment(Qt.AlignCenter)
        self.caption.setStyleSheet('font-size:16px;color:#627a90;')
        col.addWidget(self.caption)
        col.addStretch(1)
        self.set_network({})

    def set_network(self, info):
        url = wireless_url(info)
        if url == self.url and (self.code.text() or not self.code.icon().isNull()):
            return
        if url != self.url and self.popup is not None:
            self.popup.close()
        self.url = url
        self.code.setIcon(QIcon())
        self.code.setEnabled(False)
        if not url:
            self.code.setText('手机扫码\n等待连接热点')
            self.code.setToolTip('板卡连接手机热点并获取无线IP后，自动显示二维码。')
            self.caption.setText('等待 Wi-Fi')
            return
        image = qr_pixmap(url, 116)
        if image.isNull():
            self.code.setText('二维码不可用')
            self.code.setToolTip('请安装segno；手机也可输入无线地址：' + url)
            self.caption.setText('请使用无线地址')
            return
        self.code.setText('')
        self.code.setIcon(QIcon(image))
        self.code.setIconSize(image.size())
        self.code.setEnabled(True)
        self.code.setToolTip('手机浏览器扫码打开操作台，点击放大\n' + url)
        self.caption.setText('手机扫码 · 放大')

    def open_large(self):
        if not self.url or not self.code.isEnabled():
            return
        if self.popup is not None:
            self.popup.raise_()
            self.popup.activateWindow()
            return
        image = qr_pixmap(self.url, 420)
        if image.isNull():
            return
        dialog = QDialog(self)
        self.popup = dialog
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.setWindowTitle('手机扫码打开操作台')
        col = QVBoxLayout(dialog)
        col.setContentsMargins(24, 18, 24, 18)
        col.setSpacing(12)
        code = QLabel()
        code.setStyleSheet('background:white;padding:12px;')
        code.setAlignment(Qt.AlignCenter)
        code.setPixmap(image)
        col.addWidget(code)
        address = QLabel(self.url)
        address.setTextInteractionFlags(Qt.TextSelectableByMouse)
        address.setAlignment(Qt.AlignCenter)
        col.addWidget(address)
        note = QLabel('板卡连接手机热点后，用手机浏览器的扫一扫打开。')
        note.setWordWrap(True)
        note.setAlignment(Qt.AlignCenter)
        col.addWidget(note)
        close = QPushButton('关闭')
        close.clicked.connect(dialog.close)
        col.addWidget(close)
        dialog.destroyed.connect(self._popup_closed)
        dialog.show()

    def _popup_closed(self):
        self.popup = None
