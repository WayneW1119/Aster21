"""Local IPC uses the same delegation and live-policy receipt boundaries as files."""
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from relay.store import Problem
import test_grants
import test_live_permissions


@unittest.skipIf(os.name == 'nt', 'Linux/WSL capability')
class LocalIPCTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_grants.GrantTests.setUp
    member = test_grants.GrantTests.member
    running = test_live_permissions.LivePermissionTests.running

    async def test_delegation_creation_validation_and_saved_disabled_policy(self):
        self.service.mode = 'real'
        parent = self.member(); child = self.member('child', 1, parent['id'])
        for value in (1, [], 'yes'):
            with self.assertRaises(Problem):
                await self.service.configure('owner', parent['id'], local_ipc=value)
        with self.assertRaises(Problem):
            await self.service.configure(parent['id'], child['id'], local_ipc=True)
        await self.service.configure('owner', parent['id'], local_ipc=True)
        await self.service.configure(parent['id'], child['id'], local_ipc=True)
        with self.assertRaisesRegex(Problem, '下级'):
            await self.service.configure('owner', parent['id'], local_ipc=False)
        self.service.relay = AsyncMock(return_value={'account': {'id': 'new'}, 'token': 'dummy'})
        self.service.start = AsyncMock()
        created = await self.service.create(parent['id'], 'new', 'Test', local_ipc=True)
        self.assertTrue(created['config']['local_ipc'])
        await self.service.configure('owner', child['id'], sandbox_enabled=False)
        state = self.service.view(self.service.store.agent(child['id']))
        self.assertTrue(state['config']['local_ipc'])
        self.assertIsNone(state['filesystem']['local_ipc'])
        await self.service.configure('owner', child['id'], sandbox_enabled=True, local_ipc=False)
        with self.assertRaisesRegex(Problem, '本机进程通信'):
            with patch('squad.service.os.name', 'nt'):
                self.service.set_local_ipc({}, True)

    async def test_live_update_without_pause_and_failure_rollback(self):
        self.service.mode = 'real'
        member = self.member(); proc = self.running(member)
        sent = []; proc.stdin.write = lambda value: sent.append(json.loads(value))
        with patch('squad.service.POLICY_WAIT_SECONDS', .01), patch.object(self.service, '_stop', AsyncMock()) as stop:
            result = await self.service.configure('owner', member['id'], local_ipc=True)
            stop.assert_not_called()
        self.assertTrue(sent[-1]['policy']['local_ipc'])
        self.assertFalse(result['filesystem']['local_ipc'])
        update = self.service.permission_update(member['id'])
        self.service.complete_permission_update(member['id'], update['id'], False, {'error': 'probe rejected'})
        self.assertNotIn('local_ipc', self.service.store.agent(member['id'])['config'])
        self.assertIs(self.service.workers[member['id']], proc)
