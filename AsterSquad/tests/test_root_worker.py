"""Owner-only root identity, saved sandbox settings, and fixed launcher scope."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
import os
import tomllib

from relay.store import Problem
from squad import root_worker
from test_grants import GrantTests


class RootWorkerTests(GrantTests):
    async def test_only_owner_can_grant_or_revoke_and_never_implicitly_inherit(self):
        parent = self.member()
        child = self.member('child', 1, parent['id'])
        self.service.mode = 'real'
        with patch('squad.root_worker.check', AsyncMock()):
            for value in (True, False):
                with self.assertRaises(Problem):
                    await self.service.configure(parent['id'], child['id'], run_as_root=value)
            for invalid in (1, 'true', []):
                with self.assertRaises(Problem):
                    await self.service.configure('owner', parent['id'], run_as_root=invalid)
            await self.service.configure('owner', parent['id'], run_as_root=True)
            self.assertFalse(self.service.store.agent(child['id'])['config'].get('run_as_root', False))
            with self.assertRaises(Problem):
                await self.service.create(parent['id'], 'other', 'Identity', run_as_root=True)

    async def test_toggle_preserves_grants_and_sandbox_preference(self):
        member = self.member(grants=[{'path':str(self.external), 'access':'write'}])
        self.service.mode = 'real'
        with patch('squad.root_worker.check', AsyncMock()), patch('squad.root_worker.release', AsyncMock()):
            root = await self.service.configure('owner', member['id'], run_as_root=True)
            self.assertFalse(root['sandbox_enforced'])
            config = json.loads((self.service.credentials(member['id']).parent/'worker.json').read_text())
            self.assertTrue(config['run_as_root'])
            self.assertFalse(config['sandbox_enabled'])
            self.assertEqual(root['config']['grants'], member['config']['grants'])
            self.assertTrue(root['config'].get('sandbox_enabled', True))
            normal = await self.service.configure('owner', member['id'], run_as_root=False)
            self.assertTrue(normal['sandbox_enforced'])
            self.assertEqual(normal['config']['grants'], member['config']['grants'])

    async def test_unavailable_root_fails_before_stopping_or_saving(self):
        member = self.member()
        self.service.mode = 'real'
        self.service.workers[member['id']] = object()
        try:
            with patch('squad.root_worker.check', AsyncMock(side_effect=Problem('Unavailable',409))), patch.object(self.service,'_stop',AsyncMock()) as stop:
                with self.assertRaises(Problem):
                    await self.service.configure('owner', member['id'], run_as_root=True)
                stop.assert_not_called()
            self.assertEqual(self.service.store.agent(member['id'])['config'], member['config'])
        finally:self.service.workers.clear()

    async def test_running_identity_change_restarts_same_record_without_live_policy(self):
        member = self.member()
        self.service.mode = 'real'
        self.service.workers[member['id']] = object()
        async def stop(ident):self.service.workers.pop(ident)
        async def start(ident):return {'ok':True}
        with patch('squad.root_worker.check',AsyncMock()), patch.object(self.service,'_stop',AsyncMock(side_effect=stop)) as stopped, patch.object(self.service,'_start',AsyncMock(side_effect=start)) as started:
            result=await self.service.configure('owner',member['id'],run_as_root=True)
        stopped.assert_awaited_once();started.assert_awaited_once()
        self.assertEqual(result['id'],member['id'])
        self.assertEqual(result['thread'],member['thread'])
        self.assertEqual(result['configuration_update'],'restart')

    async def test_unchanged_false_preserves_live_permission_update(self):
        member=self.member()
        self.service.mode='real';self.service.workers[member['id']]=object()
        try:
            with patch.object(self.service,'_stop',AsyncMock()) as stop:
                await self.service.configure('owner',member['id'],run_as_root=False)
                stop.assert_not_called()
        finally:self.service.workers.clear()

    def test_launcher_uses_database_grant_and_rejects_workspace_forgery(self):
        member=self.member()
        self.service.mode='real'
        file,_=self.service.prepare(member)
        data=json.loads(file.read_text());data['run_as_root']=True;file.write_text(json.dumps(data));file.chmod(0o600)
        settings={'uid':os.getuid(),'data_root':str(self.service.root)}
        checked,home=root_worker.validated_config(file,settings)
        self.assertFalse(checked['run_as_root'])  # A flag in worker.json alone grants nothing.
        forged=self.service.workspace(member)/'control/agents'/member['id']/'worker.json'
        forged.parent.mkdir(parents=True);forged.write_text(file.read_text());forged.chmod(0o600)
        with self.assertRaises(ValueError):root_worker.validated_config(forged,settings)
        data['home']=str(self.external);file.write_text(json.dumps(data))
        with self.assertRaises(ValueError):root_worker.validated_config(file,settings)

    async def test_demo_mode_rejects_root(self):
        member=self.member()
        with self.assertRaises(Problem):await self.service.configure('owner',member['id'],run_as_root=True)
        with self.assertRaises(Problem):await self.service.create('owner','other','Identity',run_as_root=True)
