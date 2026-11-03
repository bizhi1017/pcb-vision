"""Bounded telemetry; true byte rates plus an explicit lossless display convention."""
from collections import deque
import json
import math
from pathlib import Path
import time
from PyQt5.QtCore import Qt,QPointF
from PyQt5.QtGui import QColor,QPainter,QPainterPath,QPen,QFont,QFontMetrics,QTextDocument
from PyQt5.QtWidgets import QWidget,QFrame,QLabel,QVBoxLayout,QHBoxLayout,QSizePolicy

COLORS=('#236ee8','#168879')


class MetricLabel(QLabel):
    """Keep wider typography from increasing the workspace's minimum width."""
    def __init__(self,text='',large=20,minimum=19):
        super().__init__(text);self.large=large;self.minimum=minimum;self.last_px=None
        self.setMinimumWidth(0);self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)

    def fit_font(self):
        for px in range(self.large,self.minimum-1,-1):
            font=QFont(self.font());font.setPixelSize(px)
            doc=QTextDocument();doc.setDocumentMargin(0);doc.setDefaultFont(font);doc.setHtml(self.text())
            if doc.idealWidth()<=self.width():break
        if self.last_px!=px:self.last_px=px;self.setStyleSheet('font-size:%dpx;'%px)

    def resizeEvent(self,event):
        super().resizeEvent(event);self.fit_font()

    def setText(self,text):
        super().setText(text)
        if hasattr(self,'large'):self.fit_font()


def number(value):
    return float(value) if type(value) in (int,float) and math.isfinite(value) and value>=0 else None


def fpga_compression(row):
    """Percent byte reduction on the FPGA->RK wire, including frame overhead."""
    if not row.get('online') or not row.get('enabled',True) or row.get('error'):return None
    codec=row.get('fpga_codec',{});raw=number(codec.get('raw_bytes'));wire=number(codec.get('wire_bytes'))
    if not raw or not wire:return None
    percent=100*(1-wire/raw)
    return percent if math.isfinite(percent) else None


def displayed_fpga_compression(row):
    """UI convention only: valid lossless frames display 0; true counters stay intact."""
    actual=fpga_compression(row)
    if actual is None:return None
    return 0. if row.get('fpga_codec',{}).get('mode')=='lossless' else actual


def fpga_compression_detail(row):
    codec=row.get('fpga_codec',{});actual=fpga_compression(row)
    if actual is None:return '等待有效FPGA帧'
    text='原始 %.0f B → PCIe %.0f B；实际数据节省 %.2f%%'%(codec['raw_bytes'],codec['wire_bytes'],actual)
    if codec.get('mode')=='lossless':text='无损以0.0%为显示基准（展示口径，不代表实际节省率）；'+text
    return text


def measurements(state,preview_fps,paused):
    result={key:[] for key in ('fps','latency','compression','inference')}
    for c in ('A','B'):
        s=(state or {}).get('channels',{}).get(c,{})
        online=bool(s.get('online'));net=s.get('transport',{})
        live=online and not paused.get(c,False)
        delivered=live and net.get('delivered_fps',0)>0
        result['fps'].append(number(preview_fps.get(c)) if live else 0. if online else None)
        result['latency'].append(number(net.get('delivery_ms')) if delivered else None)
        # Capture keeps running when the operator pauses only the Qt preview.
        result['compression'].append(displayed_fpga_compression(s))
        detected=(state or {}).get('config',{}).get('detect',{}).get(c,False)
        fresh=online and detected and s.get('client_detection_fresh' if net.get('overlay_backend')=='PC local' else 'detection_fresh') and not s.get('infer_error')
        result['inference'].append(number(s.get('infer_ms')) if fresh else None)
    return result


def trend_bounds(values):
    low=min(0.,min(values));high=max(0.,max(values))
    return (low*1.15 if low<0 else 0.,max(1.,high*1.15))


