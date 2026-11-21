"""Downstream hosts need no internal imports for these integration surfaces."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from aster.auth import ChatGPTAccount, ChatGPTAuthError
from aster.skills import builtin_directory, validate_sources
from aster.permissions import probe_windows_sandbox, smoke_windows_sandbox

class PublicHostAPITests(unittest.TestCase):
    def test_account_configuration_does_not_read_or_create_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            account = ChatGPTAccount(folder, 'example')
            self.assertEqual(account.profile, 'example')
            self.assertEqual(list(Path(folder).iterdir()), [])
            with self.assertRaises(ChatGPTAuthError):
                ChatGPTAccount(folder, '../other')

    def test_skill_resources_and_validation(self):
        self.assertTrue((builtin_directory()/'aster-harness/SKILL.md').is_file())
        sources = validate_sources([{'path': '/example', 'label': 'example'}])
        self.assertFalse(sources[0]['expose_path'])
        with self.assertRaises(ValueError):
            validate_sources([{'path': '/example', 'expose_path': 'yes'}])

    def test_native_diagnostics_delegate_without_launching_or_model_calls(self):
        with patch('agent_harness.experimental.windows_mxc_probe.diagnose', return_value={'ok': True}) as call:
            self.assertEqual(probe_windows_sandbox('executor'), {'ok': True})
            call.assert_called_once_with('executor')
        with patch('agent_harness.experimental.windows_mxc_smoke.smoke', return_value={'ok': True}) as call:
            self.assertEqual(smoke_windows_sandbox('executor'), {'ok': True})
            call.assert_called_once_with('executor')
