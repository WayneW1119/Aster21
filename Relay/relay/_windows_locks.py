"""Windows byte-range locks, released automatically when the descriptor closes."""
import ctypes
from ctypes import wintypes
import msvcrt
LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8
class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.LockFileEx.argtypes = [wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(OVERLAPPED)]
kernel.UnlockFileEx.argtypes = [wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(OVERLAPPED)]
def flock(file, operation):
    fd = file if isinstance(file,int) else file.fileno()
    handle = msvcrt.get_osfhandle(fd)
    overlapped = OVERLAPPED()
    # Windows locks are mandatory. Reserve an out-of-data byte so normal readers
    # retain POSIX advisory-lock semantics without touching file contents/length.
    overlapped.OffsetHigh = 0x10000000
    if operation & LOCK_UN:
        ok = kernel.UnlockFileEx(handle,0,1,0,ctypes.byref(overlapped))
    else:
        flags = (2 if operation & LOCK_EX else 0) | (1 if operation & LOCK_NB else 0)
        ok = kernel.LockFileEx(handle,flags,0,1,0,ctypes.byref(overlapped))
    if not ok:
        code = ctypes.get_last_error()
        if code == 33: raise BlockingIOError(code,'File is locked')
        raise ctypes.WinError(code)
