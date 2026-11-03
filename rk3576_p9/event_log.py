"""Bounded, ordered application events for the Qt Ethernet console."""
from collections import deque
import threading
import time
import uuid


class EventLog:
    def __init__(self,capacity=1000):
        self.lock=threading.Lock();self.entries=deque(maxlen=capacity)
        self.sequence=0;self.session=uuid.uuid4().hex

    def emit(self,message,level='INFO',camera=None):
        if level not in ('INFO','WARN','ERROR'):raise ValueError('invalid log level')
        if camera not in (None,'A','B'):raise ValueError('invalid log camera')
        with self.lock:
            self.sequence+=1
            self.entries.append(dict(id=self.sequence,at=time.monotonic(),level=level,camera=camera,message=str(message)[:2000]))

    def read(self,after=0,session=None,limit=200):
        if type(after)is not int or after<0 or type(limit)is not int or not 1<=limit<=200:raise ValueError('invalid log cursor')
        with self.lock:
            reset=(session is not None and session!=self.session) or after>self.sequence
            if reset:after=0
            oldest=self.entries[0]['id'] if self.entries else self.sequence+1
            dropped=max(0,oldest-after-1)
            items=[dict(e) for e in self.entries if e['id']>after][:limit]
            return dict(session=self.session,reset=reset,events=items,cursor=items[-1]['id'] if items else after,
                        dropped=dropped,server_uptime=time.monotonic())
