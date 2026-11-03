"""Process-owned CPU roles for the confirmed RK3576 layout; no OS writes here."""
import os

def choose_cpus(role,allowed,camera=None,shared=None):
    allowed=set(allowed)
    shared=os.environ.get('PCB_CPU_LAYOUT')=='shared' if shared is None else shared
    if role=='capture':
        if camera not in (0,1):raise ValueError('invalid camera CPU role')
        preferred=(0,2)[camera]
        if not shared and preferred in allowed:return {preferred}
        return {cpu for cpu in allowed if cpu>=4} or allowed
    if role in ('inference_worker','inference_parent'):
        if shared:return (allowed&{0,1,2,3} or allowed) if role=='inference_worker' else allowed
        return allowed&{1,3} or allowed
    if role=='hdmi':return allowed if shared else allowed&{1,3} or allowed
    raise ValueError('invalid CPU role')
