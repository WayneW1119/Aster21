"""Windows host primitives. Imported only by Windows execution paths."""
import asyncio, os, signal, subprocess, threading, queue, sys

def shell_arguments(shell, command, login):
    name=os.path.basename(shell).lower()
    if name in ('powershell.exe','powershell','pwsh.exe','pwsh'):
        return [shell,'-NoLogo',* ([] if login else ['-NoProfile']),'-NonInteractive','-Command','[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); ' + command]
    if name in ('cmd.exe','cmd'): return [shell,'/d','/s','/c',command]
    return [shell,'-lc' if login else '-c',command]

class StdinLines:
    # A daemon thread avoids asyncio's non-cancellable executor shutdown waiting
    # forever on a console read. The worker owns stdin for its entire lifetime.
    def __init__(self):
        self.queue=queue.Queue()
        threading.Thread(target=self._read,daemon=True).start()
    def _read(self):
        while True:
            line=sys.stdin.readline();self.queue.put(line)
            if not line:return
    async def readline(self):
        while True:
            try:return self.queue.get_nowait()
            except queue.Empty:await asyncio.sleep(.025)

def install_stop_handler(loop, callback):
    import win32event
    event=win32event.CreateEvent(None,False,False,os.environ.get('ASTER_HOST_STOP_EVENT','Local\\AsterStop-' + str(os.getpid())))
    def wait_stop():
        win32event.WaitForSingleObject(event,win32event.INFINITE)
        try:loop.call_soon_threadsafe(callback)
        except RuntimeError:pass
        finally:event.Close()
    threading.Thread(target=wait_stop,daemon=True).start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig,lambda *_:loop.call_soon_threadsafe(callback))

import weakref
_jobs = weakref.WeakKeyDictionary()
async def spawn(*args, **kwargs):
    """Attach a suspended process to its kill-on-close Job before any code runs."""
    import psutil, uuid, win32job, win32api, win32con, win32process
    kwargs.pop('start_new_session', None)
    kwargs['creationflags'] = (kwargs.get('creationflags', 0) | subprocess.CREATE_NEW_PROCESS_GROUP
                               | subprocess.CREATE_NO_WINDOW | win32con.CREATE_SUSPENDED)
    stop_name = 'Local\\AsterStop-' + uuid.uuid4().hex
    kwargs['env'] = dict(kwargs.get('env') or os.environ)
    kwargs['env']['ASTER_HOST_STOP_EVENT'] = stop_name
    job = win32job.CreateJobObject(None, '')
    process = creation = None
    try:
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        # Cancellation must not abandon a process created while awaiting its pipes.
        creation = asyncio.create_task(asyncio.create_subprocess_exec(*args, **kwargs))
        process = await asyncio.shield(creation)
        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        threads = psutil.Process(process.pid).threads()
        if len(threads) != 1:
            raise RuntimeError('Expected one suspended primary thread')
        thread = win32api.OpenThread(win32con.THREAD_SUSPEND_RESUME, False, threads[0].id)
        try:
            if win32process.ResumeThread(thread) != 1:
                raise RuntimeError('Primary thread was not suspended exactly once')
        finally:
            thread.Close()
    except BaseException:
        try:
            if process is None and creation is not None:
                try:
                    process = await asyncio.shield(creation)
                except BaseException:
                    pass
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        finally:
            job.Close()
        raise
    process._aster_stop_event = stop_name
    _jobs[process] = job
    weakref.finalize(process, job.Close)
    return process

async def terminate_tree(process):
    if sandbox_stop := getattr(process, '_aster_sandbox_stop', None):
        await sandbox_stop()
        return
    import win32job
    job=_jobs.pop(process,None)
    if job is not None:
        try:win32job.TerminateJobObject(job,1)
        finally:job.Close()
    elif process.returncode is None:
        process.kill()

def read_confined(path, limit):
    import win32file, win32con
    from pathlib import Path
    path=Path(path).absolute()
    if any(p.is_symlink() or is_reparse(p) for p in (path,*path.parents)):
        raise ValueError('Reparse paths are unavailable for private resources')
    handle=win32file.CreateFile(str(path),win32con.GENERIC_READ,win32con.FILE_SHARE_READ,None,win32con.OPEN_EXISTING,0x00200000,0)
    try:
        actual=win32file.GetFinalPathNameByHandle(handle,0)
        if actual.startswith(chr(92)*2 + '?' + chr(92)):actual=actual[4:]
        if os.path.normcase(actual) != os.path.normcase(str(path)):
            raise ValueError('Private resource path changed during opening')
        size=win32file.GetFileSize(handle)
        if size>limit:raise ValueError('Private resource exceeds size limit')
        return win32file.ReadFile(handle,size)[1] if size else b''
    finally:handle.Close()

def request_stop(process):
    """Request cooperative shutdown, including venv redirector children."""
    import win32event,win32con,psutil
    names=[getattr(process,'_aster_stop_event','Local\\AsterStop-' + str(process.pid))]
    try:names.extend('Local\\AsterStop-' + str(p.pid) for p in psutil.Process(process.pid).children(recursive=True))
    except psutil.Error:pass
    for name in names:
        try:event=win32event.OpenEvent(win32con.EVENT_MODIFY_STATE,False,name)
        except Exception:continue
        try:win32event.SetEvent(event)
        finally:event.Close()
        return
    raise RuntimeError('No cooperative shutdown endpoint; process was not force-killed')

def is_reparse(path):
    try:return bool(os.lstat(path).st_file_attributes & 0x400)
    except FileNotFoundError:return False

def private_file(path):
    """Limit a private file to the current Windows account and LocalSystem."""
    import win32api, win32con, win32security
    if is_reparse(path):raise ValueError('Private file must not be a reparse point')
    token=win32security.OpenProcessToken(win32api.GetCurrentProcess(),win32con.TOKEN_QUERY)
    try:sid=win32security.GetTokenInformation(token,win32security.TokenUser)[0]
    finally:token.Close()
    acl=win32security.ACL()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION,0x1F01FF,sid)
    acl.AddAccessAllowedAce(win32security.ACL_REVISION,0x1F01FF,win32security.CreateWellKnownSid(win32security.WinLocalSystemSid))
    win32security.SetNamedSecurityInfo(str(path),win32security.SE_FILE_OBJECT,win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,None,None,acl,None)
