"""Enlarge an existing live view without another stream, decoder or pixel copy."""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QDialog,QWidget,QVBoxLayout,QHBoxLayout,QLabel,
                            QPushButton,QButtonGroup,QShortcut,QSizePolicy)


class LivePreviewDialog(QDialog):
    def __init__(self,station,camera):
        super().__init__(station,Qt.Window|Qt.WindowMinMaxButtonsHint|Qt.WindowCloseButtonHint)
        self.station=station;self.camera=None;self.view=None;self.was_maximized=True
        self.resize(1440,900);self.setMinimumSize(640,420)
        layout=QVBoxLayout(self);layout.setContentsMargins(10,10,10,10);layout.setSpacing(8)
        toolbar=QHBoxLayout();toolbar.setSpacing(8);layout.addLayout(toolbar)
        heading=QLabel('实时放大');heading.setProperty('role','channelTitle');toolbar.addWidget(heading)
        group=QButtonGroup(self);group.setExclusive(True);self.channels={}
        for c in ('A','B'):
            button=QPushButton('相机 '+c);button.setProperty('role','previewMode');button.setCheckable(True)
            button.clicked.connect(lambda _,c=c:self.select_camera(c));group.addButton(button);toolbar.addWidget(button)
            self.channels[c]=button
        toolbar.addStretch(1)
        self.fullscreen=QPushButton('全屏查看');self.fullscreen.setProperty('role','outline');self.fullscreen.setCheckable(True)
        self.fullscreen.clicked.connect(self.toggle_fullscreen);toolbar.addWidget(self.fullscreen)
        close=QPushButton('返回双路');close.setProperty('role','accent');close.clicked.connect(self.reject);toolbar.addWidget(close)
        self.canvas=QWidget();self.canvas.setStyleSheet('background:#101010;');layout.addWidget(self.canvas,1)
        self.video_layout=QVBoxLayout(self.canvas);self.video_layout.setContentsMargins(0,0,0,0)
        hint=QLabel('双击画面或按 Esc 返回双路 · F11 切换全屏');hint.setProperty('role','secondary');layout.addWidget(hint)
        shortcut=QShortcut(QKeySequence('F11'),self);shortcut.activated.connect(self.toggle_fullscreen)
        self.select_camera(camera)

    def select_camera(self,camera):
        if camera not in self.channels:raise ValueError('Unknown preview channel')
        if self.camera==camera:return
        self.restore_view()
        self.camera=camera;self.view=self.station.preview_wells[camera].take_view()
        self.old_maximum_width=self.view.maximumWidth();self.old_policy=self.view.sizePolicy()
        self.view.setMaximumWidth(16777215);self.view.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding)
        self.video_layout.addWidget(self.view,1);self.view.show()
        self.channels[camera].setChecked(True);self.setWindowTitle('相机 '+camera+' · 实时放大')

    def restore_view(self):
        if self.view is None:return
        view=self.view;camera=self.camera;self.view=None;self.camera=None
        self.video_layout.removeWidget(view)
        view.setMaximumWidth(self.old_maximum_width);view.setSizePolicy(self.old_policy)
        self.station.preview_wells[camera].restore_view()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showMaximized() if self.was_maximized else self.showNormal()
        else:
            self.was_maximized=self.isMaximized();self.showFullScreen()
        self.fullscreen.setChecked(self.isFullScreen())
        self.fullscreen.setText('退出全屏' if self.isFullScreen() else '全屏查看')

    def done(self,result):
        self.restore_view();super().done(result)

    def closeEvent(self,event):
        self.restore_view();super().closeEvent(event)
