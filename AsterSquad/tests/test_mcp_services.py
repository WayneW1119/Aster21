import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import AsyncMock, patch

import test_grants
from squad import mcp_services
from relay.store import Problem


class CatalogTests(unittest.TestCase):
    def test_host_definitions_validate_through_public_sdk_and_hide_connection_details(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'services.json'
            entry = {"name": "Web", "default": True, "server": {"url": "https://example.com/mcp", "bearer_token_env_var": "WEB_KEY"}}
            path.write_text(json.dumps({"web": entry}))
            catalog = mcp_services.load_catalog(path)
            self.assertEqual(mcp_services.public_catalog(catalog), [{"id": "web", "name": "Web", "default": True}])
            self.assertEqual(mcp_services.environment_keys({"web": catalog["web"]["server"]}), {"WEB_KEY"})
            for value in ({"relay": entry}, {"web": {**entry, "default": "yes"}},
                          {"web": {**entry, "server": {"url": "file:///private"}}},
                          {"web": {**entry, "server": {"url": "https://example.com", "bearer_token_env_var": "SQUAD_MODEL_KEY"}}}):
                path.write_text(json.dumps(value))
                with self.subTest(value=value), self.assertRaises(ValueError): mcp_services.load_catalog(path)


class ServiceCatalogTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_grants.GrantTests.setUp
    member = test_grants.GrantTests.member

    async def test_opt_in_catalog_entry_applies_without_restarting_worker(self):
        record = self.member()
        ident = record['id']
        path = self.root / 'catalog.json'
        path.write_text(json.dumps({'browser': {'name': 'Browser', 'default': False,
            'server': {'command': 'browser-mcp'}}}))
        self.service.mcp_catalog_path = path
        service = self.service

        class Input:
            def write(self, line):
                self.request = json.loads(line)

            async def drain(self):
                service.complete_permission_update(ident, self.request['id'], True, {'servers': ['browser']})

        from types import SimpleNamespace
        worker = SimpleNamespace(stdin=Input())
        service.workers[ident] = worker
        service.worker_capabilities[ident] = {'live_mcp': True}
        result = await service.configure('owner', ident, mcp_services_ids=['browser'])
        self.assertEqual(result['configuration_update'], 'live')
        self.assertEqual(result['mcp_services'], ['browser'])
        self.assertIs(service.workers[ident], worker)
        self.assertEqual(worker.stdin.request['op'], 'mcp_servers')
        self.assertEqual(set(worker.stdin.request['servers']), {'browser', 'squad', 'relay'})

    def catalog(self):
        path = self.root/'catalog.json'
        path.write_text(json.dumps({"web": {"name": "Web", "default": True, "server": {
            "url": "http://127.0.0.1:19930/mcp", "bearer_token_env_var": "TEST_WEB_TOKEN", "required": False}}}))
        self.service.mcp_catalog = mcp_services.load_catalog(path)

    async def test_optional_failure_is_visible_without_rolling_back_other_services(self):
        self.catalog()
        record = self.member()
        ident = record['id']
        self.service.save_permission_update(ident, {'id': 'change', 'kind': 'mcp',
            'status': 'pending', 'created': 1, 'previous': record['config']})
        self.service.complete_permission_update(ident, 'change', True,
            {'servers': ['squad', 'relay'], 'unavailable': ['web']})
        view = self.service.view(self.service.store.agent(ident))
        self.assertEqual(view['mcp_services'], ['web'])
        self.assertEqual(view['permission_update']['unavailable_mcp'], ['web'])
        self.assertFalse(view['permission_update']['pending'])

    async def test_rejected_mcp_change_restores_selection_and_preserves_other_permissions(self):
        self.catalog()
        record = self.member()
        ident = record['id']
        self.service.save_permission_update(ident, {'id': 'change', 'kind': 'mcp',
            'status': 'pending', 'created': 1, 'previous': record['config']})
        self.service.store.update(ident, config={**record['config'], 'mcp_services': []})
        self.service.complete_permission_update(ident, 'change', False, {'error': 'invalid config'})
        self.assertEqual(self.service.store.agent(ident)['config'], record['config'])
        self.assertEqual(self.service.view(self.service.store.agent(ident))['permission_update']['status'], 'failed')

    async def test_defaults_selection_and_delegation_preserve_unrelated_configuration(self):
        self.catalog(); parent = self.member(); child = self.member('child', 1, parent['id'])
        self.assertEqual(self.service.view(child)['mcp_services'], ['web'])
        same = await self.service.configure('owner', child['id'], mcp_services_ids=['web'])
        self.assertEqual(same['config'], child['config'])  # No unrelated restart on a path-only save.
        await self.service.configure(parent['id'], child['id'], mcp_services_ids=[])
        await self.service.configure(parent['id'], child['id'], mcp_services_ids=['web'])
        for ids in (['unknown'], ['web','web'], 'web'):
            with self.subTest(ids=ids), self.assertRaises(Problem):
                await self.service.configure('owner', child['id'], mcp_services_ids=ids)
        disabled = await self.service.configure('owner', child['id'], mcp_services_ids=[])
        self.assertEqual(disabled['mcp_services'], [])
        self.assertEqual(disabled['config']['grants'], child['config']['grants'])
        self.assertEqual(disabled['config']['model'], child['config']['model'])
        public = json.dumps(self.service.snapshot())
        self.assertNotIn('TEST_WEB_TOKEN', public)
        self.assertNotIn('19930', public)

    async def test_profile_connections_persist_and_catalog_removal_revokes_only_managed_service(self):
        self.catalog(); record = self.member(); self.service.prepare(record)
        thread = '11111111-1111-4111-8111-111111111111'
        home = self.service.aster_home(record)/'threads'/thread;home.mkdir(parents=True)
        (home/'thread.toml').write_text('[model]\nname="fake"\n')
        (home/'mcp.toml').write_text('[mcp_servers.custom]\nurl="https://custom.example/mcp"\n')
        self.service.store.update(record['id'], thread=thread)
        record = self.service.store.agent(record['id']); self.service.prepare(record)
        servers = tomllib.loads((home/'mcp.toml').read_text())['mcp_servers']
        self.assertEqual(set(servers), {'squad','relay','web','custom'})
        self.assertEqual(servers['web']['bearer_token_env_var'], 'TEST_WEB_TOKEN')
        self.service.mcp_catalog = {}; self.service.prepare(record)
        servers = tomllib.loads((home/'mcp.toml').read_text())['mcp_servers']
        self.assertEqual(set(servers), {'squad','relay','custom'})

    async def test_worker_receives_only_explicit_extra_credentials(self):
        self.catalog(); record = self.member()
        spawn = AsyncMock(side_effect=RuntimeError('capture-before-spawn'))
        with patch.dict(os.environ, {'TEST_WEB_TOKEN':'web-token','UNRELATED_SECRET':'hidden','SQUAD_API_KEY':'dummy-model'}), patch('squad.service.asyncio.create_subprocess_exec', spawn):
            with self.assertRaisesRegex(RuntimeError, 'capture-before-spawn'):
                await self.service.start('owner', record['id'])
        env = spawn.call_args.kwargs['env']
        self.assertEqual(env['TEST_WEB_TOKEN'], 'web-token')
        self.assertNotIn('UNRELATED_SECRET', env)
        self.assertEqual(env['SQUAD_MODEL_KEY'], 'dummy-model')

    async def test_child_creation_does_not_gain_parent_disabled_service(self):
        self.catalog(); parent = self.member()
        await self.service.configure('owner', parent['id'], mcp_services_ids=[])
        self.service.relay = AsyncMock(return_value={'account': {'id': 'child-relay'}, 'token': 'dummy'})
        self.service.start = AsyncMock(return_value={'ok': True})
        child = await self.service.create(parent['id'], 'child', 'Test child', 'fake')
        self.assertEqual(child['mcp_services'], [])
