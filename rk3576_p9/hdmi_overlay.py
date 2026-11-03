"""Adapter for the exact, accepted workstation projection implementation."""
import base64
from types import SimpleNamespace
import cv2
from station_preview_overlay import PreviewOverlay,CLASS_STYLES

LABELS=tuple(style[0] for style in CLASS_STYLES)
COLORS=tuple(tuple(int(style[1][i:i+2],16) for i in (1,3,5)) for style in CLASS_STYLES)


class HdmiOverlay:
    def __init__(self):self.key=None;self.core=PreviewOverlay();self.meta=None

    @staticmethod
    def gray(image):
        small=cv2.resize(image,(320,180),interpolation=cv2.INTER_LINEAR)
        return cv2.GaussianBlur(cv2.cvtColor(small,cv2.COLOR_BGR2GRAY),(3,3),.6)

    def project(self,image,reference,detections,key,exact=False,reference_png=None):
        if self.key!=key:
            if reference_png is None:
                ok,png=cv2.imencode('.png',self.gray(reference),[cv2.IMWRITE_PNG_COMPRESSION,1])
                if not ok:raise ValueError('HDMI tracking reference encoding failed')
                reference_png=base64.b64encode(png).decode('ascii')
            self.meta=dict(enabled=True,epoch=key[0],result_seq=key[1],age_ms=0,max_age_ms=800,
                width=reference.shape[1],height=reference.shape[0],detections=detections[:128],reference_png=reference_png)
            self.key=key
        pixels=cv2.cvtColor(cv2.resize(image,(320,180),interpolation=cv2.INTER_LINEAR),cv2.COLOR_BGR2RGB)
        boxes=self.core.project(SimpleNamespace(_preview_pixels=pixels),self.meta)
        return boxes,self.core.state
