"""Real mount exceptions, immutable host protections and policy inclusion."""
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aster.permissions import access_for, covers
from agent_harness.core.sandbox import FilesystemSandbox, parse_policy
from agent_harness.core.sandbox.launcher import pin_path


class PolicyPrecedenceTests(unittest.TestCase):
    def test_specificity_precedes_mode_and_equal_paths_use_deny_write_read(self):
        rules=[{'path':'/project','access':'deny'},{'path':'/project/app','access':'write'},{'path':'/project/app/reference','access':'read'}]
        for ordered in itertools.permutations(rules):
            self.assertEqual(access_for('/project/private/key',ordered),'deny')
            self.assertEqual(access_for('/project/app/main.py',ordered),'write')
            self.assertEqual(access_for('/project/app/reference/file',ordered),'read')
        for ordered in itertools.permutations(['read','write','deny']):
            policy=parse_policy({'rules':[{'path':'/same','access':a} for a in ordered]})
            self.assertEqual(policy['rules'],[{'path':'/same','access':'deny'}])

    def test_whole_policy_inclusion_preserves_nested_restrictions_and_exceptions(self):
        parent=[{'path':'/project','access':'deny'},{'path':'/project/app','access':'write'},{'path':'/project/app/private','access':'deny'},{'path':'/project/app/private/public','access':'read'}]
        self.assertTrue(covers(parent,parent))
        self.assertTrue(covers(parent,[{'path':'/project/app/private/public','access':'read'}]))
        self.assertFalse(covers(parent,[{'path':'/project/app','access':'write'}]))
        self.assertFalse(covers(parent,[{'path':'/project/app/private/public','access':'write'}]))
        self.assertFalse(covers(parent,[{'path':'/project2','access':'read'}]))


@unittest.skipUnless(sys.platform.startswith('linux') and Path('/usr/bin/bwrap').is_file(),'Linux sandbox required')
class MountPrecedenceTests(unittest.TestCase):
    def test_reopened_subtrees_cannot_read_peers_and_nested_rules_apply_to_real_commands_and_file_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);work=root/'work';work.mkdir();project=root/'project';app=project/'app';app.mkdir(parents=True)
            private=app/'private';private.mkdir();public=private/'public';public.mkdir();(public/'read.txt').write_text('readable')
            (project/'secret.txt').write_text('TOP_SECRET');(private/'secret.txt').write_text('PRIVATE')
            (app/'escape').symlink_to(project/'secret.txt')
            rules=[{'path':str(project),'access':'deny'},{'path':str(app),'access':'write'},{'path':str(private),'access':'deny'},{'path':str(public),'access':'read'}]
            sandbox=FilesystemSandbox(work,{'rules':list(reversed(rules))})
            script=f'''
from pathlib import Path
app=Path({str(app)!r}); public=Path({str(public)!r})
(app/'ok').write_text('ok')
assert (public/'read.txt').read_text()=='readable'
for action in [lambda:Path({str(project/'secret.txt')!r}).read_text(),lambda:Path({str(private/'secret.txt')!r}).read_text(),lambda:(app/'escape').read_text(),lambda:(public/'bad').write_text('bad'),lambda:list(Path({str(project)!r}).iterdir())]:
    try:action()
    except OSError:pass
    else:raise RuntimeError('Unexpected access')
print('scoped-access-ok')
'''
            command,env=sandbox.command([sys.executable,'-c',script])
            result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr);self.assertIn('scoped-access-ok',result.stdout)
            self.assertEqual(sandbox.file_operation('read',path=str(public/'read.txt'),start_line=1,end_line=2),'1: readable')
            with self.assertRaises(ValueError):sandbox.file_operation('read',path=str(project/'secret.txt'),start_line=1,end_line=2)
            self.assertFalse((public/'bad').exists())

    def test_protected_roots_stay_hidden_and_runtime_write_ceiling_survives_specific_rules(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);work=root/'work';work.mkdir();secret=root/'host-secret';secret.mkdir();(secret/'token').write_text('never')
            with self.assertRaises(ValueError):FilesystemSandbox(work,{'rules':[{'path':str(secret/'token'),'access':'write'}]},private_roots=[secret])
            sandbox=FilesystemSandbox(work,{'rules':[{'path':str(secret/'token'),'access':'read'}]},private_roots=[secret])
            command,env=sandbox.command(['/bin/cat',str(secret/'token')]);result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=15)
            self.assertNotEqual(result.returncode,0);self.assertNotIn('never',result.stdout)
            # A venv is wholly trusted; base installs expose only runtime subtrees.
            prefix=Path(sys.prefix).resolve()
            if prefix == Path(sys.base_prefix).resolve():
                prefix=Path(sys._base_executable).resolve().parent
            probe=prefix/'policy-test-no-write'
            sandbox=FilesystemSandbox(work,{'rules':[{'path':str(prefix),'access':'write'}]})
            command,env=sandbox.command([sys.executable,'-c',f'from pathlib import Path;Path({str(probe)!r}).write_text("bad")'])
            result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=15)
            self.assertNotEqual(result.returncode,0);self.assertFalse(probe.exists())

    def test_root_deny_preserves_explicit_runtime_roots_and_synthetic_views(self):
        with tempfile.TemporaryDirectory() as folder:
            work=Path(folder)
            sandbox=FilesystemSandbox(work,{'rules':[{'path':'/','access':'deny'},{'path':'/usr','access':'read'}]})
            command,env=sandbox.command(['/bin/true'])
            result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            command,env=sandbox.command(['/usr/bin/cat','/etc/passwd'])
            result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=15)
            self.assertNotEqual(result.returncode,0)

    def test_source_descriptor_walk_rejects_aliases_and_pins_original_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);actual=root/'actual';actual.mkdir();(actual/'data').write_text('ok');alias=root/'alias';alias.symlink_to(actual,target_is_directory=True)
            with self.assertRaises(OSError):pin_path(alias/'data')
            with self.assertRaises(ValueError):pin_path(alias)
            fd=pin_path(actual/'data')
            try:self.assertEqual(Path(f'/proc/self/fd/{fd}').read_text(),'ok')
            finally:os.close(fd)
