"""Explicit root grants, default-deny execution and historical compatibility."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_harness.core.sandbox import FilesystemSandbox, parse_policy
from agent_harness.core.sandbox.policy import access_for


class ExplicitPathPolicyTests(unittest.TestCase):
    def test_legacy_is_normalized_once_and_new_empty_policy_stays_private(self):
        legacy = parse_policy({'rules': []}, legacy=True)
        self.assertEqual(access_for('/unlisted', legacy['rules']), 'read')
        self.assertEqual(parse_policy(legacy, legacy=True), legacy)
        self.assertEqual(access_for('/unlisted', parse_policy({})['rules']), 'deny')
        self.assertEqual(access_for('/unlisted', []), 'deny')
        full = {'rules': [{'path': '/path-'+str(i), 'access': 'read'} for i in range(64)]}
        migrated = parse_policy(full, legacy=True)
        self.assertEqual(parse_policy(migrated), migrated)

    @unittest.skipUnless(Path('/usr/bin/bwrap').exists(), 'Linux bubblewrap required')
    def test_root_read_can_be_removed_without_losing_workspace_or_dns(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root/'workspace';workspace.mkdir()
            reference = root/'reference.txt';reference.write_text('reference')
            hidden = root/'hidden.txt';hidden.write_text('hidden')
            for root_access in ('read', None, 'deny'):
                rules = [{'path': str(reference), 'access': 'read'}]
                if root_access is not None:
                    rules.insert(0, {'path': '/', 'access': root_access})
                sandbox = FilesystemSandbox(workspace, {'rules': rules})
                code = '''
import json, pathlib, sys
reference, hidden = map(pathlib.Path, sys.argv[1:])
pathlib.Path('created.txt').write_text('workspace')
try: hidden.read_text(); readable=True
except OSError: readable=False
print(json.dumps([reference.read_text(), readable, bool(pathlib.Path('/etc/resolv.conf').read_text())]))
'''
                argv, env = sandbox.command([sys.executable, '-c', code, str(reference), str(hidden)])
                result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), ['reference', root_access == 'read', True])
                self.assertEqual((workspace/'created.txt').read_text(), 'workspace')
