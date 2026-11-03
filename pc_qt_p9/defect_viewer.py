"""Frozen full-resolution crops from the exact inference reference frame."""
import base64,math
from PyQt5.QtCore import Qt,QSize
from PyQt5.QtGui import QImage,QPixmap,QIcon
from PyQt5.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLabel,QListWidget,QListWidgetItem,QScrollArea,QPushButton,QSlider,QFileDialog


def decode_crops(result):
    image=QImage.fromData(base64.b64decode(result['png'],validate=True))
    if image.isNull() or image.width()!=result['width'] or image.height()!=result['height']:raise ValueError('缺陷原图尺寸或PNG无效')
    crops=[]
    for d in result['detections'][:16]:
        box=d['box']
        if len(box)!=4 or not all(math.isfinite(v) for v in box):raise ValueError('缺陷坐标无效')
        x0,y0=max(0,math.floor(box[0])),max(0,math.floor(box[1]));x1,y1=min(image.width(),math.ceil(box[2])),min(image.height(),math.ceil(box[3]))
        if x1>x0 and y1>y0:crops.append((d,image.copy(x0,y0,x1-x0,y1-y0)))
    if not crops:raise ValueError('本次结果没有可查看的缺陷框')
    return crops


class DefectViewer(QDialog):
    def __init__(self,result,parent=None):
        super().__init__(parent);self.result=result;self.crops=decode_crops(result)
        self.setWindowTitle('相机 '+result['camera']+' · 缺陷高清截图');self.resize(1120,760)
        col=QVBoxLayout(self);col.addWidget(QLabel('相机 %s · 检测帧 %s · 原图 %d×%d · 定格截图'%(result['camera'],result['seq'],result['width'],result['height'])))
        body=QHBoxLayout();col.addLayout(body,1)
        self.list=QListWidget();self.list.setFixedWidth(240);self.list.setIconSize(QSize(190,115))
        self.list.setViewMode(QListWidget.IconMode);self.list.setMovement(QListWidget.Static);self.list.setResizeMode(QListWidget.Adjust)
        self.list.setWordWrap(True);self.list.setGridSize(QSize(215,180));self.list.setSpacing(4);body.addWidget(self.list)
        self.area=QScrollArea();self.area.setWidgetResizable(False);body.addWidget(self.area,1)
        self.picture=QLabel();self.picture.setStyleSheet('background:#142b3e;');self.area.setWidget(self.picture)
        for i,(d,img) in enumerate(self.crops):
            item=QListWidgetItem(QIcon(QPixmap.fromImage(img)),f"{i+1}. {d.get('label','缺陷')} {d['score']:.2f}\n{img.width()}×{img.height()} px")
            item.setSizeHint(QSize(210,175));self.list.addItem(item)
        row=QHBoxLayout();col.addLayout(row);self.detail=QLabel();row.addWidget(self.detail)
        self.zoom=QSlider(Qt.Horizontal);self.zoom.setRange(50,800);self.zoom.setValue(200);row.addWidget(self.zoom,1)
        one=QPushButton('100%');one.clicked.connect(lambda:self.zoom.setValue(100));row.addWidget(one)
        fit=QPushButton('适合窗口');fit.clicked.connect(self.fit);row.addWidget(fit)
        save=QPushButton('保存本缺陷PNG');save.clicked.connect(self.save);row.addWidget(save)
        self.list.currentRowChanged.connect(self.render);self.zoom.valueChanged.connect(self.render);self.list.setCurrentRow(0)

    def render(self,*_):
        index=self.list.currentRow()
        if index<0:return
        d,img=self.crops[index];factor=self.zoom.value()/100
        pix=QPixmap.fromImage(img).scaled(max(1,round(img.width()*factor)),max(1,round(img.height()*factor)),Qt.KeepAspectRatio,Qt.FastTransformation)
        self.picture.setPixmap(pix);self.picture.resize(pix.size());self.detail.setText(f"{d.get('label','缺陷')} · {self.zoom.value()}%")

    def fit(self):
        img=self.crops[self.list.currentRow()][1];size=self.area.viewport().size()
        self.zoom.setValue(max(50,min(800,int(min(size.width()/img.width(),size.height()/img.height())*100))))

    def save(self):
        i=self.list.currentRow();name=f"PCB_{self.result['camera']}_Defect_f{self.result['seq']}_{i+1}.png"
        path,_=QFileDialog.getSaveFileName(self,'保存缺陷高清截图',name,'PNG (*.png)')
        if path and not self.crops[i][1].save(path,'PNG'):self.detail.setText('保存失败，请检查目录权限')
