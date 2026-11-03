"""P9 full-frame codec. Importing never opens hardware.

Dictionary matches copy one aligned 32-bit word from an earlier word, distance
1..65535 words. Chunk raw fallback bounds expansion; CRC covers reconstructed
bytes, including RAW10 row padding. Legacy modes are decoded unchanged.
Protocol v2 mode 3 is explicit RAW6/GRAY5 packing, NOT lossless/JPEG/LZ77.
Native decoder is optional for CPU fixtures.
"""
import binascii
import ctypes as C
import struct
import threading
from pathlib import Path
from capture_profiles import profile as capture_profile,DEFAULT as CAPTURE_DEFAULT,MAX_PACKET as PROFILE_MAX_PACKET

BUILD = 0x56305037
MAGIC = 0x31433756
CHUNK = 0x4B4E4843
END = 0x31453756
RAW_BYTES = 2432 * 1097
GRAY_BYTES = 1920 * 1080
MAX_PACKET = PROFILE_MAX_PACKET
MODES = ('raw', 'lossless', 'lossy9', 'lossy6')
LEGACY_MODES = ('raw', 'lossless', 'lossy9', 'lossy8')

def compact_payload(data, begin, pre, stride, line_bytes):
    """Independent bit-exact P9 reference, including arbitrary chunk/row splits."""
    bucket=0;bits=0;out=bytearray()
    for i,b in enumerate(data,begin):
        offset=i%stride
        if pre:n=5;value=b>>3
        elif offset>=line_bytes:n=8;value=b
        elif offset%5==4:n=0;value=0
        else:n=6;value=b>>2
        bucket|=value<<bits;bits+=n
        while bits>=8:out.append(bucket&255);bucket>>=8;bits-=8
    if bits:out.append(bucket)
    return bytes(out)

def expand_compact(payload, begin, count, pre, stride, line_bytes):
    out=bytearray();bucket=0;bits=0;pos=0
    for i in range(begin,begin+count):
        offset=i%stride
        if pre:n=5;shift=3
        elif offset>=line_bytes:n=8;shift=0
        elif offset%5==4:n=0;shift=0
        else:n=6;shift=2
        while bits<n:
            if pos>=len(payload):raise ValueError('compact token overrun')
            bucket|=payload[pos]<<bits;bits+=8;pos+=1
        out.append((bucket&((1<<n)-1))<<shift);bucket>>=n;bits-=n
    if pos!=len(payload) or bucket:raise ValueError('compact unused bytes/bits')
    return out

def encode_compact_reference(data,camera=0,pre=0,seq=1,epoch=0,stride=2432,line_bytes=2430):
    if not data or len(data)%16:raise ValueError('aligned nonempty frame required')
    data=quantize(data,3,pre,stride,line_bytes)
    header=struct.pack('<7I',MAGIC,0x02000000|(camera<<16)|(pre<<8)|3,len(data),seq,epoch,1024,0)
    result=bytearray(header+struct.pack('<I',binascii.crc32(header)))
    for index,begin in enumerate(range(0,len(data),1024)):
        raw=data[begin:begin+1024];payload=compact_payload(raw,begin,pre,stride,line_bytes)
        result.extend(struct.pack('<4I',CHUNK,len(raw),len(payload)|0xc0000000,index))
        result.extend(payload);result.extend(bytes((-len(payload))%16))
    checks=wire_crcs(result)
    result.extend(struct.pack('<4I',END,binascii.crc32(data),len(data),epoch))
    result.extend(struct.pack('<4I',*checks));body=len(result)
    result.extend(struct.pack('<4I',0x54373056,body,0x01000000|(camera<<16)|(4<<11)|(pre<<8)|(seq&255),body^0xffffffff))
    return bytes(result)

def wire_crcs(data):
    """Four CRC-32 lanes over ordered 32-bit words of each 16-byte beat."""
    return tuple(binascii.crc32(b''.join(data[i:i+4] for i in range(lane*4,len(data),16))) for lane in range(4))


def quantize(data, mode, pre=0, stride=2432, line_bytes=2430):
    out = bytearray(data)
    if mode >= 2:
        mask = 0xAA if mode == 2 else 0
        if pre:
            clear = 0xFE if mode == 2 else 0xF8
            for i in range(len(out)): out[i] &= clear
        else:
            for row in range(0,len(out),stride):
                for i in range(row,min(row+line_bytes,len(out))):
                    if (i-row)%5==4:out[i]&=mask
                    elif mode==3:out[i]&=0xfc
    return bytes(out)


