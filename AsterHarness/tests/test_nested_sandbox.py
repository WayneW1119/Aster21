"""Nested user identities keep the enclosing filesystem and process boundaries."""

import asyncio
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from agent_harness.core.sandbox import FilesystemSandbox
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.client import ScriptedClient

LINUX = sys.platform.startswith("linux") and Path("/usr/bin/bwrap").exists()


@unittest.skipUnless(LINUX, "Linux bubblewrap required")
class NestedSandboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aster-nested-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.hidden = self.work / "hidden"
        self.hidden.write_text("secret")
        self.reference = self.work / "reference"
        self.reference.write_text("reference")
        self.rules = [
            {"path": "/", "access": "read"},
            {"path": str(self.hidden), "access": "deny"},
            {"path": str(self.reference), "access": "read"},
        ]

    def execute(self, code, local_ipc=False):
        sandbox = FilesystemSandbox(
            self.work, {"rules": self.rules, "local_ipc": local_ipc}
        )
        argv, env = sandbox.command(
            [
                "/usr/bin/unshare",
                "--user",
                "--map-root-user",
                sys.executable,
                "-c",
                code,
            ]
        )
        result = subprocess.run(
            argv, env=env, capture_output=True, text=True, timeout=15
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_child_identity_preserves_paths_proc_and_host_process_isolation(self):
        code = f"""
import ctypes, os
from pathlib import Path
assert os.getuid() == 0  # Root only in the nested namespace.
c = ctypes.CDLL(None)
assert c.prctl(39, 0, 0, 0, 0) == 1  # no_new_privs survives nesting.
assert c.prctl(21, 0, 0, 0, 0) == 2  # seccomp filter survives nesting.
assert Path('reference').read_text() == 'reference'
Path('allowed').write_text('ok')
for path in (Path('hidden'), Path('/proc/self/root'+{str(self.hidden)!r})):
 try: content = path.read_text()
 except OSError: pass
 else: assert content == '', 'Original denied content exposed'
for action in (lambda: Path('hidden').write_text('bad'),
               lambda: Path('reference').write_text('bad'),
               lambda: Path('reference').unlink(),
               lambda: Path('/proc/self/root'+{str(self.hidden)!r}).write_text('bad')):
 try: action()
 except OSError: pass
 else: raise AssertionError('Nested identity bypassed path policy')
assert not Path('/proc/{os.getpid()}').exists(), 'Host process exposed'
for name in ('sys', 'fs', 'irq', 'bus'):
 p=Path('/proc')/name
 if p.exists(): assert os.statvfs(p).f_flag & os.ST_RDONLY, name
try: os.kill({os.getpid()}, 0)
except ProcessLookupError: pass
else: raise AssertionError('Host process addressable')
"""
        for local_ipc in (False, True):
            with self.subTest(local_ipc=local_ipc):
                self.execute(code, local_ipc)
        self.assertEqual(self.hidden.read_text(), "secret")
        self.assertEqual(self.reference.read_text(), "reference")

    def test_mount_apis_namespace_join_and_ipc_stay_restricted(self):
        code = """
import ctypes, errno, socket
c=ctypes.CDLL(None, use_errno=True)
lib=ctypes.CDLL('libseccomp.so.2')
lib.seccomp_syscall_resolve_name.argtypes=[ctypes.c_char_p]
# Names resolve per architecture; blocked syscalls must fail before arguments
# are interpreted (no mount, process attach or namespace join is performed).
for name in ('mount','umount2','pivot_root','fsopen','fsconfig','fsmount',
             'fspick','open_tree','move_mount','mount_setattr','setns',
             'ptrace','process_vm_readv','process_vm_writev','pidfd_getfd'):
 number=lib.seccomp_syscall_resolve_name(name.encode())
 if number < 0 and name == "mount_setattr":
  lib.seccomp_arch_native.restype=ctypes.c_uint32
  assert lib.seccomp_arch_native() in (0xC000003E,0xC00000B7,0x40000003)
  number=442
 if number < 0: continue
 ctypes.set_errno(0)
 assert c.syscall(ctypes.c_long(number),0,0,0,0,0,0)==-1, name
 assert ctypes.get_errno()==errno.EPERM, (name,ctypes.get_errno())
for flags in (0, 0x20000, 0x10020000):  # no-op, mount, user+mount
 ctypes.set_errno(0)
 assert c.unshare(flags)==-1 and ctypes.get_errno()==errno.EPERM
try: socket.socket(socket.AF_UNIX)
except PermissionError: pass
else: raise AssertionError('Local IPC implicitly granted')
"""
        self.execute(code)

    async def test_live_revocation_terminates_detached_nested_children(self):
        allowed = self.work / "output"
        allowed.mkdir()
        counter = allowed / "counter"
        session = ThreadManager(self.root / "threads").create(
            ScriptedClient([]),
            self.work,
            configuration=SessionConfiguration(filesystem_policy={"rules": self.rules}),
        )
        self.addAsyncCleanup(session.close)
        # Detach from the command's process group, remaining inside its PID
        # namespace. Killing the namespace owner must still reap this child.
        writer = (
            "import os,time;from pathlib import Path;os.setsid();p=Path("
            + repr(str(counter))
            + ");i=0\nwhile True:\n p.write_text(str(i));i+=1;time.sleep(.02)"
        )
        fork = (
            "import os,sys;pid=os.fork();os.waitpid(pid,0) if pid else os.execv(sys.executable,[sys.executable,'-c',"
            + repr(writer)
            + "])"
        )
        command = shlex.join(
            [
                "/usr/bin/unshare",
                "--user",
                "--map-root-user",
                sys.executable,
                "-c",
                fork,
            ]
        )
        result = await session.process_manager.exec_command(
            command, login=False, yield_time_ms=100
        )
        self.assertIn("session_id", result)
        for _ in range(100):
            if counter.exists():
                break
            await asyncio.sleep(0.02)
        self.assertTrue(counter.exists())
        from agent_harness.core.session.input_queue import HarnessEvent

        await session.apply_step_filesystem_policy(
            [
                HarnessEvent(
                    "host_policy_reload",
                    {
                        "_filesystem_policy": {
                            "rules": [
                                *self.rules,
                                {"path": str(allowed), "access": "read"},
                            ]
                        }
                    },
                )
            ]
        )
        self.assertNotIn(result["session_id"], session.process_manager.processes)
        before = counter.read_text()
        await asyncio.sleep(0.15)
        self.assertEqual(counter.read_text(), before)
        result = await session.process_manager.exec_command(
            shlex.join(
                [
                    "/usr/bin/unshare",
                    "--user",
                    "--map-root-user",
                    sys.executable,
                    "-c",
                    "from pathlib import Path;Path("
                    + repr(str(counter))
                    + ').write_text("bad")',
                ]
            ),
            login=False,
            yield_time_ms=1000,
        )
        self.assertNotEqual(result["exit_code"], 0)
