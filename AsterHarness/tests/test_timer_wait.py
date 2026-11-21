import asyncio
import errno
import os
import time
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_harness.core.event_generators import timer_wait
from agent_harness.core.event_generators.timer import TimerGenerator


class FakeTimerFD:
    """Real readiness notification, controllable clock and kernel read outcome."""

    def __init__(self):
        self.fd, self.writer = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.arms = []
        self.error = None
        self.closed = False

    def arm(self, deadline):
        self.arms.append(deadline)

    def read(self):
        value = os.read(self.fd, 8)
        if self.error:
            raise OSError(self.error, "injected kernel notification")
        return value

    def notify(self, error=None):
        self.error = error
        os.write(self.writer, b"12345678")

    def close(self):
        self.closed = True
        os.close(self.fd)
        os.close(self.writer)


@unittest.skipUnless(sys.platform == "linux", "Linux timers")
class DeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def start_fake(self):
        fake = FakeTimerFD()
        clock = SimpleNamespace(now=100.0)
        self.enterContext(patch.object(timer_wait, "_TimerFD", return_value=fake))
        self.enterContext(patch.object(timer_wait, "time",
                                      SimpleNamespace(time=lambda: clock.now)))
        task = asyncio.create_task(timer_wait.wait_until(200.0))
        async def cleanup():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.addAsyncCleanup(cleanup)
        await asyncio.sleep(0)
        self.assertEqual(fake.arms, [200.0])
        return fake, clock, task

    async def drain_callbacks(self):
        for _ in range(5):
            await asyncio.sleep(0)

    async def test_actual_kernel_deadline(self):
        started = time.time()
        await asyncio.wait_for(timer_wait.wait_until(started + .05), 2)
        self.assertGreaterEqual(time.time(), started + .05)

    async def test_overdue_does_not_allocate_descriptor(self):
        with patch.object(timer_wait, "_TimerFD") as create:
            await timer_wait.wait_until(time.time() - 10)
        create.assert_not_called()

    async def test_forward_clock_change_past_deadline_completes(self):
        fake, clock, task = await self.start_fake()
        clock.now = 300
        fake.notify(errno.ECANCELED)
        await asyncio.wait_for(task, 1)
        self.assertEqual(fake.arms, [200.0])
        self.assertTrue(fake.closed)

    async def test_backward_change_rearms_without_early_delivery(self):
        fake, clock, task = await self.start_fake()
        clock.now = 50
        fake.notify(errno.ECANCELED)
        await self.drain_callbacks()
        self.assertFalse(task.done())
        self.assertEqual(fake.arms, [200.0, 200.0])
        clock.now = 200
        fake.notify()
        await asyncio.wait_for(task, 1)
        self.assertTrue(fake.closed)

    async def test_forward_change_before_deadline_rearms(self):
        fake, clock, task = await self.start_fake()
        clock.now = 150
        fake.notify(errno.ECANCELED)
        await self.drain_callbacks()
        self.assertFalse(task.done())
        self.assertEqual(fake.arms, [200.0, 200.0])

    async def test_spurious_readiness_does_not_finish_early(self):
        fake, clock, task = await self.start_fake()
        fake.notify()
        await self.drain_callbacks()
        self.assertFalse(task.done())
        self.assertEqual(fake.arms, [200.0, 200.0])

    async def test_cancellation_closes_descriptor_and_removes_reader(self):
        fake, clock, task = await self.start_fake()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(fake.closed)
        self.assertNotIn(fake.fd, asyncio.get_running_loop()._selector.get_map())

    async def test_read_error_propagates_and_closes_descriptor(self):
        fake, clock, task = await self.start_fake()
        fake.notify(errno.EIO)
        with self.assertRaises(OSError) as error:
            await task
        self.assertEqual(error.exception.errno, errno.EIO)
        self.assertTrue(fake.closed)

    async def test_clock_change_during_rearm_is_retried(self):
        fake = FakeTimerFD()
        arm = fake.arm
        attempts = 0
        def arm_with_change(deadline):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError(errno.ECANCELED, "clock changed during rearm")
            arm(deadline)
        with patch.object(timer_wait, "_TimerFD", return_value=fake), \
                patch.object(fake, "arm", side_effect=arm_with_change):
            task = asyncio.create_task(timer_wait.wait_until(time.time() + 30))
            try:
                await asyncio.sleep(0)
                self.assertEqual(attempts, 2)
                self.assertFalse(task.done())
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(fake.closed)

    async def test_native_wait_cancellation_releases_kernel_descriptor(self):
        native = timer_wait._TimerFD()
        fd = native.fd
        with patch.object(timer_wait, "_TimerFD", return_value=native):
            task = asyncio.create_task(timer_wait.wait_until(time.time() + 30))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with self.assertRaises(OSError) as error:
            os.fstat(fd)
        self.assertEqual(error.exception.errno, errno.EBADF)

    async def test_arm_failure_closes_descriptor(self):
        fake = FakeTimerFD()
        with patch.object(timer_wait, "_TimerFD", return_value=fake), \
                patch.object(fake, "arm", side_effect=OSError(errno.EINVAL, "bad setting")):
            with self.assertRaises(OSError):
                await timer_wait.wait_until(time.time() + 30)
        self.assertTrue(fake.closed)


@unittest.skipUnless(sys.platform == "linux", "Linux timers")
class TimerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_timer_does_not_depend_on_loop_monotonic_deadline(self):
        # Freeze the event loop clock after setup. A monotonic sleep cannot expire,
        # but timerfd readiness still wakes the selector and delivers the event.
        loop = asyncio.get_running_loop()
        timer = TimerGenerator(30, "native deadline")
        events = []
        async def emit(kind, data):
            events.append((kind, data))
        fixed = loop.time()
        task = asyncio.create_task(timer.run(emit))
        watchdog = threading.Timer(2, lambda: loop.call_soon_threadsafe(task.cancel))
        watchdog.start()
        try:
            with patch.object(loop, "time", return_value=fixed):
                await task
        finally:
            watchdog.cancel()
            watchdog.join()
        self.assertEqual(events, [("timer_fired", {
            "message": "native deadline", "occurrence": 1})])
        self.assertEqual(timer.events_emitted, 1)
        self.assertIsNone(timer.next_fire_at)
