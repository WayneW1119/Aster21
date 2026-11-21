"""Portable validation of desktop presets; no DPAPI, UAC, model or process calls."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from squad import windows_desktop as desktop

class DesktopConfigurationTests(unittest.TestCase):
    def test_initialize_without_provider_keys_does_not_enable_optional_services(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {k:v for k,v in os.environ.items() if k in ('HOME','USERPROFILE','HOMEDRIVE','HOMEPATH','SystemRoot','WINDIR','LOCALAPPDATA','APPDATA','PATH','TEMP','TMP')}, clear=True), patch.object(desktop, 'credentials') as credentials, patch.object(desktop, 'write') as write:
            desktop.initialize(Path(folder))
            config = write.call_args.args[1]
            self.assertFalse(config['web_enabled'])
            self.assertFalse(config['images_enabled'])
            self.assertNotIn('DEEPSEEK_API_KEY', credentials.call_args.args[1])

    def test_catalog_uses_configured_provider_and_optional_services_only(self):
        config = {'model': 'example-model', 'model_id': 'example', 'url': 'https://api.example.test/responses',
                  'api_key_env': 'EXAMPLE_KEY', 'ports': {'web': 21930}, 'web_enabled': False, 'images_enabled': False}
        with patch.object(desktop, 'credentials', return_value={'EXAMPLE_KEY': 'private-example'}), patch.object(desktop, 'write') as write:
            desktop.configure_catalogs(Path('/example'), config)
            models = write.call_args_list[0].args[1]
            self.assertEqual(models['example']['api_key_env'], 'EXAMPLE_KEY')
            self.assertNotIn('private-example', json.dumps(models))
            self.assertEqual(write.call_args_list[1].args[1], {})
        self.assertTrue(desktop.service_enabled({}, 'web'))  # Legacy preset preserves behavior.

    def test_disabled_service_packages_and_ports_are_not_required(self):
        config = {'api_key_env': 'EXAMPLE_KEY', 'web_enabled': False, 'images_enabled': False,
                  'ports': {'console': 21910, 'web': 21910, 'images': 21910}}
        def package(name):
            self.assertNotIn(name, ('aster_web', 'aster_images'))
            return object()
        with patch.object(desktop.importlib.util, 'find_spec', side_effect=package), patch.object(desktop, 'credentials', return_value={'EXAMPLE_KEY': 'example'}), patch.object(desktop.socket, 'socket', return_value=MagicMock()):
            desktop.preflight(Path('/example'), config)

    def test_optional_service_flags_are_boolean(self):
        with self.assertRaises(ValueError):
            desktop.service_enabled({'web_enabled': 'false'}, 'web')

    def test_runtime_model_key_is_accepted_without_reinitializing_credentials(self):
        config = {'model': 'model', 'model_id': 'example', 'url': 'https://api.example.test/responses',
                  'api_key_env': 'EXAMPLE_KEY', 'ports': {}, 'web_enabled': False, 'images_enabled': False}
        with patch.dict(os.environ, {'EXAMPLE_KEY': 'runtime-only'}), patch.object(desktop, 'credentials', return_value={}), patch.object(desktop, 'write') as write:
            desktop.configure_catalogs(Path('/example'), config)
            self.assertIn('example', write.call_args_list[0].args[1])
            self.assertNotIn('runtime-only', str(write.call_args_list))

    def test_chatgpt_catalog_includes_new_models_and_preserves_existing_option(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = {'chatgpt': {'home': folder}, 'ports': {},
                      'web_enabled': False, 'images_enabled': False}
            with patch.dict(os.environ, {}, clear=True), patch.object(desktop, 'credentials', return_value={}), patch('squad._windows.private_file'):
                desktop.configure_catalogs(root, config)
            models = json.loads((root / 'models.json').read_text())
            self.assertEqual(set(models), {'gpt-6.1-sol', 'gpt-6.2-astra', 'gpt-6.2-sol'})
            for name, entry in models.items():
                self.assertEqual(entry, {'name': name, 'url': 'https://api.openai.com/v1/responses',
                                        'auth': 'chatgpt', 'auth_home': str(root.resolve()),
                                        'auth_profile': 'default'})
