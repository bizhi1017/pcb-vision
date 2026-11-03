"""Bound allocator retention and record memory pressure; CPU/procfs only.

Does not stop capture, change codecs or create hardware contexts. Diagnostic
records contain counters and allocation sites, never images or credentials.
"""
import ctypes as C
import gc
import json
import os
from pathlib import Path
import time
import tracemalloc


class MallInfo(C.Structure):
    _fields_=[(name,C.c_size_t) for name in ('arena','ordblks','smblks','hblks','hblkhd','usmblks','fsmblks','uordblks','fordblks','keepcost')]


class Allocator:
    def __init__(self):
        self.lib=None
        try:
            lib=C.CDLL(None)
            lib.malloc_trim.argtypes=[C.c_size_t];lib.malloc_trim.restype=C.c_int
            lib.mallinfo2.argtypes=[];lib.mallinfo2.restype=MallInfo
            self.lib=lib
        except (OSError,AttributeError):pass

    def info(self):
        if not self.lib:return {}
        row=self.lib.mallinfo2()
        return {k:int(getattr(row,k)) for k in ('arena','hblkhd','uordblks','fordblks','keepcost')}

    def trim(self):
        if not self.lib:return False
        gc.collect()
        return bool(self.lib.malloc_trim(0))


def memory_info():
    result={}
    for filename,names in (('/proc/self/status',('VmRSS','RssAnon','RssFile','RssShmem')),
                           ('/proc/meminfo',('MemAvailable','MemTotal'))):
        try:
            for row in Path(filename).read_text().splitlines():
                key,_,value=row.partition(':')
                if key in names:result[key+'_kb']=int(value.split()[0])
        except (OSError,ValueError,IndexError):pass
    return result


def enable_trace():
    if os.environ.get('PCB_MEMORY_TRACE')=='1' and not tracemalloc.is_tracing():tracemalloc.start(4)


class MemoryMonitor:
    def __init__(self,hub,directory=None,allocator=None):
        self.hub=hub;self.directory=Path(directory or Path(__file__).with_name('diagnostics'))
        self.allocator=allocator or Allocator();self.slot=0;self.last_trim=-1e9;self.last_record=-1e9;self.last_rss=0

    def sample(self,now=None):
        now=time.monotonic() if now is None else now
        state=memory_info();rss=state.get('VmRSS_kb',0)
        state['allocator_bytes']=self.allocator.info()
        if rss>=512*1024 and now-self.last_trim>=10:
            self.last_trim=now;before=rss;self.allocator.trim();state.update(memory_info())
            state['trim_released_kb']=max(0,before-state.get('VmRSS_kb',before))
        if tracemalloc.is_tracing():state['python_traced_bytes'],state['python_peak_bytes']=tracemalloc.get_traced_memory()
        with self.hub.lock:
            self.hub.memory=dict(state)
            config=dict(detect=dict(self.hub.config['detect']),hdmi=self.hub.config['hdmi'],fpga_codec=dict(self.hub.config['fpga_codec']))
        if rss>=350*1024 and rss-self.last_rss>=128*1024 and now-self.last_record>=10:
            self.last_record=now;self.last_rss=rss
            top=[str(x) for x in tracemalloc.take_snapshot().statistics('lineno')[:20]] if tracemalloc.is_tracing() else []
            record=dict(pid=os.getpid(),monotonic=now,memory=state,config=config,top_python_allocations=top)
            try:
                self.directory.mkdir(parents=True,exist_ok=True)
                target=self.directory/('memory_%d.json'%self.slot);temporary=target.with_suffix('.tmp')
                temporary.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temporary,target)
                self.slot=(self.slot+1)%4
            except OSError:pass
            self.hub.events.emit('预览内存 %.0f MB；已保存内存诊断，请保持当前设置核查。'%(state.get('VmRSS_kb',rss)/1024),'WARN')
        return state

    def run(self):
        while not self.hub.stop.is_set():
            try:self.sample()
            except Exception as exc:self.hub.events.emit('内存统计暂不可用：'+type(exc).__name__,'WARN')
            self.hub.stop.wait(2)
