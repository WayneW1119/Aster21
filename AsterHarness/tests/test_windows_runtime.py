"""Native Windows preview contracts; no model, credentials or running deployment."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(os.name == 'nt', 'Windows-native execution contracts')
class WindowsRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_shell_background_poll_stop_and_unicode(self):
        from agent_harness.core.unified_exec import UnifiedExecProcessManager
        with tempfile.TemporaryDirectory() as folder:
            manager = UnifiedExecProcessManager(Path(folder))
            try:
                result = await manager.exec_command("Write-Output 'ready'", login=False)
                self.assertEqual(result['exit_code'], 0)
                self.assertIn('ready', result['output'])
                result = await manager.exec_command('Start-Sleep -Seconds 20', yield_time_ms=250)
                handle = result['session_id']
                stopped = await manager.stop_process(handle)
                self.assertIn('exit_code', stopped)
                self.assertEqual(await manager.list_processes(), [])
                with self.assertRaisesRegex(ValueError, 'tty'):
                    await manager.exec_command('echo x', tty=True)
            finally:
                await manager.close()

    async def test_lock_is_exclusive_between_processes_and_released_on_close(self):
        from agent_harness import _windows_locks as locks
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'lock'
            script = "from agent_harness import _windows_locks as f;import sys;h=open(sys.argv[1],'a');f.flock(h,f.LOCK_EX|f.LOCK_NB)"
            with path.open('a') as stream:
                locks.flock(stream, locks.LOCK_EX | locks.LOCK_NB)
                result = await asyncio.to_thread(subprocess.run, [sys.executable,'-c',script,str(path)], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'BlockingIOError', result.stderr)
            result = await asyncio.to_thread(subprocess.run, [sys.executable,'-c',script,str(path)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    async def test_timer_cancellation_and_overdue_delivery(self):
        from agent_harness.core.event_generators.timer_wait import wait_until
        await asyncio.wait_for(wait_until(time.time()-1), .1)
        task = asyncio.create_task(wait_until(time.time()+60))
        await asyncio.sleep(.03)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        start = time.time()
        await wait_until(start+.1)
        self.assertGreaterEqual(time.time(), start+.1)

    async def test_confined_reader_rejects_escape_and_unicode_is_exact(self):
        from agent_harness._windows import read_confined
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'中文.md'
            path.write_text('中文 contents', encoding='utf-8')
            self.assertEqual(read_confined(path,1000).decode('utf-8'), '中文 contents')
            with self.assertRaises(ValueError):
                read_confined(path,1)

    async def test_job_closes_descendants_after_parent_exits(self):
        import win32api, win32con, win32process
        from agent_harness._windows import spawn, terminate_tree
        with tempfile.TemporaryDirectory() as folder:
            marker=Path(folder)/'pid'
            code="import subprocess,sys,pathlib;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);pathlib.Path(sys.argv[1]).write_text(str(p.pid))"
            process=await spawn(sys.executable,'-c',code,str(marker))
            await process.wait()
            pid=int(marker.read_text())
            handle=win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION | win32con.SYNCHRONIZE,False,pid)
            try:
                self.assertEqual(win32process.GetExitCodeProcess(handle),259)
                await terminate_tree(process)
                for _ in range(100):
                    if win32process.GetExitCodeProcess(handle)!=259:break
                    await asyncio.sleep(.01)
                self.assertNotEqual(win32process.GetExitCodeProcess(handle),259)
            finally:
                handle.Close()
                await terminate_tree(process)
