"""Root and Windows administrator share management policy, not OS launchers."""
import unittest
from unittest.mock import AsyncMock, patch
from relay.store import Problem
import test_grants

class PrivilegedIdentityTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_grants.GrantTests.setUp
    member = test_grants.GrantTests.member

    async def test_parent_cannot_reconfigure_start_or_reset_privileged_child(self):
        parent = self.member(); child = self.member('child', 1, parent['id'])
        for flag in ('run_as_root', 'windows_admin'):
            self.service.store.update(child['id'], config={**child['config'], flag: True})
            with self.subTest(flag=flag):
                with self.assertRaises(Problem):
                    await self.service.configure(parent['id'], child['id'], instruction='new instructions')
                with self.assertRaises(Problem):
                    await self.service.start(parent['id'], child['id'])
                with self.assertRaises(Problem):
                    await self.service.reconnect_relay(parent['id'], child['id'])

    async def test_owner_can_edit_saved_sandbox_settings_without_touching_privileged_worker(self):
        child = self.member(); self.service.mode = 'real'
        for flag in ('run_as_root', 'windows_admin'):
            self.service.store.update(child['id'], config={**child['config'], flag: True})
            worker = object(); self.service.workers[child['id']] = worker
            with patch.object(self.service, '_stop', AsyncMock()) as stop:
                for enabled in (False, True):
                    updated = await self.service.configure('owner', child['id'], sandbox_enabled=enabled)
                    self.assertEqual(updated['config']['sandbox_enabled'], enabled)
                    self.assertFalse(updated['sandbox_enforced'])
                    self.assertIs(self.service.workers[child['id']], worker)
                stop.assert_not_called()
            self.service.workers.pop(child['id'])
