"""GPU devices are explicit permissions, with bounded real-device smoke tests."""
import asyncio
import importlib.util
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness.core.sandbox import FilesystemSandbox, parse_policy, gpu_devices
from agent_harness.core.session import SessionConfiguration
import test_filesystem_sandbox as filesystem
from agent_harness.core.sandbox import launcher
from agent_harness.agent_config import default_profile, load_profile
from agent_harness.core.context.world_state.environment import environment_item


class GPUValidationTests(unittest.TestCase):
    def test_pre_gpu_environment_evidence_remains_byte_stable(self):
        # Frozen output from Harness 0.1.17: history and compaction checkpoints
        # validate these exact rendered bytes, not only the structured policy.
        fixture = json.loads((Path(__file__).parent/'fixtures/environment-before-gpu.json').read_text())
        self.assertEqual(environment_item(fixture['state']), fixture['item'])

    def test_permission_is_opt_in_and_strictly_boolean(self):
        self.assertNotIn('gpu', parse_policy({}))
        self.assertNotIn('gpu', parse_policy({'gpu': False}))
        for invalid in (1, 'true', [], None):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                parse_policy({'gpu': invalid})

    def test_unavailable_device_fails_closed(self):
        with tempfile.TemporaryDirectory() as workspace, patch(
                'agent_harness.core.sandbox.gpu_devices', return_value=()):
            with self.assertRaisesRegex(RuntimeError, 'GPU access'):
                FilesystemSandbox(workspace, {'gpu': True})

    def test_profile_roundtrip_retains_device_permission(self):
        with tempfile.TemporaryDirectory() as folder, patch(
                'agent_harness.core.sandbox.gpu_devices', return_value=('/dev/dxg',)):
            root = Path(folder)
            data = default_profile(root, root)
            data['sandbox']['gpu'] = True
            profile = load_profile(root/'agent.toml', data=data, instruction_text='GPU profile fixture')
            self.assertTrue(profile.configuration.filesystem_policy['gpu'])
            self.assertTrue(profile.snapshot()['sandbox']['gpu'])

    @unittest.skipUnless(Path('/usr/bin/bwrap').exists(), 'Linux bubblewrap required')
    def test_launcher_rejects_unrecognized_device_before_spawning(self):
        with tempfile.TemporaryDirectory() as workspace:
            sandbox = FilesystemSandbox(workspace, {})
            config = sandbox.materialize(sandbox.policy)
            config['devices'] = ['/dev/null']
            with patch.object(launcher.subprocess, 'Popen') as spawn:
                with self.assertRaisesRegex(ValueError, 'Unsupported GPU device'):
                    launcher.launch(config, ['/bin/true'])
                spawn.assert_not_called()

    @unittest.skipUnless(gpu_devices(), 'WSL GPU device required')
    def test_path_writes_never_grant_devices_and_explicit_denies_are_respected(self):
        with tempfile.TemporaryDirectory() as workspace:
            sandbox = FilesystemSandbox(workspace, {'rules': [{'path': '/', 'access': 'write'}]})
            self.assertNotIn('devices', sandbox.materialize(sandbox.policy))
            for forbidden in ('/dev', '/dev/dxg'):
                with self.subTest(path=forbidden), self.assertRaises(ValueError):
                    FilesystemSandbox(workspace, {'gpu': True, 'rules': [{'path': forbidden, 'access': 'deny'}]})
            with self.assertRaises(ValueError):
                FilesystemSandbox(workspace, {'gpu': True}, private_roots=('/dev',))


@unittest.skipUnless(gpu_devices() and importlib.util.find_spec('torch'), 'WSL GPU and installed PyTorch required')
class GPUExecutionTests(unittest.IsolatedAsyncioTestCase):
    setUp = filesystem.FilesystemSandboxTests.setUp
    session = filesystem.FilesystemSandboxTests.session
    shell = filesystem.FilesystemSandboxTests.shell

    async def test_grant_compute_revoke_and_resume_same_thread(self):
        rules = [{'path': '/', 'access': 'read'}]
        session = self.session(filesystem.ScriptedClient([
            [filesystem.call('wait_for_event', 'granted')],
            [filesystem.call('wait_for_event', 'grant-receipt')],
        ]), configuration=SessionConfiguration(filesystem_policy={'rules': rules}))
        python = shlex.quote(sys.executable) + ' -u -c '
        await session.set_filesystem_policy({'rules': rules, 'gpu': True})
        await session.run_until_idle()
        self.assertEqual(session.sandbox.describe()['gpu_devices'], ['/dev/dxg'])
        thread = session.thread_id
        await session.suspend()
        session = self.manager.resume(filesystem.ScriptedClient([
            [filesystem.call('wait_for_event', 'revoked')],
            [filesystem.call('wait_for_event', 'revoke-receipt')],
        ]), thread)
        self.addAsyncCleanup(session.close)
        self.assertTrue(session.sandbox.policy['gpu'])
        self.assertEqual(session.thread_id, thread)
        command = python + shlex.quote("import numpy,torch,time; x=torch.arange(8.,device='cuda',requires_grad=True); loss=(x*x).sum(); loss.backward(); assert loss.item()==140 and torch.equal(x.grad,2*x); print('GPU_READY',flush=True); time.sleep(60)")
        running = await self.shell(session, command)
        handle = running['session_id']
        output = running['output']
        for _ in range(15):
            if 'GPU_READY' in output:
                break
            part = await session.process_manager.write_stdin(handle, '', yield_time_ms=1000)
            output += part['output']
        self.assertIn('GPU_READY', output)
        await session.set_filesystem_policy({'rules': rules})
        await session.run_until_idle()
        self.assertNotIn(handle, session.process_manager.processes)
        probe = await self.shell(session, python + shlex.quote("import torch; print(torch.cuda.is_available())"))
        output = probe['output']
        for _ in range(15):
            if 'session_id' not in probe:
                break
            probe = await session.process_manager.write_stdin(probe['session_id'], '', yield_time_ms=1000)
            output += probe['output']
        self.assertNotIn('session_id', probe)
        self.assertEqual(output.strip(), 'False')
        await session.suspend()
        restored = self.manager.resume(filesystem.ScriptedClient([]), session.thread_id)
        self.addAsyncCleanup(restored.close)
        self.assertFalse(restored.sandbox.policy.get('gpu', False))
        self.assertEqual(restored.thread_id, session.thread_id)
