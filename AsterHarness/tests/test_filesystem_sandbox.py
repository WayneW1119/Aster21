"""Real Linux enforcement, not mocked path checks or prompt-only restrictions."""

import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_harness.core.client import ScriptedClient
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.session.step import build_prompt
from agent_harness.core.session.step_context import StepContext
from agent_harness.core.sandbox import FilesystemSandbox, parse_policy
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.event_generators.program import ProgramGenerator
from agent_harness.core.event_generators.program_testing import (
    test_program as run_program_test,
)

AVAILABLE = sys.platform.startswith("linux") and Path("/usr/bin/bwrap").exists()


def call(name, ident, **arguments):
    return {
        "type": "function_call",
        "name": name,
        "call_id": ident,
        "arguments": json.dumps(arguments),
    }


class PolicyTests(unittest.TestCase):
    def test_schema_rejects_invalid_modes_shapes_and_versions(self):
        for value in (
            [],
            {"rules": None},
            {"rules": [{"path": "a", "access": "hidden"}]},
            {"rules": [{"path": "a", "access": []}]},
            {"rules": [{"path": "", "access": "read"}]},
            {"rules": [], "approval": "ask"},
            {"rules": [], "version": False},
            {"default_access": "write"},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_policy(value)
        self.assertIsNone(SessionConfiguration().filesystem_policy)
        self.assertEqual(parse_policy({}), {"rules": [], "version": 1, "format": 2})


@unittest.skipUnless(AVAILABLE, "Linux bubblewrap required")
class FilesystemSandboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.outside = self.root / "outside.txt"
        self.outside.write_text("outside readable")
        self.secret = self.work / "secret.txt"
        self.secret.write_text("SECRET_NEVER_READ")
        self.blocked = self.work / "private"
        self.blocked.mkdir()
        (self.blocked / "inner.txt").write_text("PRIVATE_NEVER_READ")
        self.readonly = self.work / "readonly"
        self.readonly.mkdir()
        (self.readonly / "data.txt").write_text("reference")
        self.policy = {
            "rules": [
                {"path": "/", "access": "read"},
                {"path": str(self.secret), "access": "deny"},
                {"path": str(self.blocked), "access": "deny"},
                {"path": str(self.readonly), "access": "read"},
            ]
        }
        self.manager = ThreadManager(self.root / "threads")
        self.events = []

    def test_default_deny_keeps_explicit_reads_workspace_writes_and_loopback(self):
        reference = self.root / "reference.txt"
        reference.write_text("explicit read")
        rules = [{"path": str(reference), "access": "read"}, *[r for r in self.policy["rules"] if r["path"] != "/"]]
        sandbox = FilesystemSandbox(self.work, {"rules": rules})
        self.assertEqual(sandbox.describe()["default_access"], "deny")
        script = """
import socket, sys
from pathlib import Path
reference, outside, secret = map(Path, sys.argv[1:4])
assert reference.read_text() == 'explicit read'
assert not outside.exists()
try:
    secret.read_text()
except OSError:
    pass
else:
    raise AssertionError('Explicitly denied workspace file was readable')
Path('result.txt').write_text('workspace write')
if sys.argv[5] == 'resolver':
    assert Path('/etc/resolv.conf').read_text()
with socket.create_connection(('127.0.0.1', int(sys.argv[4])), timeout=3):
    pass
"""
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            resolver = "resolver" if sandbox.materialize(sandbox.policy)["resolver"] else ""
            argv, env = sandbox.command([sys.executable, "-c", script, str(reference),
                                         str(self.outside), str(self.secret), str(listener.getsockname()[1]), resolver])
            result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.work / "result.txt").read_text(), "workspace write")

    def session(self, client=None, **kwargs):
        config = kwargs.pop(
            "configuration", SessionConfiguration(filesystem_policy=self.policy)
        )
        session = self.manager.create(
            client or ScriptedClient([]),
            self.work,
            configuration=config,
            trace=lambda event, data: self.events.append((event, data)),
            **kwargs,
        )
        self.addAsyncCleanup(session.close)
        return session

    async def shell(self, session, command):
        return await session.process_manager.exec_command(
            command, login=False, yield_time_ms=1000
        )

    def python(self, code):
        return shlex.quote(sys.executable) + " -c " + shlex.quote(code)

    def compact(self, session, first=None, last=None):
        history = session.state.history
        ids = history.ids()
        session._compact_boundary = set(ids)
        return json.loads(
            session.compact_history(
                first or ids[0],
                last or ids[-1],
                history.generation,
                "Preserve current permissions and verified progress.",
            )
        )

    async def test_shared_memory_is_writable_private_and_scoped_to_execution_tree(self):
        with tempfile.NamedTemporaryFile(prefix='aster-host-shm-', dir='/dev/shm') as host:
            for rules in ([], [{'path': '/', 'access': 'read'}], [{'path': '/', 'access': 'write'}]):
                with self.subTest(rules=rules):
                    session = self.session(configuration=SessionConfiguration(filesystem_policy={'rules': rules}))
                    code = """import os
from pathlib import Path
from multiprocessing.shared_memory import SharedMemory
assert not Path(HOST).exists()
assert os.statvfs('/dev').f_flag & os.ST_RDONLY
assert not os.statvfs('/dev/shm').f_flag & os.ST_RDONLY
Path('/dev/shm/execution-marker').write_text('private')
memory = SharedMemory(create=True, size=1)
try:
    memory.buf[0] = 7
    child = os.fork()
    if child == 0:
        attached = SharedMemory(name=memory.name)
        attached.buf[0] += 1
        attached.close()
        os._exit(0)
    assert os.waitpid(child, 0)[1] == 0
    assert memory.buf[0] == 8
finally:
    memory.close()
    memory.unlink()
""".replace('HOST', repr(host.name))
                    result = await self.shell(session, self.python(code))
                    self.assertEqual(result.get('exit_code'), 0, result)
                    result = await self.shell(session, 'test ! -e /dev/shm/execution-marker')
                    self.assertEqual(result.get('exit_code'), 0, result)

    async def test_writable_root_keeps_denials_runtime_views_and_seccomp(self):
        policy = {"rules": [{"path": "/", "access": "write"}, *self.policy["rules"]]}
        session = self.session(
            configuration=SessionConfiguration(filesystem_policy=policy)
        )
        result = await self.shell(
            session, "printf changed > " + shlex.quote(str(self.outside))
        )
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(self.outside.read_text(), "changed")
        for target in (
            self.secret,
            self.readonly / "data.txt",
            session.agent_context.memory_root / "index.md",
        ):
            result = await self.shell(
                session, "printf forbidden > " + shlex.quote(str(target))
            )
            self.assertNotEqual(result["exit_code"], 0, result)
        self.assertEqual(self.secret.read_text(), "SECRET_NEVER_READ")
        for path in (
            "/run",
            str(session.sandbox.package),
            str(session.sandbox.package.parent / "aster"),
            sys.prefix,
        ):
            result = await self.shell(
                session,
                self.python(
                    "import os;assert os.statvfs("
                    + repr(path)
                    + ").f_flag & os.ST_RDONLY"
                ),
            )
            self.assertEqual(result["exit_code"], 0, result)
        self.assertNotEqual(
            (await self.shell(session, "touch /run/root-write-escape"))["exit_code"], 0
        )
        self.assertNotEqual(
            (await self.shell(session, "unshare --user --mount --map-root-user true"))[
                "exit_code"
            ],
            0,
        )
        self.assertNotEqual(
            (
                await self.shell(
                    session, self.python("import socket;socket.socket(socket.AF_UNIX)")
                )
            )["exit_code"],
            0,
        )
        self.assertIn(
            {"path": "/", "access": "write"}, session.sandbox.describe()["rules"]
        )
        self.assertEqual(session.sandbox.describe()["default_access"], "deny")
        from agent_harness.core.context.world_state.environment import environment_item

        self.assertIn(
            'default_access="deny"',
            environment_item(session.sandbox.describe())["text"],
        )

    async def test_workspace_write_external_read_and_external_write_denial(self):
        session = self.session()
        result = await self.shell(
            session,
            "printf working > result.txt; cat " + shlex.quote(str(self.outside)),
        )
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual((self.work / "result.txt").read_text(), "working")
        denied = await self.shell(
            session, "printf changed > " + shlex.quote(str(self.outside))
        )
        self.assertNotEqual(denied["exit_code"], 0)
        self.assertEqual(self.outside.read_text(), "outside readable")
        result = await self.shell(
            session, "cd " + shlex.quote(str(self.root)) + "; printf no > new.txt"
        )
        self.assertNotEqual(result["exit_code"], 0)
        self.assertFalse((self.root / "new.txt").exists())

    async def test_deny_file_and_directory_block_read_write_but_names_can_remain(self):
        session = self.session()
        for command in (
            "cat secret.txt",
            "cat private/inner.txt",
            "ls private",
            "printf no > secret.txt",
            "printf no > private/new.txt",
        ):
            with self.subTest(command=command):
                result = await self.shell(session, command)
                self.assertNotEqual(result["exit_code"], 0, result)
                self.assertNotIn("NEVER_READ", result["output"])
        self.assertIn("secret.txt", (await self.shell(session, "ls"))["output"])
        self.assertEqual(self.secret.read_text(), "SECRET_NEVER_READ")

    async def test_readonly_carveout_cannot_be_replaced_deleted_or_renamed(self):
        session = self.session()
        for command in (
            "printf no > readonly/data.txt",
            "rm readonly/data.txt",
            "mv readonly elsewhere",
            "rmdir private",
            "rm secret.txt",
            "mv secret.txt moved",
        ):
            result = await self.shell(session, command)
            self.assertNotEqual(result["exit_code"], 0, (command, result))
        self.assertEqual((self.readonly / "data.txt").read_text(), "reference")

    async def test_builtin_file_patch_image_enforce_boundary_and_skills_are_host_service(self):
        (self.work / "link").symlink_to(self.secret)
        hidden_skill = self.blocked / "hidden/SKILL.md"
        hidden_skill.parent.mkdir()
        hidden_skill.write_text(
            "---\nname: hidden\ndescription: PRIVATE_GUIDE_DESCRIPTION\n---\nbody"
        )
        session = self.session(
            configuration=SessionConfiguration(
                filesystem_policy=self.policy, skill_sources=({"path": str(self.blocked)},)
            )
        )
        self.assertIn(
            "PRIVATE_GUIDE_DESCRIPTION", json.dumps(build_prompt(session).input)
        )
        skill = await session.router.invoke_async({"type": "function_call", "namespace": "skills",
            "name": "read", "call_id": "skill", "arguments": json.dumps({"skill_id": "hidden"})})
        self.assertIn("PRIVATE_GUIDE_DESCRIPTION", skill.output)
        for name, args in [
            ("read_file", {"path": "secret.txt", "start_line": 1, "end_line": 10}),
            ("read_file", {"path": "link", "start_line": 1, "end_line": 10}),
            ("list_directory", {"path": "private"}),
            (
                "apply_patch",
                {
                    "patch": "*** Begin Patch\n*** Delete File: secret.txt\n*** End Patch"
                },
            ),
        ]:
            result = await session.router.invoke_async(call(name, "probe", **args))
            self.assertIn("error", result.output.lower(), result.output)
            self.assertNotIn("SECRET_NEVER_READ", result.output)
        read = await session.router.invoke_async(
            call(
                "read_file",
                "outside",
                path=str(self.outside),
                start_line=1,
                end_line=10,
            )
        )
        self.assertIn("outside readable", read.output)
        image = await session.router.invoke_async(
            call("view_image", "image", path="secret.txt")
        )
        self.assertIn("error", image.output.lower())

    async def test_environment_is_filtered_and_private_thread_files_are_denied(self):
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "sentinel-not-to-inherit",
                "UNRELATED_SECRET": "other-secret",
            },
        ):
            session = self.session()
            result = await self.shell(
                session,
                self.python(
                    "import os,json;print(json.dumps([os.getenv('OPENAI_API_KEY'),os.getenv('UNRELATED_SECRET')]))"
                ),
            )
            self.assertIn("[null, null]", result["output"])
        result = await self.shell(
            session, "cat " + shlex.quote(str(session.agent_context.instructions_path))
        )
        self.assertNotEqual(result["exit_code"], 0)
        write = call(
            "write_memory", "memory", path="index.md", text="dedicated memory works"
        )
        session.record_items([write])
        await session.tool_runtime.execute_batch(
            StepContext.capture(session), [write], set()
        )
        batch = await session.input_queue.drain(wait=False)
        session.record_items([e.item for e in batch])
        self.assertEqual(session.memory.current["text"], "dedicated memory works")

    async def test_children_symlinks_and_mount_escape_do_not_bypass_restrictions(self):
        (self.work / "outside-link").symlink_to(self.outside)
        session = self.session()
        code = "import subprocess,sys;raise SystemExit(subprocess.call([sys.executable,'-c',\"open('outside-link','w').write('bad')\"]))"
        self.assertNotEqual(
            (await self.shell(session, self.python(code)))["exit_code"], 0
        )
        self.assertNotEqual(
            (await self.shell(session, "unshare --user --mount --map-root-user true"))[
                "exit_code"
            ],
            0,
        )
        code = "import socket;socket.socket(socket.AF_UNIX)"
        self.assertNotEqual(
            (await self.shell(session, self.python(code)))["exit_code"], 0
        )
        self.assertEqual(self.outside.read_text(), "outside readable")

    async def test_policy_paths_must_exist_and_not_cross_symlinks_or_reserved_views(
        self,
    ):
        link = self.work / "alias"
        link.symlink_to(self.blocked, target_is_directory=True)
        for path in (str(link), str(self.work / "missing"), "/run", "/proc"):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                FilesystemSandbox(
                    self.work, {"rules": [{"path": path, "access": "read"}]}
                )

    async def test_missing_backend_fails_closed(self):
        session = self.session()
        with patch(
            "agent_harness.core.sandbox.subprocess.run",
            side_effect=OSError("backend failed"),
        ):
            with self.assertRaises(OSError):
                session.sandbox.file_operation(
                    "read", path="secret.txt", start_line=1, end_line=1
                )
        self.assertFalse((self.work / "fallback-marker").exists())

    async def test_custom_script_is_sandboxed_and_cannot_register_denied_source(self):
        session = self.session()
        manager = session.custom_tools
        manager.apply(
            manager.prepare_group("create", "demo", description="Sandbox probes")
        )
        definition = {
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {}},
            "execution": {"command": sys.executable, "script_path": "secret.txt"},
        }
        with self.assertRaises(ValueError):
            manager.prepare_tool("register", "demo", "bad", definition)
        script = self.work / "tool.py"
        script.write_text(
            "import json,pathlib\np=pathlib.Path('secret.txt')\ntry: p.read_text();print('LEAK')\nexcept PermissionError: print('blocked')\n"
        )
        definition["execution"]["script_path"] = "tool.py"
        manager.apply(manager.prepare_tool("register", "demo", "probe", definition))
        result = await manager.bindings()[0].call({})
        self.assertTrue(result["success"], result)
        self.assertEqual(result["stdout"].strip(), "blocked")

    async def test_program_generator_uses_dedicated_event_fd_and_persistent_state(self):
        session = self.session()
        script = self.work / "source.py"
        script.write_text(
            "from agent_harness.events import emit_event,state_directory\nimport pathlib\ns=state_directory()/'count';s.write_text(str(int(s.read_text())+1) if s.exists() else '1')\nfor i in range(2):\n try:pathlib.Path('secret.txt').read_text();value='LEAK'\n except PermissionError:value='blocked'\n assert emit_event('probe',{'result':value})['status']=='accepted'\n"
        )
        record = session.generator_manager.register(
            ProgramGenerator(str(script), str(self.work), mode="once")
        )
        await asyncio.wait_for(
            session.generator_manager._tasks[record["generator_id"]], 5
        )
        records = session.generator_manager.list(True)
        self.assertEqual(records[0]["status"], "completed", records)
        self.assertEqual(records[0]["events_emitted"], 2)
        state = Path(records[0]["configuration"]["state_dir"]) / "count"
        self.assertEqual(state.read_text(), "1")
        self.assertNotIn("LEAK", json.dumps(session.input_queue.snapshot()))
        isolated = await run_program_test(
            script, self.work, timeout_ms=5000, sandbox=session.sandbox
        )
        self.assertEqual(isolated["status"], "completed", isolated)
        self.assertEqual(len(isolated["events"]), 2)

    async def test_runtime_policy_update_rebases_after_compact_and_resumes(self):
        session = self.session(
            ScriptedClient(
                [[call("wait_for_event", "wait")], [call("wait_for_event", "wait2")]]
            )
        )
        initial = session.environment.prefix_items()
        pending = await self.shell(session, "sleep 60")
        self.assertIn("session_id", pending)
        requested = await session.set_filesystem_policy(
            {"rules": [{"path": str(self.secret), "access": "read"}]}
        )
        await session.run_until_idle()
        self.assertEqual(session.sandbox.version, 2)
        self.assertNotIn(pending["session_id"], session.process_manager.processes)
        self.assertEqual(session.environment.prefix_items(), initial)
        self.assertTrue(
            any("_environment_state" in i for i in session.state.history.snapshot())
        )
        self.assertNotIn("_filesystem_policy", json.dumps(build_prompt(session).input))
        self.assertNotIn("_environment_state", json.dumps(build_prompt(session).input))
        self.assertIn(
            "SECRET_NEVER_READ", (await self.shell(session, "cat secret.txt"))["output"]
        )
        changed = next(
            i
            for i in session.state.history.ids()
            if "_environment_state" in session.state.history.item(i)
        )
        self.compact(session, changed, changed)
        self.assertEqual(session.environment.prefix_items(), initial)
        self.compact(session)
        self.assertEqual(session.environment.baseline["policy_version"], 2)
        await session.suspend()
        restored = self.manager.resume(ScriptedClient([]), session.thread_id)
        self.addAsyncCleanup(restored.close)
        self.assertEqual(restored.sandbox.version, 2)
        build_prompt(restored)
        self.assertIn(
            "SECRET_NEVER_READ",
            (await self.shell(restored, "cat secret.txt"))["output"],
        )

    async def test_host_disable_survives_compaction_resume_and_can_reenable(self):
        session = self.session(ScriptedClient([
            [call("wait_for_event", "off")], [call("wait_for_event", "off2")]]))
        initial = session.environment.prefix_items()
        pending = await self.shell(session, "sleep 60")
        # A host process outside the sandbox: test only our disposable child.
        host = await asyncio.create_subprocess_exec("sleep", "60")
        try:
            before = await self.shell(session, "kill -0 " + str(host.pid))
            self.assertNotEqual(before["exit_code"], 0)
            await session.set_filesystem_policy(None)
            self.assertTrue(session.sandbox.enabled)  # Not applied early.
            await session.run_until_idle()
            self.assertFalse(session.sandbox.enabled)
            self.assertNotIn(pending["session_id"], session.process_manager.processes)
            self.assertEqual(session.environment.prefix_items(), initial)
            self.assertIn('type="disabled"', json.dumps(build_prompt(session).input).replace('\\"', '"'))
            self.assertEqual(session.environment.current["policy_version"], 2)
            self.assertIn("SECRET_NEVER_READ", (await self.shell(session, "cat secret.txt"))["output"])
            result = await self.shell(session, "kill -TERM " + str(host.pid))
            self.assertEqual(result["exit_code"], 0)
            await asyncio.wait_for(host.wait(), 3)
            changed = next(i for i in session.state.history.ids()
                           if "_environment_state" in session.state.history.item(i))
            self.compact(session, changed, changed)
            self.compact(session)
            await session.suspend()
            restored = self.manager.resume(ScriptedClient([
                [call("wait_for_event", "on")], [call("wait_for_event", "on2")]]), session.thread_id)
            self.addAsyncCleanup(restored.close)
            self.assertFalse(restored.sandbox.enabled)
            self.assertEqual(restored.sandbox.version, 2)
            self.assertEqual(restored.thread_id, session.thread_id)
            await restored.set_filesystem_policy(self.policy)
            await restored.run_until_idle()
            self.assertTrue(restored.sandbox.enabled)
            self.assertEqual(restored.sandbox.version, 3)
            with self.assertRaises(ValueError):
                restored.router.registry.get_tool("read_file").handler(path="secret.txt", start_line=1, end_line=10)
            await restored.suspend()
            again = self.manager.resume(ScriptedClient([]), session.thread_id)
            self.addAsyncCleanup(again.close)
            self.assertTrue(again.sandbox.enabled)
            self.assertEqual(again.sandbox.version, 3)
        finally:
            if host.returncode is None:
                host.kill()
            await host.wait()

    async def test_initially_disabled_thread_can_enable_file_handlers_and_resume(self):
        session = self.session(ScriptedClient([
            [call("wait_for_event", "on")], [call("wait_for_event", "on2")]]),
            configuration=SessionConfiguration())
        self.assertIsNone(session.environment.baseline)
        await session.set_filesystem_policy(self.policy)
        await session.run_until_idle()
        with self.assertRaises(ValueError):
            session.router.registry.get_tool("read_file").handler(path="secret.txt", start_line=1, end_line=10)
        await session.suspend()
        restored = self.manager.resume(ScriptedClient([]), session.thread_id)
        self.addAsyncCleanup(restored.close)
        self.assertEqual(restored.sandbox.version, 2)
        build_prompt(restored)

    async def test_pending_policy_update_survives_suspend_without_early_application(
        self,
    ):
        session = self.session()
        await session.set_filesystem_policy({"rules": []})
        self.assertEqual(session.sandbox.version, 1)
        await session.suspend()
        restored = self.manager.resume(
            ScriptedClient(
                [[call("wait_for_event", "wait")], [call("wait_for_event", "wait2")]]
            ),
            session.thread_id,
        )
        self.addAsyncCleanup(restored.close)
        self.assertEqual(restored.sandbox.version, 1)
        await restored.run_until_idle()
        self.assertEqual(restored.sandbox.version, 2)

    async def test_policy_tightening_restarts_services_but_does_not_replay_once_sources(
        self,
    ):
        client = ScriptedClient(
            [[call("wait_for_event", f"wait{i}")] for i in range(8)]
        )
        session = self.session(
            client, configuration=SessionConfiguration(filesystem_policy={})
        )
        script = self.work / "long-source.py"
        script.write_text(
            "from agent_harness.events import emit_event,state_directory\nfrom pathlib import Path\nimport time\np=state_directory()/'count';n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n))\ntry:Path('secret.txt').read_text();readable=True\nexcept PermissionError:readable=False\nemit_event('ready',{'run':n,'readable':readable})\ntime.sleep(60)\n"
        )
        service = session.generator_manager.register(
            ProgramGenerator(str(script), str(self.work), mode="service")
        )
        once = session.generator_manager.register(
            ProgramGenerator(str(script), str(self.work), mode="once")
        )

        async def wait_count(record, n):
            path = Path(record["configuration"]["state_dir"]) / "count"
            for _ in range(200):
                if path.exists() and path.read_text() == str(n):
                    return
                await asyncio.sleep(0.01)
            self.fail("Generator did not reach expected startup count")

        await wait_count(service, 1)
        await wait_count(once, 1)
        await session.set_filesystem_policy(
            {"rules": [{"path": str(self.secret), "access": "deny"}]}
        )
        await session.run_until_idle()
        await wait_count(service, 2)
        await asyncio.sleep(0.05)
        records = {r["generator_id"]: r for r in session.generator_manager.list(True)}
        self.assertEqual(records[service["generator_id"]]["status"], "active", records)
        self.assertEqual(records[once["generator_id"]]["status"], "failed", records)
        self.assertEqual(
            (Path(once["configuration"]["state_dir"]) / "count").read_text(), "1"
        )
        fired = [
            data["generated_event"]
            for event, data in self.events
            if event == "generator_fired"
        ]
        readiness = [
            e["data"]["payload"] for e in fired if e["kind"] == "program_event"
        ]
        self.assertTrue(
            any(e["run"] == 2 and e["readable"] is False for e in readiness), readiness
        )

    async def test_policy_probe_failure_preserves_old_policy_and_processes(self):
        client = ScriptedClient(
            [[call("wait_for_event", "wait")], [call("wait_for_event", "wait2")]]
        )
        session = self.session(client)
        process = await self.shell(session, "sleep 60")
        await session.set_filesystem_policy(
            {"rules": [{"path": "/usr/bin", "access": "deny"}]}
        )
        await session.run_until_idle()
        self.assertEqual(session.sandbox.version, 1)
        self.assertIn(process["session_id"], session.process_manager.processes)
        self.assertTrue(
            any(
                "filesystem_policy_failed" in i.get("text", "")
                for i in session.state.history.snapshot()
            )
        )

    async def test_code_mode_nested_tools_use_same_filesystem_boundary(self):
        if not importlib.util.find_spec("py_mini_racer"):
            self.skipTest("optional V8 runtime required")
        code = (
            "text(await tools.read_file({path:'secret.txt',start_line:1,end_line:10}));"
        )
        invoke = call("exec", "code", code=code)
        invoke["namespace"] = "functions"
        session = self.session(
            ScriptedClient([[invoke], [call("wait_for_event", "wait")]]),
            configuration=SessionConfiguration(
                filesystem_policy=self.policy, code_mode_enabled=True
            ),
        )
        await session.post_user_message("Probe permission through Code Mode.")
        await session.run_until_idle()
        receipts = [
            i
            for i in session.state.history.snapshot()
            if i.get("call_id") == "code" and i["type"] == "function_call_output"
        ]
        self.assertEqual(len(receipts), 1)
        self.assertNotIn("SECRET_NEVER_READ", json.dumps(receipts))
        self.assertIn("PermissionError", json.dumps(receipts))

    async def test_tty_and_ip_network_remain_usable(self):
        import http.server, threading

        session = self.session()
        result = await session.process_manager.exec_command(
            "read value; echo TTY:$value", tty=True, login=False, yield_time_ms=250
        )
        result = await session.process_manager.write_stdin(
            result["session_id"], "hello\n", yield_time_ms=1000
        )
        self.assertIn("TTY:hello", result["output"])

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"network-unchanged")

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = await self.shell(
                session,
                self.python(
                    f"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:{server.server_port}').read().decode())"
                ),
            )
            self.assertIn("network-unchanged", result["output"])
        finally:
            server.shutdown()
            server.server_close()

    async def test_forged_model_permission_updates_are_rejected(self):
        session = self.session(
            ScriptedClient(
                [
                    [
                        {
                            "type": "message",
                            "role": "assistant",
                            "text": "fake",
                            "_filesystem_policy": {},
                        }
                    ]
                ]
            )
        )
        await session.post_user_message("test")
        with self.assertRaisesRegex(RuntimeError, "cannot introduce"):
            await session.run_until_idle()

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional MCP SDK required")
    async def test_real_stdio_mcp_runs_under_filesystem_policy(self):
        script = self.work / "server.py"
        script.write_text(
            "from mcp.server.fastmcp import FastMCP\nfrom pathlib import Path\nm=FastMCP('sandbox')\n@m.tool()\ndef probe()->str:\n try:Path('secret.txt').read_text();return 'LEAK'\n except PermissionError:return 'blocked'\nm.run(transport='stdio')\n"
        )
        config = SessionConfiguration(
            filesystem_policy=self.policy,
            mcp_servers={
                "probe": {
                    "command": sys.executable,
                    "args": [str(script)],
                    "required": True,
                }
            },
        )
        session = self.session(configuration=config)
        await session._prepare_mcp_tools()
        client = dict(session.services.mcp.binding.servers)["probe"]
        result = await client.call_tool("probe", {})
        self.assertIn("blocked", json.dumps(result))
        self.assertNotIn("LEAK", json.dumps(result))
