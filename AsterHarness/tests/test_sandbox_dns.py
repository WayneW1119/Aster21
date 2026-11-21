"""Resolver symlinks survive hidden host bridges without exposing siblings."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_harness.core.sandbox as sandbox_module
from agent_harness.core.sandbox import FilesystemSandbox


@unittest.skipUnless(sys.platform.startswith('linux') and Path('/usr/bin/bwrap').is_file(), 'Linux sandbox required')
class SandboxDNSTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='aster-dns-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / 'work'; self.work.mkdir()
        self.bridge = self.root / 'bridge'; self.bridge.mkdir()
        self.source = self.bridge / 'network' / 'resolv.conf'
        self.source.parent.mkdir()
        self.source.write_text('nameserver 192.0.2.53\n')
        self.secret = self.source.parent / 'unrelated'; self.secret.write_text('hidden')
        self.alias = self.root / 'etc' / 'resolv.conf'
        self.alias.parent.mkdir(); self.alias.symlink_to(self.source)
        self.enterContext(patch.object(sandbox_module, 'RESOLV_CONF', self.alias))
        self.enterContext(patch.object(sandbox_module, 'HOST_BRIDGES', (self.bridge,)))

    def execute(self, sandbox, script):
        command, env = sandbox.command([sys.executable, '-c', script])
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_only_resolver_is_readable_and_immutable_even_under_broad_write(self):
        sandbox = FilesystemSandbox(self.work, {'rules': [{'path': str(self.root), 'access': 'write'}]})
        script = f'''
from pathlib import Path
alias=Path({str(self.alias)!r}); source=Path({str(self.source)!r})
assert alias.is_symlink()
assert alias.read_text() == 'nameserver 192.0.2.53\\n'
for action in [lambda: alias.write_text('changed'), lambda: source.write_text('changed'),
               lambda: Path({str(self.secret)!r}).read_text(),
               lambda: list(Path({str(self.bridge)!r}).iterdir()),
               lambda: list(source.parent.iterdir()),
               lambda: (source.parent/'new').write_text('new')]:
    try: action()
    except OSError: pass
    else: raise RuntimeError('Unexpected bridge access')
print('resolver-only')
'''
        self.assertEqual(self.execute(sandbox, script), 'resolver-only')
        self.assertEqual(self.source.read_text(), 'nameserver 192.0.2.53\n')

    def test_explicit_denies_and_private_roots_are_never_reopened_for_dns(self):
        cases = [([{'path': str(self.alias.parent), 'access': 'deny'}], ()),
                 ([{'path': str(self.source), 'access': 'deny'}], ()),
                 ([{'path': str(self.bridge), 'access': 'deny'}], ()),
                 ([], (self.bridge,)), ([], (self.source,)), ([], (self.alias.parent,))]
        for rules, private in cases:
            with self.subTest(rules=rules, private=private):
                sandbox = FilesystemSandbox(self.work, {'rules': rules}, private_roots=private)
                self.assertIsNone(sandbox.materialize(sandbox.policy)['resolver'])
                script = f'''
from pathlib import Path
try: Path({str(self.alias)!r}).read_text()
except OSError: print('denied')
else: raise RuntimeError('Resolver reopened a private path')
'''
                self.assertEqual(self.execute(sandbox, script), 'denied')

    def test_regular_external_or_missing_resolvers_do_not_gain_an_exception(self):
        self.alias.unlink()
        for target in (None, self.secret, self.root/'missing', self.alias):
            with self.subTest(target=target):
                if self.alias.exists() or self.alias.is_symlink(): self.alias.unlink()
                if target is None: self.alias.write_text('nameserver 192.0.2.53\n')
                else: self.alias.symlink_to(target)
                # A symlink into a normal private directory is not an infrastructure grant.
                with patch.object(sandbox_module, 'HOST_BRIDGES', ()):
                    sandbox = FilesystemSandbox(self.work, {'rules': []})
                    expected = str(self.alias) if target is None else None
                    self.assertEqual(sandbox.materialize(sandbox.policy)['resolver'], expected)

    def test_resolver_under_synthetic_run_does_not_expose_host_runtime(self):
        runtime = Path('/run/user') / str(os.getuid())
        if not runtime.is_dir() or not os.access(runtime, os.W_OK):
            self.skipTest('Writable host user runtime required for /run fixture')
        with tempfile.TemporaryDirectory(prefix='aster-dns-', dir=runtime) as folder:
            source = Path(folder) / 'resolv.conf'; source.write_text('nameserver 192.0.2.53\n')
            secret = Path(folder) / 'private'; secret.write_text('hidden')
            self.alias.unlink(); self.alias.symlink_to(source)
            sandbox = FilesystemSandbox(self.work, {'rules': []})
            self.assertEqual(sandbox.materialize(sandbox.policy)['resolver'], str(source))
            script = f'''
from pathlib import Path
assert Path({str(self.alias)!r}).read_text() == 'nameserver 192.0.2.53\\n'
assert not Path({str(secret)!r}).exists()
assert not Path({str(runtime/'bus')!r}).exists()
try: Path({str(source)!r}).write_text('changed')
except OSError: print('resolver-only')
else: raise RuntimeError('Writable runtime resolver')
'''
            self.assertEqual(self.execute(sandbox, script), 'resolver-only')
