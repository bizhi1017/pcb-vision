"""Trusted sensor/FPGA frame contracts, shared by control and CPU decoding."""
from dataclasses import dataclass,asdict

P8_BUILD=0x5630503A
RETIRED_P8_BUILD=0x56305038
RETIRED_P8R1_BUILD=0x56305039
P8_CAPABILITY=0x384B434D
PROFILE_REGISTER=0x20
SENSOR_MASK_REGISTER=0x24
CAPABILITY_REGISTER=0x28
GENERATION_REGISTER=0x2c

@dataclass(frozen=True)
class CaptureProfile:
    id:str
    mode:int
    width:int
    height:int
    fps:int
    raw_width:int
    raw_height:int
    crop_x:int
    crop_y:int
    fingerprint:int
    @property
    def line_bytes(self):return self.raw_width//4*5
    @property
    def stride(self):return (self.line_bytes+15)//16*16
    @property
    def raw_bytes(self):return self.stride*self.raw_height
    @property
    def gray_bytes(self):return self.width*self.height
    @property
    def packet_capacity(self):return self.raw_bytes+((self.raw_bytes+1023)//1024)*16+80
    @property
    def shape(self):return (self.height,self.width,3)
    def status(self):
        return dict(asdict(self),stride=self.stride,line_bytes=self.line_bytes,raw_bytes=self.raw_bytes,packet_capacity=self.packet_capacity)

PROFILES={
    '1080p60':CaptureProfile('1080p60',0,1920,1080,60,1944,1097,4,16,0x2D59500E),
    # IMX415 all-pixel RAW: binning's extra 12 dummy columns are absent.
    # Sony timing chart p56: 3864 columns, 2192 payload rows, crop (12,20).
    '4k30':CaptureProfile('4k30',1,3840,2160,30,3864,2192,12,20,0x384B3032),
}
DEFAULT=PROFILES['1080p60']
MAX_FRAME_BYTES=max(p.raw_bytes for p in PROFILES.values())
MAX_PACKET=max(p.packet_capacity for p in PROFILES.values())

def profile(value):
    if isinstance(value,CaptureProfile):
        if PROFILES.get(value.id)!=value:raise ValueError('untrusted capture geometry')
        return value
    try:return PROFILES[value]
    except (KeyError,TypeError):raise ValueError('capture profile must be 1080p60 or 4k30') from None

def selection(value):
    if not isinstance(value,dict) or set(value)!={'profile','channels'}:raise ValueError('capture requires profile and channels')
    p=profile(value['profile']);channels=value['channels']
    if not isinstance(channels,list) or not channels or any(c not in ('A','B') for c in channels) or len(set(channels))!=len(channels):raise ValueError('invalid active capture channels')
    if p.mode and len(channels)!=1:raise ValueError('4K capture requires exactly one active camera')
    return dict(profile=p.id,channels=sorted(channels))
