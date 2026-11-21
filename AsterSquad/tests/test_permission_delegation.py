"""Capabilities follow direct-child authority without granting root or racing receipts."""
import unittest
from unittest.mock import AsyncMock, patch

from relay.store import Problem
import test_grants


class PermissionDelegationTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_grants.GrantTests.setUp
    member = test_grants.GrantTests.member

    def catalog(self):
        self.service.mcp_catalog = {name: {'name': name, 'default': False,
            'server': {'url': 'https://example.invalid/mcp'}} for name in ('web', 'images')}

    async def test_grant_revoke_and_independent_user_permissions(self):
        parent = self.member(); child = self.member('child', 1, parent['id'])
        self.service.mode = 'real'; self.catalog()
        with patch('squad.service.gpu_devices', return_value=('/dev/dxg',)):
            await self.service.configure('owner', parent['id'], gpu_access=True,
                host_systemd=True, sandbox_enabled=False, mcp_services_ids=['web'])
            # This service was independently granted by the user.
            await self.service.configure('owner', child['id'], mcp_services_ids=['images'])
            await self.service.configure(parent['id'], child['id'], gpu_access=True,
                host_systemd=True, sandbox_enabled=False, mcp_services_ids=['web', 'images'])
            config = self.service.store.agent(child['id'])['config']
            self.assertTrue(config['gpu_access']); self.assertTrue(config['host_systemd'])
            self.assertFalse(config['sandbox_enabled'])
            for kwargs in ({'gpu_access': False}, {'host_systemd': False},
                           {'sandbox_enabled': True}, {'mcp_services_ids': []}):
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(Problem, '下级'):
                    await self.service.configure('owner', parent['id'], **kwargs)
            with self.assertRaises(Problem):
                await self.service.configure(parent['id'], child['id'], mcp_services_ids=[])
            await self.service.configure(parent['id'], child['id'], gpu_access=False,
                host_systemd=False, sandbox_enabled=True, mcp_services_ids=['images'])
            await self.service.configure('owner', parent['id'], gpu_access=False,
                host_systemd=False, sandbox_enabled=True, mcp_services_ids=[])
            self.assertEqual(self.service.service_ids(self.service.store.agent(child['id'])), ['images'])

    async def test_unowned_capabilities_and_root_are_not_transferred(self):
        parent = self.member(); child = self.member('child', 1, parent['id'])
        self.service.mode = 'real'; self.catalog()
        # Root does not imply explicit delegation rights for other capabilities.
        self.service.store.update(parent['id'], config={**parent['config'], 'run_as_root': True})
        with patch('squad.service.gpu_devices', return_value=('/dev/dxg',)):
            for kwargs in ({'gpu_access': True}, {'host_systemd': True},
                           {'sandbox_enabled': False}, {'mcp_services_ids': ['web']},
                           {'run_as_root': True}, {'run_as_root': False}):
                with self.subTest(kwargs=kwargs), self.assertRaises(Problem):
                    await self.service.configure(parent['id'], child['id'], **kwargs)
            # Also cannot revoke a user's independent grant outside own authority.
            await self.service.configure('owner', child['id'], host_systemd=True)
            with self.assertRaises(Problem):
                await self.service.configure(parent['id'], child['id'], host_systemd=False)
            self.assertTrue(self.service.store.agent(child['id'])['config']['host_systemd'])

    async def test_creation_defaults_explicit_selection_and_no_side_effect_on_denial(self):
        parent = self.member(); self.catalog(); self.service.mode = 'real'
        self.service.relay = AsyncMock(return_value={'account': {'id': 'child'}, 'token': 'dummy'})
        self.service.start = AsyncMock()
        with patch('squad.service.gpu_devices', return_value=('/dev/dxg',)):
            for kwargs in ({'gpu_access': True}, {'host_systemd': True},
                           {'sandbox_enabled': False}, {'mcp_services_ids': ['web']}):
                with self.subTest(kwargs=kwargs), self.assertRaises(Problem):
                    await self.service.create(parent['id'], 'denied', 'Test', **kwargs)
            self.service.relay.assert_not_called()
            self.assertEqual(len(self.service.store.agents()), 1)
            await self.service.configure('owner', parent['id'], gpu_access=True,
                host_systemd=True, sandbox_enabled=False, mcp_services_ids=['web'])
            default = await self.service.create(parent['id'], 'default', 'Test')
            self.assertFalse(default['config'].get('gpu_access', False))
            self.assertFalse(default['config'].get('host_systemd', False))
            self.assertTrue(default['config']['sandbox_enabled'])
            child = await self.service.create(parent['id'], 'explicit', 'Test', gpu_access=True,
                host_systemd=True, sandbox_enabled=False, mcp_services_ids=['web'])
            self.assertTrue(child['config']['gpu_access'])
            self.assertTrue(child['config']['host_systemd'])
            self.assertFalse(child['config']['run_as_root'])

    async def test_pending_receipts_and_restart_reservations_fence_delegation(self):
        parent = self.member(); child = self.member('child', 1, parent['id'])
        await self.service.configure('owner', parent['id'], host_systemd=True)
        self.service.save_permission_update(parent['id'], {'id': 'test', 'status': 'pending', 'created': 0, 'previous': parent['config']})
        with self.assertRaisesRegex(Problem, '正在更新'):
            await self.service.configure(parent['id'], child['id'], host_systemd=True)
        with self.assertRaisesRegex(Problem, '正在更新'):
            await self.service.configure('owner', child['id'], host_systemd=True)
        self.service.save_permission_update(parent['id'], {'id': 'test', 'status': 'applied', 'created': 0})
        self.service.configuration_reservations.add(child['id'])
        with self.assertRaisesRegex(Problem, '正在更新'):
            await self.service.configure('owner', parent['id'], host_systemd=False)
        self.service.configuration_reservations.clear()
        await self.service.configure(parent['id'], child['id'], host_systemd=True)
        grandchild = self.member('grandchild', 2, child['id'])
        await self.service.configure(child['id'], grandchild['id'], host_systemd=True)
        with self.assertRaisesRegex(Problem, 'grandchild'):
            await self.service.configure(parent['id'], child['id'], host_systemd=False)
        await self.service.configure(child['id'], grandchild['id'], host_systemd=False)
        await self.service.configure(parent['id'], child['id'], host_systemd=False)
        await self.service.configure('owner', parent['id'], host_systemd=False)

    def test_legacy_windows_deny_is_not_migrated_to_global_read(self):
        from squad.store import Store
        closed = Store.normalize_config({'grants': [], 'sandbox_default_access': 'deny'})
        self.assertEqual(closed['grants'], [])
        self.assertEqual(closed['filesystem_format'], 2)
        self.assertNotIn('sandbox_default_access', closed)
        opened = Store.normalize_config({'grants': [], 'sandbox_default_access': 'read'})
        self.assertEqual(opened['grants'], [{'path': '/', 'access': 'read'}])
