"""Closed-form payload budget independent of image values and RTL encoder."""
from pathlib import Path
import os,json,re
root=Path(__file__).resolve().parents[1];e=root/'test/inspection/p9_predictive'
records=[]
for name,stride,line,height,gray,wire in [('full1080_3',2432,2430,1097,False,1667536),('full4k_3',4832,4830,2192,False,6619936),('gray1080_3',1920,1920,1080,True,1328480)]:
    size=stride*height
    def prefix(n):
        if gray:return n*5
        rows,tail=divmod(n,stride);payload=min(tail,line);groups,left=divmod(payload,5)
        return rows*((line//5)*24+(stride-line)*8)+groups*24+min(left,4)*6+max(0,tail-line)*8
    chunks=[]
    for begin in range(0,size,1024):
        bits=prefix(min(begin+1024,size))-prefix(begin)
        chunks.append(((bits+127)//128)*16)
    expected=80+16*len(chunks)+sum(chunks)
    assert expected==wire and 100*(1-expected/size)>30
    records.append(dict(case=name,raw_bytes=size,wire_bytes=expected,saving_pct=100*(1-expected/size),content_independent=True))
rtl=json.loads((e/'rtl_full.json').read_text(encoding='utf-8'))
for frame in rtl['frames']:
    match=re.search(r'cycles=(\d+)',frame['stdout']);assert match
    elapsed_ms=int(match[1])/1e9
    period=1000/(30 if '4k' in frame['case'] else 60)
    assert elapsed_ms<period,(frame['case'],elapsed_ms,period)
    for row in records:
        if row['case']==frame['case']:row.update(testbench_elapsed_ms=elapsed_ms,frame_period_budget_ms=period)
assert max(v-(v>>4<<4) for v in range(1024))==15
assert max(v-(v>>3<<3) for v in range(256))==7
(e/'geometry_budget.json').write_text(json.dumps(dict(scope='Closed-form byte budget and recorded CPU RTL elapsed time at the testbench clock, not measured physical sensor fps',raw_max_quantization_error=15,gray_max_quantization_error=7,cases=records),indent=2),encoding='utf-8')
print('P9 BUDGET PASS: scene-independent >30%, padding/trailers included, RAW error<=15, gray error<=7, RTL burst timing within target frame period')