def encode_reference(data, camera=0, mode=1, pre=0, seq=1, epoch=0, dictionary_bits=11):
    """CPU golden encoder, independent dictionary search; used only by fixtures."""
    if mode==3:return encode_compact_reference(data,camera,pre,seq,epoch)
    if not data or len(data)%16:raise ValueError('aligned nonempty frame required')
    data=quantize(data,mode,pre);words=struct.unpack('<%dI'%(len(data)//4),data)
    h=[MAGIC,0x01000000|(camera<<16)|(pre<<8)|mode,len(data),seq,epoch,1024,0]
    header=struct.pack('<7I',*h);result=bytearray(header+struct.pack('<I',binascii.crc32(header)))
    table={};chunk_index=0
    for begin in range(0,len(words),256):
        flags=0;tokens=bytearray();matches=0;end=min(begin+256,len(words))
        for i in range(begin,end):
            word=words[i];key=((word>>0)^(word>>11)^(word>>22))&((1<<dictionary_bits)-1)
            old=table.get(key);table[key]=(word,i)
            match=mode!=0 and old is not None and old[0]==word and 0<i-old[1]<=65535
            if match:
                flags |= 1<<(i-begin);matches+=1;tokens.extend(struct.pack('<H',i-old[1]))
            else:tokens.extend(struct.pack('<I',word))
        compressed=matches*2>32
        payload=flags.to_bytes(32,'little')+tokens if compressed else data[begin*4:end*4]
        result.extend(struct.pack('<4I',CHUNK,(end-begin)*4,len(payload)|(compressed<<31),chunk_index))
        result.extend(payload);result.extend(bytes((-len(payload))%16));chunk_index+=1
    checks=wire_crcs(result)
    result.extend(struct.pack('<4I',END,binascii.crc32(data),len(data),epoch))
    result.extend(struct.pack('<4I',*checks))
    status=(4<<11)|(pre<<8)|(seq&255)
    result.extend(struct.pack('<4I',0x54373056,len(result),0x01000000|(camera<<16)|status,len(result)^0xffffffff))
    return bytes(result)


def decode_python(packet,camera,expected_sizes=(RAW_BYTES,GRAY_BYTES)):
    if camera not in (0,1):raise ValueError('invalid camera')
    if len(packet)<96 or len(packet)>MAX_PACKET or len(packet)%16:raise ValueError('codec packet length')
    magic,info,size,seq,epoch,chunk_bytes,reserved,hcrc=struct.unpack_from('<8I',packet)
    mode=info&255;pre=(info>>8)&255;version=info>>24
    if magic!=MAGIC or version not in (1,2) or (version==2 and mode!=3) or (info>>16)&255!=camera or mode>3 or pre>4 or reserved or chunk_bytes!=1024:raise ValueError('codec header')
    if size not in expected_sizes or size%16 or binascii.crc32(packet[:28])!=hcrc:raise ValueError('codec header CRC/size')
    if expected_sizes==(RAW_BYTES,GRAY_BYTES) and size!=(GRAY_BYTES if pre else RAW_BYTES):raise ValueError('codec geometry')
    tail,body,tag,inverse=struct.unpack_from('<4I',packet,len(packet)-16)
    if tail!=0x54373056 or body!=len(packet)-16 or inverse!=(body^0xffffffff) or tag>>24!=1 or (tag>>16)&255!=camera or tag&0xc000 or (tag>>11)&7!=4:raise ValueError('codec DMA trailer')
    if tag&0x7ff != (pre<<8)|(seq&255):raise ValueError('codec DMA frame tag')
    geometry=capture_profile('4k30' if size==capture_profile('4k30').raw_bytes else '1080p60')
    out=bytearray();at=32;index=0
    while len(out)<size:
        if at+16>len(packet)-48:raise ValueError('truncated chunk')
        cm,raw,stored,ci=struct.unpack_from('<4I',packet,at);at+=16
        compact=version==2
        if compact:
            if stored>>30!=3:raise ValueError('compact chunk flag')
            stored&=0x3fffffff;compressed=False
        else:compressed=stored>>31;stored&=0x7fffffff
        if cm!=CHUNK or ci!=index or raw!=min(1024,size-len(out)) or stored>raw+(32 if compressed else 0) or (not compact and not compressed and stored!=raw):raise ValueError('codec chunk geometry')
        end=at+stored
        if end>len(packet)-48:raise ValueError('chunk bounds')
        if compact:out.extend(expand_compact(packet[at:end],len(out),raw,pre,geometry.stride,geometry.line_bytes))
        elif compressed:
            if stored<32:raise ValueError('chunk flags')
            flags=int.from_bytes(packet[at:at+32],'little');pos=at+32
            if flags>>(raw//4):raise ValueError('excess flags')
            for i in range(raw//4):
                n=2 if flags>>i&1 else 4
                if pos+n>end:raise ValueError('token overrun')
                if n==2:
                    distance=struct.unpack_from('<H',packet,pos)[0]*4
                    if distance==0 or distance>len(out):raise ValueError('invalid backreference')
                    out.extend(out[-distance:len(out)-distance+4] if distance!=4 else out[-4:])
                else:out.extend(packet[pos:pos+4])
                pos+=n
            if pos!=end:raise ValueError('unused token bytes')
        else:out.extend(packet[at:end])
        padded=at+((stored+15)//16)*16
        if any(packet[end:padded]):raise ValueError('nonzero chunk padding')
        at=padded;index+=1
    if at+48!=len(packet):raise ValueError('extra packet bytes')
    em,crc,length,fe=struct.unpack_from('<4I',packet,at)
    if em!=END or length!=size or fe!=epoch or binascii.crc32(out)!=crc:raise ValueError('frame CRC/footer')
    if struct.unpack_from('<4I',packet,at+16)!=wire_crcs(packet[:at]):raise ValueError('encoded stream CRC')
    legacy=struct.pack('<4I',tail,size,tag,size^0xffffffff)
    meta=dict(mode=(MODES if version==2 else LEGACY_MODES)[mode],epoch=epoch,seq=seq,raw_bytes=size,wire_bytes=len(packet),pre=pre,crc32=crc)
    if version==2:meta.update(algorithm='RAW6/GRAY5 bitpack',precision_bits=5 if pre else 6)
    return bytes(out)+legacy,meta


_native=None
_buffers=threading.local()
def native_available():
    global _native
    if _native is None:
        try:
            lib=C.CDLL(str(Path(__file__).with_name('libfpga_codec.so')))
            if not hasattr(lib,'fpga_codec_protocol'):raise OSError('P9 decoder ABI missing')
            lib.fpga_codec_protocol.restype=C.c_int
            if lib.fpga_codec_protocol()!=2:raise OSError('P9 decoder protocol mismatch')
            lib.fpga_decode.argtypes=[C.c_void_p,C.c_size_t,C.c_uint,C.c_void_p,C.c_size_t]
            lib.fpga_decode.restype=C.c_int
            if hasattr(lib,'fpga_decode_profile'):
                lib.fpga_decode_profile.argtypes=lib.fpga_decode.argtypes+[C.c_uint,C.c_uint];lib.fpga_decode_profile.restype=C.c_int
            _native=lib
        except OSError:_native=False
    return bool(_native)

def decode(packet,camera,output=None,profile=None):
    geometry=capture_profile(profile or CAPTURE_DEFAULT)
    target=None
    if output is not None:
        target=memoryview(output)
        if not target.c_contiguous or target.readonly or not target.nbytes:raise ValueError('codec output must be writable and contiguous')
        target=target.cast('B')
    if not native_available():
        decoded,meta=decode_python(packet,camera,(geometry.raw_bytes,geometry.gray_bytes))
        if meta['raw_bytes']!=(geometry.gray_bytes if meta['pre'] else geometry.raw_bytes):raise ValueError('codec profile geometry')
        if target is None:return decoded,meta
        if len(target)<len(decoded):raise ValueError('codec output too small')
        target[:len(decoded)]=decoded
        return target[:len(decoded)].toreadonly(),meta
    output_keepalive=None
    if target is None:
        if not hasattr(_buffers,'out') or len(_buffers.out)<geometry.raw_bytes+16:_buffers.out=C.create_string_buffer(geometry.raw_bytes+16)
        out=_buffers.out;capacity=len(out)
    else:
        output_keepalive=C.c_char.from_buffer(target)
        out=C.addressof(output_keepalive);capacity=len(target)
    keepalive=None;source=packet
    if not isinstance(packet,bytes):
        view=memoryview(packet)
        if not view.c_contiguous:raise ValueError('codec input must be contiguous')
        if view.readonly:source=view.tobytes()
        elif view.nbytes:
            keepalive=C.c_char.from_buffer(view);source=C.addressof(keepalive)
        else:source=b''
    if geometry.mode:
        if not hasattr(_native,'fpga_decode_profile'):raise ValueError('4K requires the updated FPGA decoder library')
        size=_native.fpga_decode_profile(source,len(packet),camera,out,capacity,geometry.raw_bytes,geometry.gray_bytes)
    else:size=_native.fpga_decode(source,len(packet),camera,out,capacity)
    if size<0:raise ValueError('FPGA compressed frame invalid (%d)'%size)
    _,info,raw,seq,epoch=struct.unpack_from('<5I',packet)
    decoded=C.string_at(out,size) if target is None else target[:size].toreadonly()
    meta=dict(mode=(MODES if info>>24==2 else LEGACY_MODES)[info&255],epoch=epoch,seq=seq,raw_bytes=raw,wire_bytes=len(packet),pre=(info>>8)&255)
    if info>>24==2:meta.update(algorithm='RAW6/GRAY5 bitpack',precision_bits=5 if meta['pre'] else 6)
    return decoded,meta
