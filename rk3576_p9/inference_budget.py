"""Bounded, shared inference cadence that reserves headroom for live video."""
import math
from collections import deque

class InferenceBudget:
    def __init__(self,enabled=True):
        self.enabled=enabled;self.fps=12.;self.active=False;self.video_fps=None;self.reason='warmup'
        self.active_since=None;self.low_since=None;self.high_since=None
        self.last_observe=-math.inf;self.last_adjust=-math.inf;self.last_start=None
        self.probe=None;self.retry_after=-math.inf
        self.history=deque();self.recovery_fps=None;self.nominal=60.

    def observe(self,now,active,speeds,nominal=60.):
        if nominal not in (30,60):raise ValueError('unsupported source cadence')
        if nominal!=self.nominal:
            enabled=self.enabled;self.__init__(enabled);self.nominal=float(nominal)
        if now-self.last_observe<.5:return
        self.last_observe=now
        values=[max(0.,min(240.,float(v)*60/self.nominal)) for v in speeds if isinstance(v,(int,float)) and math.isfinite(v)]
        self.video_fps=min(values) if active and values else (0. if active else None)
        self.active=bool(active)
        if not self.enabled:self.reason='disabled';return
        if not active:
            self.fps=18.;self.active_since=self.low_since=self.high_since=None
            self.history.clear();self.recovery_fps=None
            self.probe=None;self.retry_after=-math.inf;self.reason='no active high-fps preview';return
        self.history.append((now,self.video_fps))
        while self.history and now-self.history[0][0]>6:self.history.popleft()
        self.recovery_fps=sum(v for _,v in self.history)/len(self.history)
        if self.active_since is None:
            self.active_since=now;self.fps=min(self.fps,12.);self.low_since=self.high_since=None
        if now-self.active_since<3:self.reason='video warmup';return
        if self.probe is not None and now-self.probe[1]>=10:
            self.probe=None
        if self.video_fps<57.5:
            self.high_since=None
            if self.low_since is None:self.low_since=now
            if now-self.low_since>=1.5 and now-self.last_adjust>=2:
                if self.probe is not None:
                    self.fps=self.probe[0];self.probe=None;self.retry_after=now+30
                    self.reason='rollback inference probe'
                else:
                    self.fps=max(8.,self.fps-2.);self.reason='reserve video headroom'
                self.last_adjust=now;self.low_since=now
                return
            self.reason='reserve video headroom'
        elif self.recovery_fps>=58.5:
            self.low_since=None
            if self.high_since is None:self.high_since=now
            if (self.probe is None and now>=self.retry_after and now-self.high_since>=6
                    and now-self.last_adjust>=6 and self.fps<14.):
                self.probe=(self.fps,now);self.fps+=.5;self.last_adjust=now;self.high_since=now
            self.reason=('probe recovery cooldown' if now<self.retry_after else
                         'trial inference increase' if self.probe is not None else 'video headroom stable')
        else:self.low_since=self.high_since=None;self.reason='hold video margin'

    def delay(self,now):
        return max(0.,self.last_start+1/self.fps-now) if self.enabled and self.last_start is not None else 0.

    def started(self,now):self.last_start=now

    def status(self):
        return dict(enabled=self.enabled,target_video_fps=57.*self.nominal/60,total_inference_fps_limit=self.fps if self.enabled else None,
                    active_video=self.active,observed_video_fps=round(self.video_fps*self.nominal/60,2) if self.video_fps is not None else None,
                    recovery_window_fps=round(self.recovery_fps*self.nominal/60,2) if self.recovery_fps is not None else None,
                    probing=self.probe is not None,recovery_hold_seconds=round(max(0.,self.retry_after-self.last_observe),1),reason=self.reason)
