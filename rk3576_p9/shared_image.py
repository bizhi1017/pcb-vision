"""Anonymous RAM-backed HQ storage; readers borrow the original ndarray lease."""
import mmap,os,weakref
import numpy as np


class SharedStorage(mmap.mmap):
    pass


def allocate_storage(size,shared=False):
    if shared and hasattr(os,'memfd_create'):
        descriptor=None
        try:
            descriptor=os.memfd_create('pcb_hq',getattr(os,'MFD_CLOEXEC',0));os.ftruncate(descriptor,size)
            storage=SharedStorage(descriptor,size,access=mmap.ACCESS_WRITE)
            storage.shared_fd=descriptor;storage.shared_size=size;storage.shared_inode=os.fstat(descriptor).st_ino
            weakref.finalize(storage,os.close,descriptor)
            return storage
        except (OSError,ValueError):
            if descriptor is not None:os.close(descriptor)
    return bytearray(size)


def shared_descriptor(image):
    if not image.flags.c_contiguous:return None
    base=image
    while getattr(base,'base',None) is not None:base=base.base
    if not isinstance(base,SharedStorage):return None
    anchor=np.ndarray((base.shared_size,),np.uint8,buffer=base)
    offset=image.ctypes.data-anchor.ctypes.data
    if offset<0 or offset+image.nbytes>base.shared_size:return None
    return dict(path='/proc/%d/fd/%d'%(os.getpid(),base.shared_fd),size=base.shared_size,inode=base.shared_inode,offset=offset)
