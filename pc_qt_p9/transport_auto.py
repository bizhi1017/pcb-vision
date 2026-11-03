"""Coalesce user edits and serialize requests per channel and setting family."""
from PyQt5.QtCore import QObject,QTimer


class AutoTransport(QObject):
    def __init__(self,window):
        super().__init__(window);self.window=window;self.pending={};self.busy=set();self.timers={}

    def editing(self,kind,c):return (kind,c) in self.pending or (kind,c) in self.busy

    def queue(self,kind,c):
        w=self.window
        if w.closing or w.config is None:return
        if kind=='fpga_codec' and not w.codec_controls[c]['mode'].isEnabled():return
        if kind=='transport' and not w.net_controls[c]['mode'].isEnabled():return
        if kind=='fpga_codec':value=w.codec_controls[c]['mode'].currentData()
        else:
            f=w.net_controls[c]
            value=dict(mode=f['mode'].currentData(),mbps=f['mbps'].value(),fps=f['fps'].value(),max_width=f['max_width'].currentData(),adaptive=f['adaptive'].isChecked())
        key=(kind,c);self.pending[key]=value
        if key not in self.timers:
            timer=QTimer(self);timer.setSingleShot(True);timer.timeout.connect(lambda key=key:self.flush(key));self.timers[key]=timer
        self.timers[key].start(150)

    def flush(self,key):
        w=self.window
        if w.closing or key in self.busy or key not in self.pending:return
        kind,c=key;value=self.pending.pop(key)
        if w.config.get(kind,{}).get(c)==value:return
        self.busy.add(key)
        def done(state):
            self.busy.discard(key);w.state(state)
            if key in self.pending:self.flush(key)
        def failed(error):
            self.busy.discard(key)
            if kind=='transport':w.net_seen.pop(c,None)
            else:w.codec_controls[c].pop('seen',None)
            w.failed(error)
            if key in self.pending:self.flush(key)
        w.command('/api/v2/config',{kind:{c:value}},done,failed)

    def close(self):
        self.pending.clear()
        for timer in self.timers.values():timer.stop()
