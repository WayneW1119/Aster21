"""Optional restart intent is distinct from worker status and explicit pause."""
import asyncio
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from relay.store import Problem
from squad.service import Service
from squad.store import Store
import test_grants


class AutostartTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_grants.GrantTests.setUp
    member = test_grants.GrantTests.member

    async def test_space_policy_overrides_legacy_member_flags_and_preserves_pause(self):
        keep = self.member()
        paused = self.member('paused', 1)
        self.service.store.update(keep['id'], run_requested=1)
        self.service.store.update(paused['id'], run_requested=0,
                                  config={**paused['config'], 'auto_start': True})
        self.service.store.set_restore_members(True)
        self.reopen()
        with patch.object(self.service, '_start', AsyncMock()) as start:
            await self.service.restore_on_startup()
            start.assert_awaited_once_with(keep['id'])
        self.service.store.set_restore_members(False)
        self.reopen()
        with patch.object(self.service, '_start', AsyncMock()) as start:
            await self.service.restore_on_startup()
            start.assert_not_awaited()

    async def test_space_disabled_while_start_is_queued_wins(self):
        a = self.member()
        self.service.store.update(a['id'], run_requested=1)
        self.service.store.set_restore_members(True)
        self.reopen()
        self.service.store.set_restore_members(False)
        with patch.object(self.service, '_start', AsyncMock()) as start:
            await self.service.restore_on_startup()
            start.assert_not_awaited()

    def reopen(self):
        self.service.store.db.close()
        self.service = Service(self.root/'squad', mode='fake', relay_url='http://127.0.0.1:1',
            relay_mcp='http://127.0.0.1:2/mcp', squad_mcp='http://127.0.0.1:3/mcp',
            owner_relay_token='test', domain='test', user_account='test', models=['fake'], model_url='https://example.invalid/responses')
        self.addCleanup(self.service.store.db.close)

    async def test_only_opted_in_requested_nonfailed_members_are_candidates(self):
        expected = None
        for slot, (name, enabled, requested, state, archived) in enumerate([
            ('keep', True, 1, 'online', 0), ('disabled', False, 1, 'online', 0),
            ('paused', True, 0, 'offline', 0), ('failed', True, 1, 'error', 0),
            ('archived', True, 1, 'archived', 1)]):
            a = self.member(name, slot)
            self.service.store.update(a['id'], config={**a['config'], 'auto_start': enabled},
                run_requested=requested, status=state, archived=archived)
            if name == 'keep': expected = a['id']
        self.reopen()
        with patch.object(self.service, '_start', AsyncMock()) as start:
            await self.service.restore_on_startup()
            await self.service.restore_on_startup()
            start.assert_awaited_once_with(expected)

    async def test_failed_restore_does_not_retry_in_this_or_next_controller(self):
        a = self.member()
        self.service.store.update(a['id'], config={**a['config'], 'auto_start': True}, run_requested=1)
        self.reopen()
        with patch.object(self.service, '_start', AsyncMock(side_effect=Problem('provider unavailable', 409))) as start:
            await self.service.restore_on_startup()
            await self.service.restore_on_startup()
            self.assertEqual(start.await_count, 1)
        self.assertEqual(self.service.store.agent(a['id'])['status'], 'error')
        self.reopen()
        with patch.object(self.service, '_start', AsyncMock()) as start:
            await self.service.restore_on_startup()
            start.assert_not_awaited()

    async def test_later_pause_or_disabled_setting_wins_over_queued_restore(self):
        a = self.member()
        self.service.store.update(a['id'], config={**a['config'], 'auto_start': True}, run_requested=1)
        self.reopen()
        await self.service.stop('owner', a['id'])
        with patch.object(self.service, '_start', AsyncMock()) as start:
            await self.service.restore_on_startup()
            start.assert_not_awaited()

    async def test_start_stop_and_shutdown_preserve_the_correct_intent(self):
        a = self.member()
        with patch.object(self.service, '_start', AsyncMock()), patch.object(self.service, '_stop', AsyncMock()):
            await self.service.start('owner', a['id'])
            self.assertTrue(self.service.store.agent(a['id'])['run_requested'])
            await self.service.stop('owner', a['id'])
            self.assertFalse(self.service.store.agent(a['id'])['run_requested'])
            await self.service.start('owner', a['id'])
            await self.service.close()
        self.reopen()
        self.assertTrue(self.service.store.agent(a['id'])['run_requested'])

    async def test_flag_edit_is_live_and_keeps_management_authorization(self):
        parent = self.member(); child = self.member('child', 1, parent['id']); other = self.member('other', 2)
        worker = object(); self.service.workers[child['id']] = worker
        with patch.object(self.service, '_stop', AsyncMock()) as stop, patch.object(self.service, 'prepare') as prepare:
            updated = await self.service.configure(parent['id'], child['id'], auto_start=True)
            self.assertTrue(updated['config']['auto_start'])
            self.assertIs(self.service.workers[child['id']], worker)
            stop.assert_not_awaited(); prepare.assert_not_called()
            with self.assertRaises(Problem):
                await self.service.configure(parent['id'], other['id'], auto_start=True)
            for invalid in (1, 'true', [], {}):
                with self.subTest(invalid=invalid), self.assertRaises(Problem):
                    await self.service.configure('owner', child['id'], auto_start=invalid)
        self.service.workers.clear()

    async def test_shutdown_drains_inflight_restore_and_skips_queued_members(self):
        ids = []
        for slot in range(3):
            a = self.member('member'+str(slot), slot)
            ids.append(a['id'])
            self.service.store.update(a['id'], config={**a['config'], 'auto_start': True}, run_requested=1)
        self.reopen(); entered = asyncio.Event(); release = asyncio.Event()
        async def start(_):
            entered.set(); await release.wait()
        with patch.object(self.service, '_start', start), patch.object(self.service, '_stop', AsyncMock()):
            restore = asyncio.create_task(self.service.restore_on_startup())
            await entered.wait()
            close = asyncio.create_task(self.service.close())
            await asyncio.sleep(0)
            self.assertTrue(self.service.closing)
            release.set()
            await asyncio.wait_for(asyncio.gather(restore, close), 3)
        self.reopen()
        for ident in ids:
            self.assertTrue(self.service.store.agent(ident)['run_requested'])
            self.assertNotEqual(self.service.store.agent(ident)['status'], 'error')

    async def test_restore_concurrency_is_bounded(self):
        for slot in range(5):
            a = self.member('member'+str(slot), slot)
            self.service.store.update(a['id'], config={**a['config'], 'auto_start': True}, run_requested=1)
        self.reopen(); active = peak = count = 0
        async def start(_):
            nonlocal active, peak, count
            active += 1; peak = max(peak, active); count += 1
            await asyncio.sleep(.01)
            active -= 1
        with patch.object(self.service, '_start', start):
            await self.service.restore_on_startup()
        self.assertEqual(count, 5)
        self.assertEqual(peak, 2)

    def test_legacy_database_migration_does_not_opt_anyone_in(self):
        path = self.root/'legacy.db'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE agents(id TEXT PRIMARY KEY, handle TEXT UNIQUE, parent TEXT, slot INTEGER, config TEXT, token_hash TEXT, relay_account TEXT, thread TEXT, status TEXT, error TEXT, created REAL)')
            db.execute('INSERT INTO agents VALUES(?,?,?,?,?,?,?,?,?,?,?)', ('legacy', 'legacy', None, 0, '{"instruction":"test","model":"fake","grants":[]}', 'hash', 'test', None, 'online', None, 0))
        store = Store(path)
        self.addCleanup(store.db.close)
        a = store.agent('legacy')
        self.assertFalse(a['run_requested'])
        self.assertFalse(a['config'].get('auto_start', False))
