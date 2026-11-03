"""CPU C/Python cross-checks. Run under Linux/WSL, never opens hardware."""
from pathlib import Path
import sys,ctypes as C,json,hashlib,struct,binascii,time,statistics
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'rk3576_p9'))
import fpga_codec as codec
e=root/'test/inspection/p9_predictive'
lib=C.CDLL(str(e/'libp9_codec_x86.so'))
lib.fpga_codec_protocol.restype=C.c_int
assert lib.fpga_codec_protocol()==2
for name in ('fpga_decode','fpga_decode_profile'):
    f=getattr(lib,name);f.argtypes=[C.c_void_p,C.c_size_t,C.c_uint,C.c_void_p,C.c_size_t]+([C.c_uint,C.c_uint] if name.endswith('_profile') else []);f.restype=C.c_int
codec._native=lib
records=[]
for profile,pre in [('1080p60',0),('1080p60',1),('4k30',0),('4k30',1)]:
    g=codec.capture_profile(profile);size=g.gray_bytes if pre else g.raw_bytes
    # Full-range byte pattern exercises every quantization value and row pad.
    raw=(bytes(range(256))*((size+255)//256))[:size]
    packet=codec.encode_compact_reference(raw,1 if pre else 0,pre,0x12345678,143,g.stride,g.line_bytes)
    expected=codec.quantize(raw,3,pre,g.stride,g.line_bytes)
    python,meta=codec.decode_python(packet,1 if pre else 0,(g.raw_bytes,g.gray_bytes))
    assert python[:-16]==expected
    times=[]
    for _ in range(3):
        start=time.perf_counter();native,nmeta=codec.decode(packet,1 if pre else 0,profile=profile);times.append((time.perf_counter()-start)*1000)
        assert native==python and nmeta['mode']=='lossy6'
    buffer=bytearray(size+16);view,_=codec.decode(packet,1 if pre else 0,output=buffer,profile=profile)
    assert bytes(view)==python
    checks=0
    bad_packets=[packet[:n] for n in (0,16,64,len(packet)-1,len(packet)-16)]
    for pos in (0,7,28,36,40,45,len(packet)//2,len(packet)-32,len(packet)-20,len(packet)-4):
        bad=bytearray(packet);bad[pos]^=1;bad_packets.append(bytes(bad))
    for bad in bad_packets:
        for f in (lambda b:codec.decode_python(b,1 if pre else 0,(g.raw_bytes,g.gray_bytes)),lambda b:codec.decode(b,1 if pre else 0,profile=profile)):
            try:f(bad)
            except ValueError:checks+=1
            else:raise AssertionError('Corrupt frame accepted')
    for f in (lambda:codec.decode(packet,0 if pre else 1,profile=profile),lambda:codec.decode(packet,1 if pre else 0,output=bytearray(16),profile=profile)):
        try:f()
        except ValueError:checks+=1
        else:raise AssertionError('Wrong camera/small buffer accepted')
    saving=100*(1-len(packet)/size);assert saving>30
    records.append(dict(profile=profile,pre=pre,raw_bytes=size,wire_bytes=len(packet),saving_pct=saving,
                        native_median_ms=statistics.median(times),rejections=checks,pixel_bytes_equal=True,packet_sha256=hashlib.sha256(packet).hexdigest()))
    print(profile,pre,'PASS',round(saving,3),'%',flush=True)
(e/'native_x86.json').write_text(json.dumps(dict(scope='WSL x86 CPU only, synthetic exhaustive byte-pattern frames; not measured RK3576 throughput',library_sha256=hashlib.sha256((e/'libp9_codec_x86.so').read_bytes()).hexdigest(),source_sha256=hashlib.sha256((root/'rk3576_p9/fpga_codec.c').read_bytes()).hexdigest(),cases=records),indent=2),encoding='utf-8')
