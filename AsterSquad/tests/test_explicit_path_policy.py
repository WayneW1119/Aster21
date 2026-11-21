"""Root rules are editable, persist across reopen, and cannot uncover secrets."""
import unittest

from relay.store import Problem
from squad.store import Store
import test_grants


class ExplicitPathPolicyTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_grants.GrantTests.setUp
    member = test_grants.GrantTests.member
    def test_legacy_store_migrates_once_then_preserves_removal(self):
        member = self.member()
        config = dict(member['config']);config.pop('filesystem_format')
        self.service.store.update(member['id'], config=config)
        store = Store(self.service.private/'squad.db')
        self.addCleanup(store.db.close)
        migrated = store.agent(member['id'])
        self.assertIn({'path': '/', 'access': 'read'}, migrated['config']['grants'])
        self.assertEqual(migrated['config']['filesystem_format'], 2)
        store.update(member['id'], config={**migrated['config'], 'grants': []})
        reopened = Store(self.service.private/'squad.db')
        self.addCleanup(reopened.db.close)
        self.assertEqual(reopened.agent(member['id'])['config']['grants'], [])

    def test_root_rule_allowed_without_reopening_platform_paths(self):
        rules = self.service.grants('owner', [{'path': '/', 'access': 'read'}])
        member = self.member(grants=rules)
        self.assertTrue(any(r['access'] == 'deny' for r in self.service.rules(member)))
        with self.assertRaises(Problem):
            self.service.grants('owner', [{'path': str(self.service.private), 'access': 'read'}])

    async def test_root_can_be_removed_and_children_cannot_reintroduce_it(self):
        parent = self.member(grants=[{'path': '/', 'access': 'read'}])
        child = self.member('child', 1, parent['id'])
        await self.service.configure('owner', parent['id'], grants=[])
        with self.assertRaises(Problem):
            await self.service.configure(parent['id'], child['id'], grants=[{'path': '/', 'access': 'read'}])
        self.assertEqual(self.service.view(self.service.store.agent(parent['id']))['filesystem']['default_access'], 'unrestricted')

    async def test_parent_root_cannot_be_removed_until_delegated_root_is_reduced(self):
        parent = self.member(grants=[{'path': '/', 'access': 'read'}])
        self.member('child', 1, parent['id'], grants=[{'path': '/', 'access': 'read'}])
        with self.assertRaises(Problem):
            await self.service.configure('owner', parent['id'], grants=[])

    def test_previous_pending_policy_is_normalized_before_rollback(self):
        member = self.member()
        previous = dict(member['config']);previous.pop('filesystem_format')
        self.service.save_permission_update(member['id'], {'id': 'old', 'status': 'pending', 'previous': previous})
        update = self.service.permission_update(member['id'])
        self.assertIn({'path': '/', 'access': 'read'}, update['previous']['grants'])
