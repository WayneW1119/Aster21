"""Linux wall-clock deadlines integrated with asyncio, without polling.

The persisted timer deadline uses CLOCK_REALTIME. Keep the kernel wait on that
same clock; asyncio.sleep instead translates it to a monotonic duration.
"""
import asyncio
import ctypes
import errno
import os
import sys
import time

_ABSTIME = 1
_CANCEL_ON_SET = 2


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


class _Itimerspec(ctypes.Structure):
    _fields_ = [("it_interval", _Timespec), ("it_value", _Timespec)]


class _TimerFD:
    """Small libc binding for Python 3.11/3.12 as well as newer Python."""

    def __init__(self):
        if sys.platform != "linux":
            raise NotImplementedError("Harness timers require Linux timerfd")
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.timerfd_create.argtypes = [ctypes.c_int, ctypes.c_int]
        self._libc.timerfd_create.restype = ctypes.c_int
        self._libc.timerfd_settime.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.POINTER(_Itimerspec), ctypes.c_void_p]
        self._libc.timerfd_settime.restype = ctypes.c_int
        self.fd = self._libc.timerfd_create(
            time.CLOCK_REALTIME, os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd == -1:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))

    def arm(self, deadline):
        # Zero disarms timerfd; callers handle already-expired deadlines directly.
        nanoseconds = max(1, round(deadline * 1_000_000_000))
        seconds, nanos = divmod(nanoseconds, 1_000_000_000)
        setting = _Itimerspec(_Timespec(0, 0), _Timespec(seconds, nanos))
        if self._libc.timerfd_settime(
                self.fd, _ABSTIME | _CANCEL_ON_SET, ctypes.byref(setting), None) == -1:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))

    def read(self):
        return os.read(self.fd, 8)

    def close(self):
        os.close(self.fd)


async def wait_until(deadline: float) -> None:
    """Wait until a realtime deadline; clock changes recheck/rearm the same date.

    CLOCK_REALTIME absolute timerfd handles gradual clock adjustments in-kernel.
    CANCEL_ON_SET wakes us for discontinuous changes. Backward changes must not
    cause early delivery; forward changes past the deadline deliver once.
    This cannot wake a suspended host. On execution resuming, overdue timers
    become ready. The Session retains ownership of event emission/progress.
    """
    if sys.platform == 'win32':
        while deadline > time.time():
            await asyncio.sleep(min(.25, deadline - time.time()))
        return
    if deadline <= time.time():
        return
    timer = _TimerFD()
    loop = asyncio.get_running_loop()
    try:
        while deadline > time.time():
            ready = loop.create_future()

            def readable():
                if ready.done():
                    return
                try:
                    timer.read()
                except BlockingIOError:
                    return
                except OSError as error:
                    if error.errno != errno.ECANCELED:
                        ready.set_exception(error)
                        return
                ready.set_result(None)

            loop.add_reader(timer.fd, readable)
            try:
                try:
                    timer.arm(deadline)
                except OSError as error:
                    # Linux may report an intervening clock change on rearm too.
                    if error.errno == errno.ECANCELED:
                        continue
                    raise
                await ready
            finally:
                loop.remove_reader(timer.fd)
    finally:
        timer.close()
