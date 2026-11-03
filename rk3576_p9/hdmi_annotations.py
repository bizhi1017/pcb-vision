"""Cached OpenCV rectangle/text tiles, premultiplied RGBA for GPU compositing."""
import cv2
import numpy as np
from hdmi_overlay import COLORS
from hdmi_station_text import station_label


class Annotations(list):
    def __init__(self,boxes=(),atlas=None):super().__init__(boxes);self.atlas=atlas


class OpenCvAtlas:
    def __init__(self):self.key=None;self.version=0;self.pixels=None;self.entries=[]

    @staticmethod
    def border(width,height,region,color):
        x,y,w,h=region;large=np.zeros((h*2,w*2),np.uint8)
        cv2.rectangle(large,((3-x)*2,(3-y)*2),((3+width-x)*2,(3+height-y)*2),255,5,cv2.LINE_AA)
        mask=cv2.resize(large,(w,h),interpolation=cv2.INTER_AREA)
        rgba=np.empty((h,w,4),np.uint8)
        rgba[:,:,:3]=((mask[:,:,None].astype(np.uint16)*np.asarray(color,np.uint16)+127)//255).astype(np.uint8)
        rgba[:,:,3]=mask;return rgba

    @staticmethod
    def label(text,color):
        return station_label(text,tuple(color))

    def prepare(self,boxes,viewport):
        left,top,w,h=viewport;specs=[];positions=[]
        for item in boxes[:128]:
            x0,y0,x1,y1=item['box'];bw=max(1,round((x1-x0)*w));bh=max(1,round((y1-y0)*h))
            specs.append((bw,bh,item['class_id'],item['label'][:40]));positions.append((left+round(x0*w),top+round(y0*h)))
        key=tuple(specs)
        if key!=self.key:
            tiles=[];entries=[]
            for index,(bw,bh,cid,label) in enumerate(specs):
                color=COLORS[cid];fw,fh=bw+7,bh+7;upper=min(7,fh);lower=min(7,fh-upper);middle=fh-upper-lower
                regions=[(0,0,fw,upper)]
                if lower:regions.append((0,fh-lower,fw,lower))
                if middle:
                    lw=min(7,fw);rw=min(7,fw-lw);regions.append((0,upper,lw,middle))
                    if rw:regions.append((fw-rw,upper,rw,middle))
                for region in regions:
                    rx,ry,_,_=region;entries.append((index,'border',rx-3,ry-3));tiles.append(self.border(bw,bh,region,color))
                entries.append((index,'label',0,0));tiles.append(self.label(label,color))
            atlas_width=max([2048]+[tile.shape[1] for tile in tiles]);placements={};x=y=row=0
            for index in sorted(range(len(tiles)),key=lambda i:tiles[i].shape[0],reverse=True):
                th,tw=tiles[index].shape[:2]
                if x+tw>atlas_width:y+=row;x=0;row=0
                placements[index]=(x,y,tw,th);x+=tw;row=max(row,th)
            atlas_height=y+row
            if atlas_width>4096 or atlas_height>4096 or atlas_width*atlas_height*4>32*1024*1024:raise RuntimeError('OpenCV HDMI overlay atlas exceeds bounded capacity')
            pixels=np.zeros((atlas_height,atlas_width,4),np.uint8) if tiles else None
            for index,tile in enumerate(tiles):
                ax,ay,tw,th=placements[index];pixels[ay:ay+th,ax:ax+tw]=tile
            if pixels is not None:pixels.setflags(write=False)
            self.pixels=pixels;self.entries=[(*entry,placements[i]) for i,entry in enumerate(entries)]
            self.key=key;self.version+=1
        quads=[]
        if self.pixels is not None:
            ah,aw=self.pixels.shape[:2]
            for index,kind,dx,dy,(ax,ay,tw,th) in self.entries:
                x,y=positions[index]
                if kind=='label':x=max(left,min(x,left+w-tw));y=max(top,y-th-2)
                else:x+=dx;y+=dy
                quads.append((x,y,tw,th,ax/aw,ay/ah,(ax+tw)/aw,(ay+th)/ah))
        return Annotations(boxes,dict(token=self.version,pixels=self.pixels,quads=quads))


def composite_annotations(canvas,annotations,viewport,cache):
    """The software fallback uses the same Chinese label/border tiles."""
    if not annotations:return
    atlas=getattr(annotations,'atlas',None)
    if atlas is None:atlas=cache.prepare(annotations,viewport).atlas
    pixels=atlas['pixels']
    if pixels is None:return
    left,top,vw,vh=viewport;ah,aw=pixels.shape[:2]
    for x,y,w,h,u0,v0,u1,v1 in atlas['quads']:
        x,y=int(x),int(y);x0,y0=max(left,x),max(top,y);x1,y1=min(left+vw,x+w),min(top+vh,y+h)
        if x1<=x0 or y1<=y0:continue
        ax,ay=round(u0*aw),round(v0*ah)
        tile=pixels[ay+y0-y:ay+y1-y,ax+x0-x:ax+x1-x].astype(np.uint16)
        original=canvas[y0:y1,x0:x1].astype(np.uint16)
        canvas[y0:y1,x0:x1]=np.minimum(255,tile[:,:,:3][:,:,::-1]+(original*(255-tile[:,:,3:4])+127)//255).astype(np.uint8)
