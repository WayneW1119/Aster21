"""Codex-style HTTP configuration and real Streamable HTTP protocol regression."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from agent_harness.core.client import ScriptedClient
from agent_harness.core.mcp.client import StreamableHttpMcpClient, McpCallError
from agent_harness.core.mcp.config import McpServerConfig, load_mcp_config
from agent_harness.core.mcp.connection_manager import McpConnectionManager
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.session.step import build_prompt
from agent_harness.core.thread_manager import ThreadManager

FIXTURE = Path(__file__).parent / 'fixtures/mcp_http_server.py'
STDIO_FIXTURE = Path(__file__).parent / 'fixtures/mcp_server.py'
SDK_AVAILABLE = bool(importlib.util.find_spec('mcp'))


def call(name, identifier, namespace=None, **arguments):
    item = {'type': 'function_call', 'name': name, 'call_id': identifier,
            'arguments': json.dumps(arguments)}
    if namespace:
        item['namespace'] = namespace
    return item


def search(identifier='search'):
    return {'type': 'tool_search_call', 'call_id': identifier, 'execution': 'client',
            'arguments': {'query': 'mcp__remote add integers', 'limit': 1}}


class HttpConfigTests(unittest.TestCase):
    def test_codex_url_headers_toml_and_stdio_storage_compatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'mcp.toml'
            path.write_text('''[mcp_servers.remote]
url = "https://example.test/mcp"
bearer_token_env_var = "TEST_MCP_TOKEN"
http_headers = { "X-Tenant" = "test" }
env_http_headers = { "X-API-Key" = "TEST_MCP_KEY" }
''')
            config = load_mcp_config(path)['remote']
            self.assertEqual(config['url'], 'https://example.test/mcp')
            self.assertNotIn('command', config)
            self.assertEqual(config['env_http_headers'], {'X-API-Key': 'TEST_MCP_KEY'})
        stdio = McpServerConfig('python3', args=('server.py',)).to_dict()
        self.assertNotIn('url', stdio)
        self.assertNotIn('http_headers', stdio)
        self.assertEqual(McpServerConfig(**stdio).transport, 'stdio')
        self.assertEqual(McpServerConfig(**config).transport, 'streamable_http')

    def test_invalid_transport_urls_headers_and_conflicting_auth_are_rejected(self):
        for fields in ({}, {'command': 'python', 'url': 'https://example.test/mcp'},
                       {'url': 'ftp://example.test/mcp'}, {'url': 'https://user:secret@example.test/mcp'},
                       {'url': 'https://example.test/#fragment'}, {'url': 'https://example.test:bad/mcp'},
                       {'url': 'https://example.test/mcp\n'}, {'url': 'https://example.test/mcp', 'args': ['x']},
                       {'command': 'python', 'http_headers': {'X-Key': 'x'}},
                       {'url': 'https://example.test/mcp', 'bearer_token_env_var': 'A=B'},
                       {'url': 'https://example.test/mcp', 'http_headers': {'X-Key': 'x\r\ninject'}},
                       {'url': 'https://example.test/mcp', 'http_headers': {'bad name': 'x'}},
                       {'url': 'https://example.test/mcp', 'http_headers': {'X-Key': 'x', 'x-key': 'y'}},
                       {'url': 'https://example.test/mcp', 'http_headers': {'MCP-Session-Id': 'fake'}},
                       {'url': 'https://example.test/mcp', 'bearer_token_env_var': 'T',
                        'env_http_headers': {'Authorization': 'OTHER'}}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                McpServerConfig(**fields)

    def test_environment_headers_override_static_without_persisting_values(self):
        static = {'X-Key': 'default'}
        config = McpServerConfig(url='https://example.test/mcp', http_headers=static,
            env_http_headers={'x-key': 'TEST_MCP_KEY'}, bearer_token_env_var='TEST_MCP_TOKEN')
        static['X-Key'] = 'caller changed'
        with patch.dict(os.environ, {'TEST_MCP_KEY': 'private-header', 'TEST_MCP_TOKEN': 'private-token'}):
            headers = config.resolve_http_headers()
        self.assertEqual(headers, {'x-key': 'private-header', 'authorization': 'Bearer private-token'})
        saved = json.dumps(config.to_dict())
        self.assertNotIn('private-token', saved)
        self.assertNotIn('private-header', saved)
        self.assertEqual(config.http_headers['X-Key'], 'default')
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'not set or empty'):
            config.resolve_http_headers()
        with patch.dict(os.environ, {'TEST_MCP_KEY': 'bad\nvalue', 'TEST_MCP_TOKEN': 'token'}):
            with self.assertRaisesRegex(ValueError, 'printable ASCII'):
                config.resolve_http_headers()


@unittest.skipUnless(SDK_AVAILABLE, 'Install .[mcp] for real HTTP integration')
class HttpIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token, self.key = secrets.token_hex(24), secrets.token_hex(24)
        self.env = patch.dict(os.environ, {'ASTER_HTTP_TEST_TOKEN': self.token, 'ASTER_HTTP_TEST_KEY': self.key})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.events = []
        self.url, self.server_root, _ = await self.start_endpoint('server')

    async def start_endpoint(self, name, json_response=False, authenticated=True):
        root = self.root / name
        root.mkdir()
        log = (root / 'server.log').open('w')
        command = [sys.executable, str(FIXTURE.resolve()), '--state-dir', str(root)]
        if json_response:
            command.append('--json-response')
        environment = {**os.environ, 'HTTP_FIXTURE_TOKEN': self.token if authenticated else '',
                       'HTTP_FIXTURE_KEY': self.key if authenticated else ''}
        process = subprocess.Popen(command, env=environment, stdout=log, stderr=log)
        self.addAsyncCleanup(self.stop_endpoint, process, log)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail('HTTP fixture exited: ' + (root / 'server.log').read_text())
            try:
                data = json.loads((root / 'server.json').read_text())
                def health():
                    with urlopen(data['origin'] + '/health', timeout=.2) as response:
                        return response.status == 200
                if await asyncio.to_thread(health):
                    return data['url'], root, process
            except (OSError, ValueError):
                pass
            await asyncio.sleep(.02)
        self.fail('HTTP fixture startup timed out')

    async def stop_endpoint(self, process, log):
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 4)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, 4)
        log.close()

    def config(self, **changes):
        return McpServerConfig(url=self.url, bearer_token_env_var='ASTER_HTTP_TEST_TOKEN',
            http_headers={'X-Tenant': 'test'}, env_http_headers={'X-API-Key': 'ASTER_HTTP_TEST_KEY'},
            **changes)

    def effects(self, root=None):
        path = (root or self.server_root) / 'effects.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def trace(self, event, data):
        self.events.append((event, data))

    async def test_sse_search_call_resources_and_session_delete(self):
        client = ScriptedClient([[search()], [call('add', 'add', 'mcp__remote', a=2, b=5)],
            [call('list_mcp_resources', 'resources'), call('list_mcp_resource_templates', 'templates'),
             call('read_mcp_resource', 'guide', server='remote', uri='fixture://guide')],
            [call('wait_for_event', 'wait')]])
        manager = ThreadManager(self.root / 'threads')
        session = manager.create(client, self.root, trace=self.trace,
            configuration=SessionConfiguration(mcp_servers={'remote': self.config().to_dict()}))
        self.addAsyncCleanup(session.close)
        await session.post_user_message('Search and call the remote fixture, then read its resources.')
        await asyncio.wait_for(session.run_until_idle(), 15)
        outputs = {i['call_id']: i['output'] for i in session.state.history.snapshot()
                   if i['type'] == 'function_call_output'}
        self.assertIn('7', outputs['add'])
        self.assertIn('fixture://guide', outputs['resources'])
        self.assertIn('fixture://items/', outputs['templates'])
        self.assertIn('Streamable HTTP', outputs['guide'])
        self.assertIn('mcp__remote', session.mcp_projection.current['namespaces'])
        remote = dict(session.services.mcp.binding.servers)['remote']
        self.assertIsInstance(remote, StreamableHttpMcpClient)
        self.assertIsNone(remote.stderr_path)
        await session.suspend()
        rows = self.effects()
        self.assertTrue(any(r.get('content_type', '').startswith('text/event-stream') for r in rows))
        self.assertTrue(any(r.get('method') == 'DELETE' and r.get('authorized') for r in rows))
        self.assertEqual(sum(r['kind'] == 'add' for r in rows), 1)
        saved = session.rollout.path.read_text() + json.dumps(self.events)
        self.assertNotIn(self.token, saved)
        self.assertNotIn(self.key, saved)
        self.assertIn('ASTER_HTTP_TEST_TOKEN', saved)

    async def test_json_response_and_static_authorization_header(self):
        url, root, _ = await self.start_endpoint('json-server', json_response=True)
        config = McpServerConfig(url=url, http_headers={
            'Authorization': 'Bearer ' + self.token, 'X-API-Key': self.key})
        client = StreamableHttpMcpClient(config, self.root)
        self.addAsyncCleanup(client.close)
        tools = await client.start()
        self.assertIn('add', [t['name'] for t in tools])
        result = await client.call_tool('add', {'a': 10, 'b': 6})
        self.assertIn('16', json.dumps(result))
        await client.close()
        self.assertTrue(any(r.get('content_type', '').startswith('application/json') for r in self.effects(root)))

    async def test_auth_failures_are_clear_no_secrets_or_accidental_anonymous_fallback(self):
        config = McpServerConfig(url=self.url, bearer_token_env_var='ASTER_HTTP_MISSING')
        client = StreamableHttpMcpClient(config, self.root)
        self.addAsyncCleanup(client.close)
        with self.assertRaisesRegex(RuntimeError, 'ASTER_HTTP_MISSING'):
            await client.start()
        self.assertEqual(self.effects(), [])
        bad = self.config().to_dict()
        bad['bearer_token_env_var'] = 'ASTER_HTTP_WRONG'
        with patch.dict(os.environ, {'ASTER_HTTP_WRONG': 'incorrect-token'}):
            manager = McpConnectionManager({'remote': bad}, self.root, self.root / 'logs', self.trace)
            self.addAsyncCleanup(manager.close)
            binding = await manager.start()
        self.assertEqual(binding.servers, ())
        self.assertIn('401', manager.failures[0]['error'])
        self.assertNotIn(self.token, json.dumps(self.events))
        await manager.replace_servers({'remote': self.config().to_dict()})
        self.assertEqual(len(manager.binding.servers), 1)

    async def test_redirect_is_not_followed_and_missing_credentials_are_not_echoed(self):
        config = McpServerConfig(url=self.url.replace('/mcp', '/redirect'),
            bearer_token_env_var='ASTER_HTTP_TEST_TOKEN',
            env_http_headers={'X-API-Key': 'ASTER_HTTP_TEST_KEY'})
        client = StreamableHttpMcpClient(config, self.root)
        self.addAsyncCleanup(client.close)
        with self.assertRaisesRegex(RuntimeError, '307'):
            await client.start()
        paths = [r['path'] for r in self.effects() if r['kind'] == 'request']
        self.assertEqual(paths, ['/redirect'])
        message = client._describe_error(RuntimeError('x' * 990 + 'Bearer ' + self.token + self.key))
        self.assertNotIn(self.token[:10], message)
        self.assertNotIn(self.key[:10], message)

    async def test_timeout_has_unknown_outcome_and_does_not_replay_business_call(self):
        client = StreamableHttpMcpClient(self.config(tool_timeout_sec=.15), self.root)
        self.addAsyncCleanup(client.close)
        await client.start()
        with self.assertRaises(McpCallError) as raised:
            await client.call_tool('slow', {'seconds': .7})
        self.assertTrue(raised.exception.outcome_unknown)
        await asyncio.sleep(.1)
        result = await client.call_tool('add', {'a': 1, 'b': 1})
        self.assertIn('2', json.dumps(result))
        self.assertEqual(sum(r['kind'] == 'slow' for r in self.effects()), 1)

    async def test_mixed_stdio_http_reuse_failed_replacement_and_retire_only_http_session(self):
        local = {'command': sys.executable, 'args': [str(STDIO_FIXTURE.resolve()), '--state-dir', str(self.root / 'stdio')]}
        remote = self.config().to_dict()
        manager = McpConnectionManager({'local': local, 'remote': remote}, self.root, self.root / 'logs', self.trace)
        self.addAsyncCleanup(manager.close)
        original = await manager.start()
        clients = dict(original.servers)
        await manager.replace_servers({'local': local, 'remote': remote})
        self.assertIs(dict(manager.binding.servers)['remote'], clients['remote'])
        bad = {**remote, 'url': self.url.replace('/mcp', '/missing'), 'required': True}
        with self.assertRaisesRegex(RuntimeError, 'Required MCP servers'):
            await manager.replace_servers({'local': local, 'remote': bad})
        self.assertIs(dict(manager.binding.servers)['remote'], clients['remote'])
        await manager.replace_servers({'local': local})
        self.assertTrue(clients['remote']._task.done())
        self.assertFalse(clients['local']._task.done())
        self.assertTrue(any(r.get('method') == 'DELETE' for r in self.effects()))
        result = await clients['local'].request('resources/read', {'uri': 'fixture://guide'})
        self.assertIn('fixture://guide', json.dumps(result))

    async def test_suite_and_agent_connections_same_endpoint_survive_resume(self):
        from agent_harness import Harness
        url, root, _ = await self.start_endpoint("accounts", authenticated=False)
        (root / "expected-token.txt").write_text(self.token)
        workspace = self.root / "workspace"
        workspace.mkdir()
        credential = workspace / "account.json"
        credential.write_text(json.dumps({"mcp_url": url, "token": self.token}))
        host = Harness(self.root / "home")
        agent = host.create(workspace, client=ScriptedClient([]),
                            configuration=SessionConfiguration(filesystem_policy=None))
        self.addAsyncCleanup(agent.close)
        await agent.install_suite({"id": "base", "mcp": {"primary": {
            "url": url, "bearer_token_env_var": "ASTER_HTTP_TEST_TOKEN", "required": True}}})
        await agent.configure_capabilities(allow_connect=True)
        await agent.start()
        cap = agent._session.capabilities
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            await cap.connect("primary", url)
        with self.assertRaisesRegex(ValueError, "agent-owned"):
            await cap.disconnect("primary")
        await cap.connect("second", url, str(credential))
        self.assertEqual(agent.mcp_status["servers"], ["primary", "second"])
        with self.assertRaises(RuntimeError):
            await cap.connect("wrong", url.replace("/mcp", "/redirect"), str(credential))
        self.assertEqual(agent.mcp_status["servers"], ["primary", "second"])
        await agent.set_suite_enabled("base", False)
        self.assertEqual(agent.mcp_status["servers"], ["second"])
        await agent.close()
        for path in (cap.path, agent.thread_path / "rollout.jsonl"):
            self.assertNotIn(self.token, path.read_text())
        agent = host.resume(agent.thread_id, client=ScriptedClient([]))
        self.addAsyncCleanup(agent.close)
        await agent.start()
        self.assertEqual(agent.mcp_status["servers"], ["second"])
        session = agent._session
        from agent_harness.core.session.step_context import StepContext
        context = StepContext.capture(session)
        async with session.services.mcp.retain_batch_clients():
            receipt = await context.tool_router.dispatch_async({"type": "function_call", "name": "disconnect_mcp",
                "call_id": "drop", "arguments": json.dumps({"name": "second"})})
            self.assertIn("disconnected", receipt["output"])
            old_client = dict(context.mcp.servers)["second"]
            self.assertIn("5", json.dumps(await old_client.call_tool("add", {"a": 2, "b": 3})))
        self.assertTrue(old_client._task.done())
        self.assertEqual(agent.mcp_status["servers"], [])

    async def test_public_endpoint_without_authentication(self):
        url, _, _ = await self.start_endpoint('public', authenticated=False)
        client = StreamableHttpMcpClient(McpServerConfig(url=url), self.root)
        self.addAsyncCleanup(client.close)
        await client.start()
        result = await client.call_tool('add', {'a': 4, 'b': 5})
        self.assertIn('9', json.dumps(result))

    async def test_startup_timeout_is_bounded(self):
        client = StreamableHttpMcpClient(self.config(startup_timeout_sec=.0001), self.root)
        self.addAsyncCleanup(client.close)
        with self.assertRaisesRegex(RuntimeError, 'Timeout'):
            await asyncio.wait_for(client.start(), 3)
        await asyncio.wait_for(client.close(), 3)
        self.assertTrue(client._task.done())

    async def test_close_during_call_fails_pending_result_without_replaying(self):
        client = StreamableHttpMcpClient(self.config(), self.root)
        self.addAsyncCleanup(client.close)
        await client.start()
        pending = asyncio.create_task(client.call_tool('slow', {'seconds': 2}))
        deadline = time.monotonic() + 2
        while not any(r['kind'] == 'slow' for r in self.effects()) and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        await asyncio.wait_for(client.close(), 3)
        with self.assertRaises(McpCallError) as raised:
            await pending
        self.assertTrue(raised.exception.outcome_unknown)
        self.assertTrue(client._task.done())
        self.assertEqual(sum(r['kind'] == 'slow' for r in self.effects()), 1)

    async def test_resume_rereads_credentials_and_compacted_definitions_stay_unloaded(self):
        manager = ThreadManager(self.root / 'threads')
        first = manager.create(ScriptedClient([[search()], [call('wait_for_event', 'wait')]]), self.root,
            configuration=SessionConfiguration(mcp_servers={'remote': self.config().to_dict()}))
        self.addAsyncCleanup(first.close)
        await first.post_user_message('Discover a remote tool.')
        await first.run_until_idle()
        self.assertTrue(first.mcp_projection.loaded_definitions())
        history = first.state.history
        first._compact_boundary = set(history.ids())
        first.compact_history(history.ids()[0], history.ids()[-1], history.generation, 'The remote source remains available.')
        await first.suspend()
        rotated = secrets.token_hex(24)
        (self.server_root / 'expected-token.txt').write_text(rotated)
        with patch.dict(os.environ, {'ASTER_HTTP_TEST_TOKEN': rotated}):
            resumed = manager.resume(ScriptedClient([[search('rediscover')],
                [call('add', 'after', 'mcp__remote', a=3, b=8)], [call('wait_for_event', 'wait_again')]]), first.thread_id)
            self.addAsyncCleanup(resumed.close)
            self.assertFalse(resumed.mcp_projection.loaded_definitions())
            await resumed.post_user_message('Rediscover and use the remote tool.')
            await resumed.run_until_idle()
            await resumed.suspend()
        rows = self.effects()
        self.assertEqual(sum(r['kind'] == 'add' for r in rows), 1)
        ids = {r['session_id'] for r in rows if r.get('method') == 'DELETE' and r.get('session_id')}
        self.assertEqual(len(ids), 2)
        saved = resumed.rollout.path.read_text()
        self.assertNotIn(rotated, saved)
        self.assertNotIn(self.token, saved)
        self.assertEqual(resumed.configuration.mcp_servers['remote']['bearer_token_env_var'], 'ASTER_HTTP_TEST_TOKEN')