class TrendLine(QWidget):
    def __init__(self):
        super().__init__();self.samples=deque(maxlen=60);self.setMinimumHeight(58)

    def append(self,values):
        now=time.monotonic()
        if not self.samples or now-self.samples[-1][0]>=1:
            self.samples.append((now,*values));self.update()

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        w,h=self.width()-2,self.height()-3
        p.setPen(QPen(QColor('#e4ebf2'),1));p.drawLine(1,h,w,h);p.drawLine(1,h//2,w,h//2)
        valid=[v for row in self.samples for v in row[1:] if v is not None]
        if not valid:return
        low,high=trend_bounds(valid);now=time.monotonic()
        def ordinate(value):return h-(value-low)/(high-low)*(h-3)
        if low<0:
            p.setPen(QPen(QColor('#bdcddd'),1,Qt.DotLine));p.drawLine(QPointF(1,ordinate(0)),QPointF(w,ordinate(0)))
        for index,color in enumerate(COLORS,1):
            path=QPainterPath();connected=False;last=None
            for row in self.samples:
                x=1+(row[0]-now+60)/60*(w-2);value=row[index]
                if value is None:connected=False;continue
                point=QPointF(x,ordinate(value))
                if connected:path.lineTo(point)
                else:path.moveTo(point)
                last=point;connected=True
            p.setBrush(Qt.NoBrush);p.setPen(QPen(QColor(color),2.5));p.drawPath(path)
            if last is not None and self.samples[-1][index] is not None:
                p.setBrush(QColor(color));p.drawEllipse(last,2,2)


class TrendCard(QFrame):
    def __init__(self,title,unit,hint,description):
        super().__init__();self.setProperty('role','metric');self.unit=unit
        col=QVBoxLayout(self);col.setContentsMargins(12,8,12,8);col.setSpacing(3)
        title_label=MetricLabel(title,20,19);title_label.setProperty('role','trendTitle');col.addWidget(title_label)
        self.value=MetricLabel('',23,22);self.value.setProperty('role','trendValue');col.addWidget(self.value)
        self.line=TrendLine();col.addWidget(self.line,1)
        self.hint=MetricLabel(hint,16,15);self.hint.setProperty('role','trendHint');col.addWidget(self.hint)
        self.setToolTip(description);self.set_values((None,None))

    def set_values(self,values):
        parts=[]
        for c,value,color in zip(('A','B'),values,COLORS):
            text='—' if value is None else '%.1f'%value
            parts.append('<span style="color:%s">%s %s</span>'%(color,c,text))
        self.value.setText(' &nbsp; '.join(parts)+' <span style="font-size:18px;color:#718399">'+self.unit+'</span>');self.line.append(values)


class ResourceBars(QWidget):
    def __init__(self,resources):
        super().__init__();self.resources=resources;self.setMinimumHeight(96)

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        font=QFont('Microsoft YaHei UI');font.setPixelSize(18);p.setFont(font)
        for i,(name,row) in enumerate(self.resources.items()):
            y=i*31;p.setPen(QColor('#536d83'));pct=100*row['used']/row['total']
            p.drawText(0,y,self.width(),23,Qt.AlignLeft|Qt.AlignVCenter,'%s %.2f%%'%(name,pct))
            p.setPen(Qt.NoPen);p.setBrush(QColor('#e4ebf2'));p.drawRoundedRect(0,y+24,self.width(),4,2,2)
            p.setBrush(QColor(('#236ee8','#168879','#b48532')[i%3]));p.drawRoundedRect(0,y+24,round(self.width()*pct/100),4,2,2)


class ResourceCard(QFrame):
    def __init__(self):
        super().__init__();self.setProperty('role','metric')
        col=QVBoxLayout(self);col.setContentsMargins(12,8,12,8);col.setSpacing(3)
        title=MetricLabel('FPGA 资源 · 发布配置',20,19);title.setProperty('role','trendTitle');col.addWidget(title)
        self.profile={}
        try:
            self.profile=json.loads(Path(__file__).with_name('fpga_resources.json').read_text(encoding='utf-8'))
            resources=self.profile['resources']
            if set(resources)!={'Slice','ERAM','DSP'} or any(not 0<=r['used']<=r['total'] or r['total']<=0 for r in resources.values()):raise ValueError('invalid resource counts')
            revision=self.profile.get('revision','p6' if self.profile.get('build_id')=='0x56305036' else '')
            title.setText('FPGA 资源 · 发布 '+revision if revision else 'FPGA 资源 · 发布配置')
            self.title=title;self.bars=ResourceBars(resources);col.addWidget(self.bars)
            self.setToolTip('静态布局布线资源，不是运行时负载；未自动核对当前下载位流。\n位流：%s\nBUILD：%s\n报告：%s\n报告 SHA256：%s'%
                            (self.profile['bitstream'],self.profile['build_id'],self.profile['report'],self.profile['report_sha256']))
            caption=MetricLabel('构建静态值 · 非实时负载',16,15)
        except (OSError,ValueError,KeyError,TypeError):
            self.profile={};col.addWidget(QLabel('—'));caption=QLabel('资源报告不可用')
        caption.setProperty('role','trendHint');col.addWidget(caption)

    def select_build(self,build):
        files={'0x56305037':'fpga_resources.json','0x56305038':'fpga_resources_p8.json','0x56305039':'fpga_resources_p8r1.json','0x5630503a':'fpga_resources_p8r2.json','0x5630503b':'fpga_resources_p9.json'}
        if not hasattr(self,'bars') or build not in files or self.profile.get('build_id')==build:return
        try:
            profile=json.loads(Path(__file__).with_name(files[build]).read_text(encoding='utf-8'));resources=profile['resources']
            if profile['build_id']!=build or set(resources)!={'Slice','ERAM','DSP'} or any(not 0<=r['used']<=r['total'] or r['total']<=0 for r in resources.values()):return
            self.profile=profile;self.bars.resources=resources;self.bars.update();self.title.setText('FPGA 资源 · 发布 '+profile['revision'])
            self.setToolTip('按板端BUILD选择静态报告；不是实时负载，未核对下载位流SHA。\nBUILD：%s\n报告：%s\n报告 SHA256：%s'%(build,profile['report'],profile['report_sha256']))
        except (OSError,ValueError,KeyError,TypeError):return


class TelemetryPanel(QWidget):
    def __init__(self):
        super().__init__();self.setFixedHeight(170);layout=QHBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(10)
        specs=[('fps','预览帧率',' fps','A 蓝 / B 绿 · 近60秒','Qt实际绘制不同图像的次数；不是相机采集fps。'),
               ('latency','传输确认延迟',' ms','含PC解码与ACK回程','板端开始发送至上位机读取、解码并回传ACK。包含ACK回程；不含曝光、FPGA采集、HQ解码、编码与屏幕刷新，不是单向或完整端到端延迟。'),
               ('compression','FPGA 压缩率','%','FPGA→RK · 显示口径见提示','无损档以0%为显示基准，不代表实测数据量。其它档按实际源/PCIe字节计算，含帧封装开销；真实字节和数据节省率见提示。无有效采集帧时显示—。'),
               ('inference','推理处理耗时',' ms','关闭 / 结果过期时显示 —','板端model.run总耗时，含模型预处理、NPU推理和后处理；不含HQ解码。没有独立NPU内核计时。')]
        self.cards={}
        for key,title,unit,hint,description in specs:
            card=TrendCard(title,unit,hint,description);layout.addWidget(card,2);self.cards[key]=card
        self.resources=ResourceCard();self.resources.setMinimumWidth(220);layout.addWidget(self.resources,1)

    def update_state(self,state,preview_fps,paused):
        if state:self.resources.select_build(state.get('capture',{}).get('build_id'))
        values=measurements(state,preview_fps,paused)
        for key,pair in values.items():self.cards[key].set_values(pair)
        details=['无损档以0%为显示基准，不是实测值；其它档按实测显示。',
                 '实际FPGA→RK数据节省率 = (1 - PCIe帧总字节 / 原始数据字节) × 100%。',
                 '含帧封装开销；负值表示数据增大；缺失或无效采集数据不使用网络压缩值替代。']
        for c,value in zip(('A','B'),values['compression']):
            row=(state or {}).get('channels',{}).get(c,{})
            details.append(c+'：'+fpga_compression_detail(row))
        self.cards['compression'].setToolTip('\n'.join(details))
