"""Windows host primitives. Imported only by Windows execution paths."""
import asyncio, os, signal, subprocess, threading, queue, sys




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
    import win32job
    job=_jobs.pop(process,None)
    if job is not None:
        try:win32job.TerminateJobObject(job,1)
        finally:job.Close()
    elif process.returncode is None:
        process.kill()
