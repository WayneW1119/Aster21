"""Real local IPC opt-in with private temporary mounts and unchanged file boundaries."""
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

from agent_harness.core.sandbox import FilesystemSandbox, parse_policy
from agent_harness.agent_config import default_profile, load_profile

LINUX = sys.platform.startswith('linux') and Path('/usr/bin/bwrap').exists()


class LocalIPCSchemaTests(unittest.TestCase):
    def test_opt_in_and_profile_roundtrip(self):
        self.assertNotIn('local_ipc', parse_policy({}))
        for value in (1, 'true', [], None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_policy({'local_ipc': value})
        if not LINUX:
            return
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = default_profile(root, root)
            data['sandbox']['local_ipc'] = True
            profile = load_profile(root/'agent.toml', data=data, instruction_text='IPC fixture')
            self.assertTrue(profile.configuration.filesystem_policy['local_ipc'])
            self.assertTrue(profile.snapshot()['sandbox']['local_ipc'])
            data['sandbox']['enabled'] = False
            with self.assertRaises(ValueError):
                load_profile(root/'agent.toml', data=data)


@unittest.skipUnless(LINUX, 'Linux bubblewrap required')
class LocalIPCExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='aster-ipc-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.work = self.root/'work'; self.work.mkdir()
        self.secret = self.work/'secret'; self.secret.write_text('hidden')
        self.reference = self.root/'reference'; self.reference.write_text('readonly')
        self.rules = [{'path': '/', 'access': 'read'}, {'path': str(self.secret), 'access': 'deny'}]

    def run_script(self, policy, script):
        sandbox = FilesystemSandbox(self.work, policy)
        cmd, env = sandbox.command([sys.executable, '-c', script])
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_default_denial_and_opt_in_roundtrip_private_tmp_and_file_guards(self):
        self.assertEqual(self.run_script({'rules': self.rules}, '''
import socket
try: socket.socket(socket.AF_UNIX)
except PermissionError: print('denied')
else: raise AssertionError('Unix socket allowed by default')
'''), 'denied')
        script = f'''
import socket
from pathlib import Path
assert not Path({str(self.reference)!r}).exists()  # host /tmp stays hidden
for name in ('/tmp', '/var/tmp'):
    path = Path(name)/'ipc.sock'
    with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as client:
        server.bind(str(path)); server.listen(1); client.connect(str(path))
        peer, _ = server.accept()
        with peer:
            client.sendall(b'ping'); assert peer.recv(4) == b'ping'
    path.unlink()
for action in (lambda: Path({str(self.secret)!r}).read_text(),
               lambda: Path('/etc/hosts').write_text('bad')):
    try: action()
    except OSError: pass
    else: raise AssertionError('File restriction bypassed')
try: socket.socket(socket.AF_VSOCK)
except OSError: pass
else: raise AssertionError('VSOCK allowed')
print('allowed')
'''
        self.assertEqual(self.run_script({'rules': self.rules, 'local_ipc': True}, script), 'allowed')
        self.assertFalse(Path('/tmp/ipc.sock').exists())
        # A fresh command gets a fresh temporary filesystem.
        self.run_script({'local_ipc': True}, "from pathlib import Path; Path('/tmp/private').write_text('x')")
        self.run_script({'local_ipc': True}, "from pathlib import Path; assert not Path('/tmp/private').exists()")
        # Explicit host reference remains read-only even with IPC enabled.
        self.run_script({'local_ipc': True, 'rules': [*self.rules, {'path': str(self.reference), 'access': 'read'}]},
                        f"from pathlib import Path; p=Path({str(self.reference)!r}); assert p.read_text()=='readonly'\ntry: p.write_text('bad')\nexcept OSError: pass\nelse: raise AssertionError('read-only bypassed')")

    def test_explicit_tmp_deny_wins_and_root_write_does_not_expose_host_tmp(self):
        self.run_script({'local_ipc': True, 'rules': [{'path': '/tmp', 'access': 'deny'}]},
            "from pathlib import Path\ntry: Path('/tmp/new').write_text('x')\nexcept OSError: pass\nelse: raise AssertionError('deny bypassed')")
        self.run_script({'local_ipc': True, 'rules': [{'path': '/', 'access': 'write'}]},
            f"from pathlib import Path; assert not Path({str(self.reference)!r}).exists()")

    def test_visible_host_socket_is_reachable_without_file_write_authority(self):
        path = self.root/'echo.sock'
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path)); server.listen(1); server.settimeout(10)
            def echo():
                conn, _ = server.accept()
                with conn:
                    conn.sendall(conn.recv(4))
            thread = threading.Thread(target=echo, daemon=True); thread.start()
            self.run_script({'local_ipc': True, 'rules': [{'path': str(path), 'access': 'read'}]}, f'''
import socket
with socket.socket(socket.AF_UNIX) as client:
    client.settimeout(3); client.connect({str(path)!r}); client.sendall(b'ping'); assert client.recv(4)==b'ping'
''')
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def test_protected_roots_and_policy_context_remain_valid(self):
        from agent_harness.core.context.world_state.environment import environment_item
        sandbox = FilesystemSandbox(self.work, {'local_ipc': True, 'rules': self.rules},
                                    private_roots=(self.secret,))
        self.assertIn('Local Unix socket', environment_item(sandbox.describe())['text'])
        cmd, env = sandbox.command([sys.executable, '-c', f"from pathlib import Path\ntry: Path({str(self.secret)!r}).read_text()\nexcept OSError: pass\nelse: raise AssertionError('private root reopened')"])
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(__import__('shutil').which('soffice'), 'LibreOffice not installed')
    def test_native_libreoffice_renders_with_ipc_permission(self):
        import shutil
        source = self.work/'sample.txt'; source.write_text('Sandbox rendering acceptance')
        profile = (self.work/'office-profile').as_uri()
        output = self.work/'rendered'; output.mkdir()
        sandbox = FilesystemSandbox(self.work, {'local_ipc': True, 'rules': self.rules})
        command, env = sandbox.command([shutil.which('soffice'), '-env:UserInstallation='+profile,
            '--headless', '--convert-to', 'pdf', '--outdir', str(output), str(source)])
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((output/'sample.pdf').read_bytes().startswith(b'%PDF-'))


