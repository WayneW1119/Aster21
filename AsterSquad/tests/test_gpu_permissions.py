"""Explicit GPU policy, live receipts, retained settings and rollback."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from relay.store import Problem
import test_grants
import test_live_permissions


class GPUPermissionTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_grants.GrantTests.setUp
    member = test_grants.GrantTests.member
    running = test_live_permissions.LivePermissionTests.running

    async def test_unowned_gpu_denied_hardware_validation_and_saved_offline_setting(self):
        member = self.member()
        child = self.member('child', 1, member['id'])
        with self.assertRaises(Problem):
            await self.service.configure(member['id'], child['id'], gpu_access=True)
        for invalid in (1, 'true', []):
            with self.subTest(value=invalid), self.assertRaises(Problem):
                await self.service.configure('owner', member['id'], gpu_access=invalid)
        self.service.mode = 'real'
        with patch('squad.service.gpu_devices', return_value=()):
            with self.assertRaises(Problem):
                await self.service.configure('owner', member['id'], gpu_access=True)
        with patch('squad.service.gpu_devices', return_value=('/dev/dxg',)):
            configured = await self.service.configure('owner', member['id'], gpu_access=True)
            self.assertTrue(configured['filesystem']['gpu_access'])
            disabled = await self.service.configure('owner', member['id'], sandbox_enabled=False)
            self.assertTrue(disabled['config']['gpu_access'])
            self.assertIsNone(disabled['filesystem']['gpu_access'])
            enabled = await self.service.configure('owner', member['id'], sandbox_enabled=True)
            self.assertTrue(enabled['filesystem']['gpu_access'])

    async def test_gpu_only_change_updates_worker_without_pause_and_rolls_back_on_failure(self):
        self.service.mode = 'real'
        member = self.member()
        proc = self.running(member)
        sent = []
        proc.stdin.write = lambda value: sent.append(json.loads(value))
        with patch('squad.service.gpu_devices', return_value=('/dev/dxg',)), patch(
                'squad.service.POLICY_WAIT_SECONDS', .01), patch.object(self.service, '_stop', AsyncMock()) as stop:
            result = await self.service.configure('owner', member['id'], gpu_access=True)
            stop.assert_not_called()
        self.assertEqual(result['configuration_update'], 'pending')
        self.assertTrue(sent[-1]['policy']['gpu'])
        self.assertFalse(result['filesystem']['gpu_access'])
        update = self.service.permission_update(member['id'])
        self.service.complete_permission_update(member['id'], update['id'], False, {'error': 'fixture rejection'})
        self.assertNotIn('gpu_access', self.service.store.agent(member['id'])['config'])
        self.assertIs(self.service.workers[member['id']], proc)

