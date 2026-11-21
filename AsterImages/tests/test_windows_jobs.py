"""Job admission precedes execution; cancellation never abandons a suspended child."""
import asyncio
import importlib
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aster_images import _windows as windows


class WindowsJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        class Handle:
            def Close(inner):
                self.events.append('close')
        self.job = Handle()
        class Process:
            pid = 42
            returncode = None
            def kill(inner):
                self.events.append('kill'); inner.returncode = -1
            async def wait(inner):
                return inner.returncode
        self.process = Process()
        self.assign = Mock(side_effect=lambda *a: self.events.append('assign'))
        self.resume = Mock(side_effect=lambda *a: self.events.append('resume') or 1)
        modules = {
            'win32job': SimpleNamespace(CreateJobObject=lambda *a: self.job,
                QueryInformationJobObject=lambda *a: {'BasicLimitInformation': {'LimitFlags': 0}},
                SetInformationJobObject=lambda *a: self.events.append('limits'),
                AssignProcessToJobObject=self.assign, TerminateJobObject=lambda *a: None,
                JobObjectExtendedLimitInformation=1, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=2),
            'win32api': SimpleNamespace(OpenProcess=lambda *a: Handle(), OpenThread=lambda *a: Handle()),
            'win32con': SimpleNamespace(PROCESS_SET_QUOTA=1, PROCESS_TERMINATE=2, THREAD_SUSPEND_RESUME=2, CREATE_SUSPENDED=4),
            'win32process': SimpleNamespace(ResumeThread=self.resume),
            'psutil': SimpleNamespace(Process=lambda pid: SimpleNamespace(threads=lambda: [SimpleNamespace(id=7)])),
        }
        self.enterContext(patch.dict('sys.modules', modules))
        self.enterContext(patch.multiple(subprocess, CREATE_NEW_PROCESS_GROUP=0x200,
            CREATE_NO_WINDOW=0x8000000, create=True))

    async def test_suspend_assign_then_resume(self):
        async def create(*args, **kwargs):
            self.assertTrue(kwargs['creationflags'] & 0x00000004)
            self.events.append('create')
            return self.process
        with patch.object(windows.asyncio, 'create_subprocess_exec', side_effect=create):
            process = await windows.spawn('fixture')
        self.assertLess(self.events.index('limits'), self.events.index('create'))
        self.assertLess(self.events.index('assign'), self.events.index('resume'))
        self.assertIs(process, self.process)
        await windows.terminate_tree(process)

    async def test_admission_failure_kills_without_resuming(self):
        self.assign.side_effect = RuntimeError('assignment denied')
        with patch.object(windows.asyncio, 'create_subprocess_exec', AsyncMock(return_value=self.process)):
            with self.assertRaisesRegex(RuntimeError, 'assignment denied'):
                await windows.spawn('fixture')
        self.resume.assert_not_called()
        self.assertIn('kill', self.events)
        self.assertEqual(self.events[-1], 'close')

    async def test_cancel_during_creation_reaps_child_without_resuming(self):
        entered = asyncio.Event(); release = asyncio.Event()
        async def create(*args, **kwargs):
            entered.set(); await release.wait()
            return self.process
        with patch.object(windows.asyncio, 'create_subprocess_exec', side_effect=create):
            task = asyncio.create_task(windows.spawn('fixture'))
            await entered.wait(); task.cancel(); release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        self.resume.assert_not_called()
        self.assertIn('kill', self.events)
        self.assertEqual(self.events[-1], 'close')

    async def test_creation_failure_closes_job(self):
        with patch.object(windows.asyncio, 'create_subprocess_exec', AsyncMock(side_effect=OSError('launch failed'))):
            with self.assertRaises(OSError):
                await windows.spawn('fixture')
        self.resume.assert_not_called()
        self.assertEqual(self.events[-1], 'close')
