"""Cross-volume root semantics can be checked without native Windows."""
import os
from pathlib import PureWindowsPath
import unittest
from unittest.mock import patch
from aster.permissions import access_for, covers

class WindowsRootPolicyTests(unittest.TestCase):
    def require_profile_runtime(self):
        if os.name == 'nt' and not os.environ.get('ASTER_MXC_EXECUTOR'):
            executor = os.environ.get('ASTER_MXC_TEST_EXECUTOR')
            if not executor:
                self.skipTest('Windows MXC executor required')
            self.enterContext(patch.dict(os.environ, {'ASTER_MXC_EXECUTOR': executor}))

    def test_virtual_root_covers_drives_but_drive_and_deeper_rules_override(self):
        rules = [{'path': '/', 'access': 'read'}, {'path': 'C:\\', 'access': 'deny'},
                 {'path': 'C:\\project', 'access': 'write'}]
        with patch('agent_harness.core.sandbox.policy.Path', PureWindowsPath):
            self.assertEqual(access_for('D:\\data', rules), 'read')
            self.assertEqual(access_for('C:\\private', rules), 'deny')
            self.assertEqual(access_for('C:\\project\\file', rules), 'write')
            self.assertFalse(covers(rules, [{'path': '/', 'access': 'write'}]))
            self.assertTrue(covers(rules, [{'path': 'C:\\project', 'access': 'write'}]))
            self.assertEqual(access_for('D:\\data', []), 'deny')

    def test_legacy_default_is_migrated_once_to_explicit_root_rules(self):
        from agent_harness.core.sandbox import parse_policy
        closed = parse_policy({'default_access': 'deny'}, legacy=True)
        opened = parse_policy({'default_access': 'read'}, legacy=True)
        self.assertEqual(closed['rules'], [])
        self.assertEqual(opened['rules'], [{'path': '/', 'access': 'read'}])
        self.assertNotIn('default_access', closed)
        self.assertEqual(parse_policy({'rules': [], 'format': 2}, legacy=True)['rules'], [])
        with self.assertRaises(ValueError):
            parse_policy({'default_access': 'deny', 'format': 2}, legacy=True)

    def test_legacy_profile_saves_only_unified_rules(self):
        self.require_profile_runtime()
        import tempfile
        from pathlib import Path
        from agent_harness.agent_config import default_profile, load_profile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = default_profile(root, root)
            data['sandbox'].pop('format')
            data['sandbox']['default_access'] = 'deny'
            profile = load_profile(root/'agent.toml', data=data, instruction_text='test')
            saved = profile.snapshot()['sandbox']
            self.assertEqual(saved['format'], 2)
            self.assertEqual(saved['rules'], [])
            self.assertNotIn('default_access', saved)

    def test_root_rule_survives_profile_round_trip_as_cross_volume_symbol(self):
        self.require_profile_runtime()
        import tempfile
        from pathlib import Path
        from agent_harness.agent_config import default_profile, load_profile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = default_profile(root, root)
            data['sandbox']['rules'] = [{'path': '/', 'access': 'read'}]
            profile = load_profile(root/'agent.toml', data=data, instruction_text='test')
            self.assertEqual(profile.snapshot()['sandbox']['rules'], [{'path': '/', 'access': 'read'}])
