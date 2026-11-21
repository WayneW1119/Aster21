"""Trusted state grants do not turn ordinary default-deny paths into readable ones."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_harness.experimental import windows_mxc_policy as policy
from aster.permissions import access_for


class MxcRoleGrantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root/'workspace'; self.workspace.mkdir()
        self.private = self.root/'private'; self.private.mkdir()
        self.state = self.private/'state'; self.state.mkdir()
        self.runtime = self.root/'runtime'; self.runtime.mkdir()
        # Only Windows path validation is substituted. The real compiler,
        # precedence, fixed protections and role checks run on both platforms.
        def local_path(value, *, base=None):
            path = Path(value)
            return ((base/path) if base is not None and not path.is_absolute() else path).resolve(strict=True)
        self.enterContext(patch.object(policy, 'os', SimpleNamespace(name='nt')))
        self.enterContext(patch.object(policy, '_path', side_effect=local_path))

    def test_private_state_is_available_without_global_read(self):
        result = policy.compile_filesystem(self.workspace, {'rules': []},
            private_roots=[self.private], role_grants=[(self.state, 'write')])
        self.assertEqual(set(result['readwritePaths']), {str(self.workspace), str(self.state)})
        self.assertEqual(result['readonlyPaths'], [])
        self.assertEqual(result['deniedPaths'], [str(self.private)])
        self.assertEqual(access_for(self.root/'unrelated', [
            {'path': p, 'access': a} for field,a in (
                ('readonlyPaths','read'), ('readwritePaths','write'), ('deniedPaths','deny'))
            for p in result[field]]), 'deny')

    def test_explicit_denials_and_runtime_protection_still_reject_roles(self):
        for path in (self.state, self.private):
            with self.subTest(path=path), self.assertRaises(ValueError):
                policy.compile_filesystem(self.workspace,
                    {'rules': [{'path': str(path), 'access': 'deny'}]},
                    role_grants=[(self.state, 'write')])
        with self.assertRaises(ValueError):
            policy.compile_filesystem(self.workspace, {'rules': []},
                private_roots=[self.private], role_grants=[(self.private, 'write')])
        with self.assertRaises(ValueError):
            policy.compile_filesystem(self.workspace, {'rules': []},
                runtime_roots=[self.runtime], role_grants=[(self.runtime, 'write')])
