"""Two ordered decode slots; socket receive can overlap native JPEG decoding."""
from collections import deque
import threading


class PreviewPipeline:
    """Bound includes decoding, completed and delivery work, never just a queue.

    Native decode may finish out of order. One drainer delivers in receive order
    so stateful tracking and acknowledgements always refer to the correct frame.
    """
    def __init__(self,decode,deliver,cancelled=None,initialize_thread=None,limit=2):
        if limit not in (1,2):raise ValueError('preview decode limit must be one or two')
        self.decode=decode;self.deliver=deliver;self.cancelled=cancelled or (lambda:False)
        self.initialize_thread=initialize_thread;self.limit=limit
        self.condition=threading.Condition();self.jobs=deque();self.order=deque()
        self.pending=0;self.peak=0;self.closed=False;self.error=None;self.draining=False
        self.workers=[threading.Thread(target=self.run,name='preview-decode-%d'%i,daemon=True) for i in range(limit)]
        for worker in self.workers:worker.start()

    def _check(self):
        if self.error is not None:raise self.error
        if self.closed or self.cancelled():raise ConnectionAbortedError('preview decode stopped')

    def check(self):
        with self.condition:self._check()

    def submit(self,job):
        with self.condition:
            self._check()
            while self.pending>=self.limit:
                self.condition.wait(.02);self._check()
            row=dict(job=job,done=False)
            self.jobs.append(row);self.order.append(row);self.pending+=1
            self.peak=max(self.peak,self.pending);self.condition.notify_all()

    def _fail(self,error):
        with self.condition:
            if self.error is None:self.error=error
            self.closed=True;self.jobs.clear();self.order.clear();self.condition.notify_all()

    def run(self):
        try:
            if self.initialize_thread is not None:self.initialize_thread()
            while True:
                with self.condition:
                    while not self.jobs and not self.closed:
                        if self.cancelled():return
                        self.condition.wait(.05)
                    if self.closed or self.cancelled():return
                    row=self.jobs.popleft()
                result=self.decode(row['job'])
                with self.condition:
                    if self.closed or self.cancelled():return
                    row.update(done=True,result=result)
                    if self.draining:continue
                    self.draining=True
                self._drain()
        except Exception as error:self._fail(error)

    def _drain(self):
        while True:
            with self.condition:
                if self.closed or self.cancelled() or not self.order or not self.order[0]['done']:
                    self.draining=False;return
                row=self.order.popleft()
            # No state lock around tracking/ACK: cancellation remains responsive
            # even while an ACK worker applies its own bounded backpressure.
            self.deliver(row['job'],row['result'])
            with self.condition:
                self.pending-=1;self.condition.notify_all()

    def finish(self):
        """Used at clean EOF; delivery errors/cancellation still propagate."""
        with self.condition:
            while self.pending:
                self._check();self.condition.wait(.02)
            self._check()

    def close(self):
        with self.condition:
            self.closed=True;self.jobs.clear();self.order.clear();self.condition.notify_all()
        for worker in self.workers:worker.join()