@unittest.skipUnless(LINUX, 'Linux bubblewrap required')
class LocalIPCLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_grant_revoke_and_resume_original_thread(self):
        import test_filesystem_sandbox as filesystem
        from agent_harness.core.thread_manager import ThreadManager
        from agent_harness.core.session import SessionConfiguration
        from agent_harness.core.context.world_state.environment import checked
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); work = root/'work'; work.mkdir()
            manager = ThreadManager(root/'threads')
            session = manager.create(filesystem.ScriptedClient([
                [filesystem.call('wait_for_event', 'a')],
                [filesystem.call('wait_for_event', 'b')],
            ]), workspace=work, configuration=SessionConfiguration(filesystem_policy={}))
            self.addAsyncCleanup(session.close)
            await session.set_filesystem_policy({'local_ipc': True})
            await session.run_until_idle()
            checked(session.sandbox.describe())
            self.assertTrue(session.sandbox.policy['local_ipc'])
            thread = session.thread_id
            await session.suspend()
            restored = manager.resume(filesystem.ScriptedClient([
                [filesystem.call('wait_for_event', 'c')],
                [filesystem.call('wait_for_event', 'd')],
            ]), thread)
            self.addAsyncCleanup(restored.close)
            self.assertTrue(restored.sandbox.policy['local_ipc'])
            await restored.set_filesystem_policy({})
            await restored.run_until_idle()
            self.assertNotIn('local_ipc', restored.sandbox.policy)
            self.assertEqual(restored.thread_id, thread)
