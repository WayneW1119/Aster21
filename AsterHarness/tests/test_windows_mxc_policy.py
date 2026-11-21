import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_harness.experimental.windows_mxc_policy import compile_filesystem


@unittest.skipUnless(os.name == "nt", "native Windows paths required")
class WindowsMxcPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "Workspace"
        self.private = self.workspace / "Private"
        self.runtime = self.root / "Runtime"
        self.private.mkdir(parents=True)
        self.runtime.mkdir()

    def test_root_rule_is_explicit_and_removing_it_restores_default_deny(self):
        closed = compile_filesystem(self.workspace, {'rules': []})
        self.assertNotIn(self.workspace.anchor, closed['readonlyPaths'])
        opened = compile_filesystem(self.workspace, {'rules': [{'path': '/', 'access': 'read'}]})
        self.assertIn(self.workspace.anchor, opened['readonlyPaths'])
        writable = compile_filesystem(self.workspace, {'rules': [{'path': '/', 'access': 'write'}]},
                                      runtime_roots=[self.runtime])
        self.assertIn(self.workspace.anchor, writable['readwritePaths'])
        self.assertIn(str(self.runtime), writable['readonlyPaths'])

    def test_protected_directory_cannot_be_reopened_by_specific_grant(self):
        child = self.private / "child"
        child.mkdir()
        rules = [
            {"path": "private/child", "access": "write"},
            {"path": str(self.runtime), "access": "write"},
        ]
        filesystem = compile_filesystem(
            self.workspace, {"rules": rules},
            private_roots=[self.private], runtime_roots=[self.runtime],
        )
        self.assertEqual(filesystem["readwritePaths"], [str(self.workspace)])
        self.assertEqual(filesystem["readonlyPaths"], [str(self.runtime)])
        self.assertEqual(filesystem["deniedPaths"], [str(self.private)])

    def test_case_alias_and_more_specific_ordinary_rule(self):
        child = self.private / "child"
        child.mkdir()
        rules = [
            {"path": "private", "access": "write"},
            {"path": "PRIVATE", "access": "deny"},
            {"path": "private/child", "access": "read"},
        ]
        filesystem = compile_filesystem(self.workspace, {"rules": rules}, cwd=child)
        self.assertEqual(filesystem["readwritePaths"], [str(self.workspace)])
        self.assertEqual(filesystem["deniedPaths"], [str(self.private)])
        self.assertIn(str(child), filesystem["readonlyPaths"])
        self.assertNotIn(self.workspace.anchor, filesystem["readonlyPaths"])
        with self.assertRaisesRegex(ValueError, "working directory"):
            compile_filesystem(self.workspace, {"rules": rules}, cwd=self.private)

    def test_rejects_paths_with_alternate_namespaces_or_streams(self):
        for value in ("../Runtime", "Private:alternate", r"\\?\C:\Windows", r"\Private", "NUL.txt"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "unambiguous"):
                compile_filesystem(self.workspace, {
                    "rules": [{"path": value, "access": "read"}],
                })
