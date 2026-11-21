"""Multiple provider routes and strict separation of host credentials."""
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest

from squad.models import load_catalog, worker_model
from squad.service import Service
from relay.server import secret_file


class ModelCatalogTests(unittest.TestCase):
    def test_provider_failure_only_exposes_allowlisted_messages(self):
        from squad.worker import provider_failure
        failure = {'type':'error','error':{'code':'subscription_sharing_usage_limit_exceeded','message':'private upstream details'}}
        self.assertIn('额度已用尽', provider_failure('api_stream_event',failure))
        self.assertNotIn('private',provider_failure('api_stream_event',failure))
        self.assertIsNone(provider_failure('api_request',failure))
        self.assertIsNone(provider_failure('api_stream_event',{'type':'error','error':{'code':'unknown','message':'secret'}}))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.auth = self.root / 'auth'
        (self.auth / 'credentials').mkdir(parents=True)
        self.catalog = {
            'deepseek': {'name': 'deepseek-flash', 'url': 'https://api.deepseek.com/responses',
                         'api_key_env': 'TEST_DEEPSEEK_KEY'},
            'gpt': {'name': 'gpt-6.1-sol', 'url': 'https://api.openai.com/v1/responses',
                    'auth': 'chatgpt', 'auth_home': str(self.auth), 'auth_profile': 'default'},
        }
        self.path = self.root / 'catalog.json'

    def load(self, value=None):
        self.path.write_text(json.dumps(self.catalog if value is None else value))
        return load_catalog(self.path)

    def test_selects_independent_endpoints_and_auth_without_secret_values(self):
        routes = self.load()
        self.assertEqual(worker_model(routes['deepseek'])['api_key_env'], 'SQUAD_MODEL_KEY')
        self.assertEqual(worker_model(routes['gpt'])['auth'], 'chatgpt')
        self.assertNotIn('api_key_env', worker_model(routes['gpt']))
        self.assertNotEqual(routes['deepseek']['url'], routes['gpt']['url'])

    def test_rejects_inline_secrets_insecure_urls_and_auth_fallback(self):
        from copy import deepcopy
        for model, key, value in [('deepseek','api_key','secret'),
                ('deepseek','url','http://example.test/responses'),
                ('deepseek','url','https://user:secret@example.test/responses'),
                ('gpt','url','https://other.example/responses'),
                ('gpt','api_key_env','OTHER_KEY')]:
            candidate = deepcopy(self.catalog)
            candidate[model][key] = value
            with self.subTest(key=key,model=model), self.assertRaises(ValueError):
                self.load(candidate)

    def test_preparation_and_switch_replace_auth_and_protect_host_credentials(self):
        routes = self.load()
        service = Service(self.root/'squad', mode='real', relay_url='http://127.0.0.1:1',
            relay_mcp='http://127.0.0.1:2/mcp', squad_mcp='http://127.0.0.1:3/mcp',
            owner_relay_token='owner', domain='domain', user_account='owner',
            models=list(routes), model_url='https://unused.test/responses', model_catalog=routes)
        self.addCleanup(service.store.db.close)
        record, _ = service.store.create('alice', None, 0,
            {'instruction':'Test', 'model':'gpt', 'grants':[]}, 'relay-id')
        secret_file(service.credentials(record['id']), json.dumps({'relay':'relay-test', 'squad':'squad-test'}))
        file,_ = service.prepare(record)
        config=json.loads(file.read_text())
        self.assertEqual(config['model_config']['auth'],'chatgpt')
        self.assertIn({'path':str(self.auth/'credentials'),'access':'deny'}, config['rules'])
        self.assertNotIn('SQUAD_MODEL_KEY',json.dumps(config['model_config']))
        # The public profile is replaced, so switching away cannot retain OAuth mode.
        thread='11111111-1111-4111-8111-111111111111'
        folder=service.aster_home(record)/'threads'/thread
        folder.mkdir(parents=True)
        secret_file(folder/'thread.toml', '[model]\nname="gpt-6.1-sol"\nauth="chatgpt"\nauth_home="/old"\n')
        service.store.update(record['id'], thread=thread,
            config={**record['config'],'model':'deepseek'})
        service.prepare(service.store.agent(record['id']))
        saved=tomllib.loads((folder/'thread.toml').read_text())['model']
        self.assertEqual(saved['auth'],'api_key')
        self.assertEqual(saved['url'],'https://api.deepseek.com/responses')
        self.assertNotIn('auth_home',saved)
