"""Startup changes affect only a registered launcher, never current processes."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from relay.store import Problem
from squad.startup import Startup


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_launcher_is_unavailable_and_rejects_changes(self):
        startup = Startup()
        self.assertFalse((await startup.status())['available'])
        self.assertIsNone((await startup.status())['enabled'])
        with self.assertRaises(Problem):
            await startup.configure(True)
        for invalid in ('true', 1, None):
            with self.assertRaises(Problem):
                await startup.configure(invalid)
        for unit in ('--help', 'arbitrary/path.service', 'wrong.socket'):
            with self.assertRaises(ValueError):
                Startup(unit=unit)

    @unittest.skipIf(os.name == 'nt', 'systemd backend')
    async def test_systemd_toggles_only_enablement_of_fixed_unit(self):
        startup = Startup(unit='example.service')
        for enabled in (False, True):
            final = 'enabled' if enabled else 'disabled'
            with patch.object(startup, 'systemctl', AsyncMock(side_effect=[
                (0, 'enabled', ''), (0, '', ''), (0, final, '')])) as call:
                result = await startup.configure(enabled)
                self.assertEqual(result['enabled'], enabled)
                self.assertEqual([c.args for c in call.await_args_list], [
                    ('is-enabled', 'example.service'),
                    ('enable' if enabled else 'disable', 'example.service'),
                    ('is-enabled', 'example.service')])

    @unittest.skipIf(os.name == 'nt', 'systemd backend')
    async def test_missing_unit_and_failed_operation_are_not_reported_as_success(self):
        startup = Startup(unit='example.service')
        with patch.object(startup, 'systemctl', AsyncMock(return_value=(1, 'not-found', 'missing'))):
            self.assertFalse((await startup.status())['available'])
            with self.assertRaises(Problem):
                await startup.configure(True)
        with patch.object(startup, 'systemctl', AsyncMock(side_effect=[(1, 'disabled', ''), (1, '', 'read-only')])):
            with self.assertRaisesRegex(Problem, 'read-only'):
                await startup.configure(True)

    @unittest.skipIf(os.name == 'nt', 'systemd backend')
    async def test_timeout_reaps_only_control_command(self):
        startup = Startup(unit='example.service')
        class Process:
            returncode = 0
            calls = 0
            killed = False
            async def communicate(self):
                self.calls += 1
                if self.calls == 1:
                    raise asyncio.TimeoutError()
                return b'', b''
            def kill(self): self.killed = True
        process = Process()
        with patch('squad.startup.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
            with self.assertRaises(Problem):
                await startup.systemctl('is-enabled', 'example.service')
        self.assertTrue(process.killed)
        self.assertEqual(process.calls, 2)

    @unittest.skipUnless(os.name == 'nt', 'native Windows registry')
    async def test_windows_registry_round_trip_uses_isolated_instance(self):
        with tempfile.TemporaryDirectory() as root:
            startup = Startup(desktop_root=root)
            self.assertFalse((await startup.status())['enabled'])
            try:
                self.assertTrue((await startup.configure(True))['enabled'])
            finally:
                result = await startup.configure(False)
            self.assertFalse(result['enabled'])
