"""Configuration lifecycle: independent profiles, safe resume, and real CLI reload."""

import argparse
import asyncio
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from uuid import uuid4

from agent_harness.agent_config import (
    atomic_write,
    default_profile,
    harness_home,
    load_profile,
    toml_text,
)
from agent_harness.management import (
    main,
    load_profile,
    save_profile_copy,
    open_session,
    managed_run,
    named_threads,
    check_resume,
    running,
    saved_configuration,
)
from agent_harness.core.client import ScriptedClient


def fake_client(profile, trace):
    return ScriptedClient(
        [
            [
                {
                    "type": "function_call",
                    "name": "send_user_message",
                    "call_id": uuid4().hex,
                    "arguments": json.dumps({"text": "ok", "wake_model": False}),
                }
            ]
            for _ in range(10)
        ]
    )


class ManagementFixture:
    def setup_files(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.workspace = self.base / "work"
        self.workspace.mkdir()
        self.env_patch = patch.dict(os.environ, {"HARNESS_HOME": str(self.home)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.model_patch = patch(
            "agent_harness.profiles.client_for", side_effect=fake_client
        )
        self.model_patch.start()
        self.addCleanup(self.model_patch.stop)

    def command(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            main(list(args))
        return out.getvalue()

    def template(self, name="sample", sandbox=False):
        self.command(
            "agent",
            "init",
            name,
            "--workspace",
            str(self.workspace),
            "--set",
            'model.name="test-model"',
            "--set",
            "sandbox.enabled=" + str(sandbox).lower(),
        )
        return self.home / "agents" / name / "agent.toml"

    def start(self, name="alice", sandbox=False):
        self.template(sandbox=sandbox)
        self.command("start", "--agent", "sample", "--name", name, "--prompt", "hello")
        return next((self.home / "threads").glob("*/thread.toml")).parent


class ProfileTests(ManagementFixture, unittest.TestCase):
    def setUp(self):
        self.setup_files()

    def test_model_protocol_selection(self):
        profile = self.template()
        self.assertEqual(load_profile(profile, entries=['model.protocol="responses"']).data["model"]["protocol"], "responses")
        messages = load_profile(profile, entries=['model.protocol="messages"',
            'model.url="https://example.test/v1/messages"'])
        self.assertEqual(messages.data["model"]["protocol"], "messages")
        with self.assertRaisesRegex(ValueError, "Messages"):
            load_profile(profile, entries=['model.protocol="messages"'])
        with self.assertRaisesRegex(ValueError, "API key"):
            load_profile(profile, entries=['model.protocol="messages"', 'model.auth="chatgpt"'])
        with self.assertRaisesRegex(ValueError, "model.protocol"):
            load_profile(profile, entries=['model.protocol="unknown"'])

    def test_home_and_template_copy_are_independent(self):
        self.assertEqual(harness_home(), self.home)
        source = self.template()
        self.command(
            "agent",
            "init",
            "second",
            "--from",
            "sample",
            "--set",
            "context.warning_tokens=70000",
        )
        target = self.home / "agents" / "second" / "agent.toml"
        self.assertEqual(
            load_profile(target).configuration.context_budget_warning_tokens, 70000
        )
        self.assertEqual(
            load_profile(source).configuration.context_budget_warning_tokens, 100000
        )
        self.assertEqual(load_profile(target).configuration.max_consecutive_steps, None)
        self.assertEqual(self.command("agent", "list").split(), ["sample", "second"])

    def test_path_anchoring_and_toml_roundtrip(self):
        source = self.template(sandbox=True)
        data = tomllib.loads(source.read_text())
        extra = source.parent / "extra"
        extra.mkdir()
        data["workspace"] = "../../../work"
        # template is home/agents/sample; go to the fixture workspace explicitly
        data["workspace"] = os.path.relpath(self.workspace, source.parent)
        data["sandbox"]["rules"] = [{"path": "extra", "access": "read"}]
        data["skills"]["directories"] = ["extra"]
        data["name"] = "中文助手"
        atomic_write(source, toml_text(data))
        profile = load_profile(source)
        self.assertEqual(profile.workspace, self.workspace)
        self.assertEqual(
            Path(profile.configuration.filesystem_policy["rules"][0]["path"]), extra
        )
        self.assertEqual(tomllib.loads(toml_text(data)), data)
        snapshot = profile.snapshot()
        self.assertEqual(Path(snapshot["skills"]["directories"][0]), extra)

    def test_override_validation_is_atomic_and_rejects_typoes(self):
        source = self.template()
        before = source.read_text()
        for entry in (
            "context.warning_tokens=-1",
            "context.warn_tokens=500",
            'model.api_key="secret"',
            "runtime.max_consecutive_steps=true",
            'tools.code_mode="yes"',
        ):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.command("config", "set", "sample", "--agent", entry)
            self.assertEqual(source.read_text(), before)
        self.command(
            "config",
            "set",
            "sample",
            "--agent",
            "context.warning_tokens=0",
            'context.tool_output_tokens="unlimited"',
            "runtime.max_consecutive_steps=30",
        )
        result = load_profile(source)
        self.assertIsNone(result.configuration.context_budget_warning_tokens)
        self.assertIsNone(result.configuration.tool_output_token_limit)
        self.assertEqual(result.configuration.max_consecutive_steps, 30)

    def test_failed_template_init_can_be_retried(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.command("agent", "init", "bad", "--set", "context.warning_tokens=-1")
        self.assertFalse((self.home / "agents" / "bad").exists())
        self.command("agent", "init", "bad")

    def test_create_overrides_resume_and_saved_audit(self):
        source = self.template()
        self.command(
            "start",
            "--agent",
            "sample",
            "--name",
            "alice",
            "--set",
            "context.warning_tokens=80000",
            "--set",
            'model.name="other"',
            "--prompt",
            "first",
        )
        root = next((self.home / "threads").glob("*/thread.toml")).parent
        profile = load_profile(root / "thread.toml")
        self.assertEqual(profile.configuration.context_budget_warning_tokens, 80000)
        self.assertEqual(load_profile(source).data["model"]["name"], "test-model")
        memory = root / "memory" / "index.md"
        before = memory.read_text()
        self.command(
            "config",
            "set",
            "alice",
            "context.warning_tokens=50000",
            'model.name="changed"',
        )
        self.command(
            "resume",
            "alice",
            "--set",
            "runtime.max_consecutive_steps=50",
            "--prompt",
            "second",
        )
        meta = saved_configuration(root)
        self.assertEqual(meta["configuration"]["context_budget_warning_tokens"], 50000)
        self.assertEqual(meta["configuration"]["max_consecutive_steps"], 50)
        self.assertEqual(memory.read_text(), before)
        rows = [
            json.loads(line)
            for line in (root / "rollout.jsonl").read_text().splitlines()
        ]
        audit = [r["payload"] for r in rows if r["type"] == "host_configuration"]
        self.assertEqual(
            [a["profile"]["model"]["name"] for a in audit], ["other", "changed"]
        )
        self.assertEqual(len([r for r in rows if r["type"] == "session_meta"]), 1)
        self.assertEqual(len(list((root / "traces").glob("*.jsonl"))), 2)
        self.assertFalse(running(root))

    def test_workspace_override_does_not_change_template(self):
        source = self.template()
        other = self.base / "other"
        other.mkdir()
        self.command(
            "start", "--agent", "sample", "--workspace", str(other), "--prompt", "hi"
        )
        root = next((self.home / "threads").glob("*/thread.toml")).parent
        self.assertEqual(load_profile(root / "thread.toml").workspace, other)
        self.assertEqual(load_profile(source).workspace, self.workspace)

    def test_existing_thread_identity_and_permissions_cannot_be_replaced(self):
        root = self.start()
        for entry in (
            'workspace="' + str(self.base) + '"',
            'instructions="'
            + str(self.home / "agents" / "sample" / "instructions.md")
            + '"',
        ):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.command("config", "set", "alice", entry)
        self.command("config", "set", "alice", 'name="renamed"')
        self.assertEqual(named_threads(self.home)[0]["name"], "renamed")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.command(
                "start", "--agent", "sample", "--name", "renamed", "--prompt", "hi"
            )
        self.assertEqual(
            json.loads(self.command("status", "renamed"))["running"], False
        )

    def test_existing_unrestricted_thread_can_enable_permissions_on_restart(self):
        root = self.start()
        secret = self.workspace / "private"
        secret.mkdir()
        self.command(
            "config",
            "set",
            "alice",
            "sandbox.enabled=true",
            'sandbox.rules=[{path="' + str(secret) + '",access="deny"}]',
        )
        self.command("resume", "alice", "--prompt", "check updated permissions")
        saved = saved_configuration(root)["configuration"]
        self.assertIsNotNone(saved["filesystem_policy"])
        self.assertEqual(saved["filesystem_policy"]["rules"][0]["access"], "deny")
        self.command("resume", "alice", "--prompt", "continue")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.command(
                "resume",
                "alice",
                "--set",
                "sandbox.enabled=false",
                "--prompt",
                "invalid",
            )

    def test_credentials_reference_is_resolved_without_serializing_key(self):
        self.model_patch.stop()
        from agent_harness.management import client_for

        source = self.template()
        keyfile = self.home / "credentials" / "default.env"
        atomic_write(keyfile, 'PRIVATE_TEST_KEY="not-a-real-secret"\n')
        self.command(
            "config", "set", "sample", "--agent", 'model.api_key_env="PRIVATE_TEST_KEY"'
        )
        profile = load_profile(source)
        client = client_for(profile, lambda *_: None)
        self.assertEqual(client.api_key, "not-a-real-secret")
        with patch.dict(os.environ, {"PRIVATE_TEST_KEY": "environment-wins"}):
            self.assertEqual(
                client_for(profile, lambda *_: None).api_key, "environment-wins"
            )
        self.assertNotIn("not-a-real-secret", toml_text(profile.snapshot()))

    def test_separate_cli_process_sends_changed_model_endpoint_and_instructions(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import subprocess
        import sys
        import threading

        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, body, self.headers.get("Authorization")))
                data = json.dumps(
                    {
                        "status": "completed",
                        "output": [
                            {
                                "type": "function_call",
                                "name": "send_user_message",
                                "call_id": uuid4().hex,
                                "arguments": json.dumps(
                                    {"text": "ok", "wake_model": False}
                                ),
                            }
                        ],
                        "usage": {"input_tokens": 100, "output_tokens": 20},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.template()
            key = "dummy-http-integration-key"
            atomic_write(
                self.home / "credentials" / "default.env",
                "MANAGEMENT_TEST_KEY=" + key + "\n",
            )
            endpoint = "http://127.0.0.1:" + str(server.server_port)
            self.command(
                "config",
                "set",
                "sample",
                "--agent",
                'model.url="' + endpoint + '/first/responses"',
                "model.allow_http=true",
                'model.api_key_env="MANAGEMENT_TEST_KEY"',
            )

            def cli(*args):
                result = subprocess.run(
                    [sys.executable, "-m", "agent_harness", *args],
                    text=True,
                    capture_output=True,
                    env=dict(os.environ),
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            cli("start", "--agent", "sample", "--name", "http-test", "--prompt", "one")
            root = next((self.home / "threads").glob("*/thread.toml")).parent
            atomic_write(
                root / "instructions.md", "Updated instruction for HTTP verification"
            )
            cli(
                "resume",
                "http-test",
                "--set",
                'model.name="second-model"',
                "--set",
                'model.url="' + endpoint + '/second/responses"',
                "--prompt",
                "two",
            )
            self.assertEqual(
                [r[0] for r in requests], ["/first/responses", "/second/responses"]
            )
            self.assertEqual(
                [r[1]["model"] for r in requests], ["test-model", "second-model"]
            )
            self.assertIn(
                "Updated instruction for HTTP verification",
                requests[1][1]["instructions"],
            )
            self.assertEqual([r[2] for r in requests], ["Bearer " + key] * 2)
            for path in root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(key, path.read_text())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


class ReloadTests(ManagementFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_files()

    async def wait_for(self, predicate):
        async with asyncio.timeout(8):
            while not predicate():
                await asyncio.sleep(0.025)

    async def test_live_reload_same_thread_pins_instruction_until_applied(self):
        path = self.template()
        profile = load_profile(path)
        session = open_session(self.home, profile)
        root = session.rollout.path.parent
        save_profile_copy(profile, root, "live")
        original = profile.instructions()
        read_fd, write_fd = os.pipe()
        stdin = os.fdopen(read_fd, encoding="utf-8")
        try:
            with patch("sys.stdin", stdin), redirect_stdout(io.StringIO()):
                worker = asyncio.create_task(
                    managed_run(
                        self.home, root, session, argparse.Namespace(prompt=None)
                    )
                )
                await self.wait_for(lambda: (root / "runtime.json").exists())
                self.assertEqual(session.agent_context.instructions(), original)
                atomic_write(root / "instructions.md", "Changed role")
                self.assertEqual(session.agent_context.instructions(), original)
                # A request whose draft becomes invalid before application is rejected;
                # the original live session must keep working until a new request.
                valid_text = (root / "thread.toml").read_text()
                self.command("reload", "live")
                atomic_write(root / "thread.toml", "invalid = [")
                status = root / "reload.status.json"
                await self.wait_for(
                    lambda: status.exists()
                    and json.loads(status.read_text()).get("status") == "rejected"
                )
                self.assertTrue(running(root))
                self.assertEqual(
                    session.configuration.context_budget_warning_tokens, 100000
                )
                self.assertEqual(session.agent_context.instructions(), original)
                atomic_write(root / "thread.toml", valid_text)
                self.command("config", "set", "live", "context.warning_tokens=75000")
                self.command("reload", "live")
                status = root / "reload.status.json"
                await self.wait_for(
                    lambda: status.exists()
                    and json.loads(status.read_text()).get("status") == "applied"
                )
                os.write(write_fd, b"hello\n")
                await self.wait_for(
                    lambda: len(
                        [
                            r
                            for r in (root / "rollout.jsonl").read_text().splitlines()
                            if "response_item" in r
                        ]
                    )
                    >= 2
                )
                os.write(write_fd, b"/quit\n")
                await asyncio.wait_for(worker, 5)
            meta = saved_configuration(root)
            self.assertEqual(
                meta["configuration"]["context_budget_warning_tokens"], 75000
            )
            audits = [
                json.loads(line)["payload"]
                for line in (root / "rollout.jsonl").read_text().splitlines()
                if json.loads(line)["type"] == "host_configuration"
            ]
            self.assertEqual(
                [a["instructions"] for a in audits], [original, "Changed role"]
            )
            self.assertFalse((root / "runtime.json").exists())
        finally:
            os.close(write_fd)
            stdin.close()
            if not session.rollout.closed:
                await session.suspend()

    async def test_sandbox_policy_resume_uses_worldstate_transition_and_denies_credentials(
        self,
    ):
        source = self.template(sandbox=True)
        atomic_write(
            self.home / "credentials" / "default.env",
            "OPENAI_API_KEY=private-test-key\n",
        )
        profile = load_profile(source)
        session = open_session(self.home, profile)
        root = session.rollout.path.parent
        save_profile_copy(profile, root, "secure")
        from agent_harness.management import prepare_session

        await prepare_session(session, profile)
        with self.assertRaisesRegex(ValueError, "PermissionError|FileNotFoundError"):
            session.sandbox.file_operation(
                "read_file", path=str(self.home / "credentials" / "default.env")
            )
        await session.suspend()
        readonly = self.workspace / "readonly"
        readonly.mkdir()
        self.command(
            "config",
            "set",
            "secure",
            'sandbox.rules=[{path="' + str(readonly) + '",access="read"}]',
        )
        new_profile = load_profile(root / "thread.toml")
        resumed = open_session(self.home, new_profile, root)
        await prepare_session(resumed, new_profile)
        self.assertEqual(resumed.sandbox.version, 2)
        self.assertEqual(resumed.thread_id, session.thread_id)
        await resumed.suspend()
        again = open_session(self.home, new_profile, root)
        self.assertEqual(again.sandbox.version, 2)
        await again.suspend()


    async def test_host_profile_can_disable_and_reenable_same_thread(self):
        from agent_harness.management import prepare_session
        source = self.template(sandbox=True)
        profile = load_profile(source)
        session = open_session(self.home, profile)
        root = session.rollout.path.parent
        save_profile_copy(profile, root, "toggle")
        await prepare_session(session, profile)
        await session.suspend()
        for enabled, version in ((False, 2), (True, 3)):
            self.command("config", "set", "toggle", "sandbox.enabled=" + str(enabled).lower(), "sandbox.rules=[]")
            profile = load_profile(root / "thread.toml")
            resumed = open_session(self.home, profile, root)
            await prepare_session(resumed, profile)
            self.assertEqual(resumed.thread_id, session.thread_id)
            self.assertEqual(resumed.sandbox.enabled, enabled)
            self.assertEqual(resumed.sandbox.version, version)
            await resumed.suspend()
            again = open_session(self.home, profile, root)
            self.assertEqual(again.sandbox.enabled, enabled)
            self.assertEqual(again.sandbox.version, version)
            await again.suspend()
