"""Sandbox toggle preserves saved grants and the original persistent Thread."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import sys
import tomllib
import unittest
from unittest.mock import AsyncMock, patch

from aster import Aster, SessionConfiguration
from relay.store import Problem
import test_grants
from test_sandbox import ProbeClient


class SandboxToggleTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_grants.GrantTests.setUp
    member = test_grants.GrantTests.member

    async def test_unowned_disable_denied_and_legacy_default_enabled(self):
        parent = self.member()
        child = self.member('child', 1, parent['id'])
        self.service.mode = 'real'
        self.assertTrue(self.service.view(parent)['sandbox_enforced'])
        with self.assertRaises(Problem):
            await self.service.configure(parent['id'], child['id'], sandbox_enabled=False)
        with self.assertRaises(Problem):
            await self.service.create(parent['id'], 'other', 'Test', sandbox_enabled=False)
        for invalid in (0, 1, 'false', []):
            with self.subTest(value=invalid), self.assertRaises(Problem):
                await self.service.configure('owner', child['id'], sandbox_enabled=invalid)
        # Setting an already effective default does not restart an existing worker.
        with patch.object(self.service, '_stop', AsyncMock()) as stop:
            await self.service.configure('owner', parent['id'], sandbox_enabled=True)
            stop.assert_not_called()

    async def test_grants_retained_in_worker_configuration_while_disabled(self):
        record = self.member(grants=[{'path': str(self.external), 'access': 'write'}])
        self.service.mode = 'real'
        before = self.service.rules(record)
        disabled = await self.service.configure('owner', record['id'], sandbox_enabled=False)
        self.assertFalse(disabled['sandbox_enforced'])
        self.assertEqual(disabled['filesystem']['default_access'], 'unrestricted')
        self.assertEqual(disabled['config']['grants'], record['config']['grants'])
        worker = json.loads((self.service.credentials(record['id']).parent/'worker.json').read_text())
        self.assertFalse(worker['sandbox_enabled'])
        self.assertEqual(worker['rules'], before)
        self.assertNotIn('default_access', worker)
        enabled = await self.service.configure('owner', record['id'], sandbox_enabled=True)
        self.assertTrue(enabled['sandbox_enforced'])
        self.assertEqual(enabled['filesystem']['default_access'], 'deny')
        self.assertEqual(self.service.rules(self.service.store.agent(record['id'])), before)

    @unittest.skipUnless(os.name == "nt", "Windows administrator configuration")
    async def test_administrator_switch_restarts_and_keeps_prior_mxc_rules(self):
        parent = self.member()
        record = self.member("child", 1, parent["id"], grants=[{"path": str(self.external), "access": "read"}])
        self.service.mode = "real"
        ident = record["id"]
        with self.assertRaisesRegex(Problem, "只有用户"):
            await self.service.configure(parent["id"], ident, windows_admin=True)
        self.service.workers[ident] = object()
        with patch.object(self.service, "_stop", AsyncMock()), patch.object(self.service, "_start", AsyncMock()) as start:
            enabled = await self.service.configure("owner", ident, windows_admin=True)
            self.assertFalse(enabled["sandbox_enforced"])
            self.assertEqual(enabled["configuration_update"], "restart")
            start.assert_awaited_once_with(ident)
            with self.assertRaisesRegex(Problem, "只有用户"):
                await self.service.start(parent["id"], ident)
            restored = await self.service.configure("owner", ident, windows_admin=False)
            self.assertEqual(start.await_count, 2)
        self.assertTrue(restored["sandbox_enforced"])
        self.assertEqual(restored["filesystem"]["default_access"], "deny")
        self.assertEqual(restored["config"]["grants"], record["config"]["grants"])

    @unittest.skipUnless(os.name == "nt", "Windows administrator configuration")
    async def test_create_administrator_saves_member_if_elevation_is_cancelled(self):
        from squad.service import AdminLaunchProblem

        self.service.mode = "real"
        parent = self.member()
        with self.assertRaisesRegex(Problem, "只有用户"):
            await self.service.create(parent["id"], "child", "Test", windows_admin=True)

        account = {"account": {"id": "relay-fixture"}, "token": "fixture-token"}
        with patch.object(self.service, "relay", AsyncMock(return_value=account)), patch.object(
            self.service, "_start", AsyncMock(side_effect=AdminLaunchProblem("UAC cancelled", 409))
        ) as start:
            created = await self.service.create("owner", "admin", "Test", windows_admin=True)

        start.assert_awaited_once_with(created["id"])
        self.assertEqual(created["startup_warning"], "UAC cancelled")
        self.assertTrue(self.service.store.agent(created["id"])["config"]["windows_admin"])
        self.assertFalse(created["sandbox_enforced"])

    @unittest.skipUnless(os.name == "nt", "Windows administrator configuration")
    async def test_cancelled_elevation_restarts_previous_configuration(self):
        from squad.service import AdminLaunchProblem

        self.service.mode = "real"
        record = self.member()
        ident = record["id"]
        self.service.workers[ident] = object()
        attempts = []

        async def start(_):
            admin = self.service.store.agent(ident)["config"].get("windows_admin", False)
            attempts.append(admin)
            if admin:
                raise AdminLaunchProblem("UAC cancelled", 409)

        with patch.object(self.service, "_stop", AsyncMock()), patch.object(self.service, "_start", side_effect=start):
            result = await self.service.configure("owner", ident, windows_admin=True)
        self.assertEqual(attempts, [True, False])
        self.assertEqual(result["configuration_update"], "rolled_back")
        self.assertTrue(result["sandbox_enforced"])

    @unittest.skipUnless(Path('/usr/bin/bwrap').exists(), 'Linux bubblewrap required')
    async def test_outside_default_survives_same_thread_resume(self):
        service = self.service
        service.mode = 'real'
        outside = self.external / 'unlisted.txt'
        outside.write_text('outside')
        record = self.member(grants=[{'path': '/', 'access': 'read'}])
        aster = Aster(service.aster_home(record))
        code = f'from pathlib import Path; print(Path({str(outside)!r}).is_file())'
        command = shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)
        agent = aster.create(service.workspace(record), client=ProbeClient(command), name='default-probe',
            configuration=SessionConfiguration(max_consecutive_steps=None, filesystem_policy={'rules': service.rules(record)}))
        thread_id = agent.thread_id
        service.store.update(record['id'], thread=thread_id)

        for default_access in ('read', 'deny', 'read'):
            if agent is None:
                updated = await service.configure('owner', record['id'], grants=[{'path': '/', 'access': 'read'}] if default_access == 'read' else [])
                self.assertEqual(updated['filesystem']['default_access'], 'deny')
                profile = tomllib.loads((aster.home/'threads'/thread_id/'thread.toml').read_text())
                self.assertNotIn('default_access', profile['sandbox'])
                self.assertEqual(any(g['path']=='/' and g['access']=='read' for g in profile['sandbox']['rules']), default_access=='read')
                agent = aster.resume(thread_id, client=ProbeClient(command))
            async with agent.messages() as messages, agent:
                await agent.send('Check default outside access')
                output = json.loads((await asyncio.wait_for(anext(messages), 20)).text)
                self.assertEqual(output['exit_code'], 0, output)
                self.assertEqual(output['output'].strip(), str(default_access == 'read'))
            self.assertEqual(agent.thread_id, thread_id)
            agent = None

    @unittest.skipUnless(Path('/usr/bin/bwrap').exists(), 'Linux bubblewrap required')
    async def test_same_thread_on_off_on_restores_actual_read_write_boundaries(self):
        service = self.service
        service.mode = 'real'
        secret = self.external/'private.txt';secret.write_text('private-test-value')
        writable = self.external/'allowed';writable.mkdir()
        grants = [{'path': str(secret), 'access': 'deny'}, {'path': str(writable), 'access': 'write'}]
        record = self.member(grants=grants)
        aster = Aster(service.aster_home(record))
        code = f'''
import json
from pathlib import Path
result={{}}
try:Path({str(secret)!r}).read_text();result['read']=True
except OSError:result['read']=False
try:
    p=Path({str(self.external/'outside.txt')!r});p.write_text('probe');p.unlink();result['write']=True
except OSError:result['write']=False
Path({str(writable/'allowed.txt')!r}).write_text('ok')
print(json.dumps(result))
'''
        command = shlex.quote(sys.executable)+' -c '+shlex.quote(code)
        agent = aster.create(service.workspace(record), client=ProbeClient(command), name='toggle-probe',
            configuration=SessionConfiguration(max_consecutive_steps=None, filesystem_policy={'rules': service.rules(record)}))
        service.store.update(record['id'], thread=agent.thread_id)
        thread_id = agent.thread_id
        rules_before = service.rules(record)
        for enabled in (True, False, True):
            if enabled is not True or agent is None:
                await service.configure('owner', record['id'], sandbox_enabled=enabled)
                profile = tomllib.loads((aster.home/'threads'/thread_id/'thread.toml').read_text())
                self.assertEqual(profile['sandbox']['enabled'], enabled)
                self.assertEqual(profile['sandbox']['rules'], rules_before if enabled else [])
                agent = aster.resume(thread_id, client=ProbeClient(command))
            async with agent.messages() as messages, agent:
                await agent.send('Check current sandbox boundary')
                output = json.loads((await asyncio.wait_for(anext(messages), 20)).text)
                self.assertEqual(output['exit_code'], 0, output)
                result = json.loads(output['output'].strip())
                self.assertEqual(result, {'read': not enabled, 'write': not enabled})
            self.assertEqual(agent.thread_id, thread_id)
            self.assertEqual(service.store.agent(record['id'])['config']['grants'], grants)
            self.assertEqual(service.rules(service.store.agent(record['id'])), rules_before)
            agent = None
