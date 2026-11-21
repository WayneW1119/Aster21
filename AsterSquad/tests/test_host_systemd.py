"""Explicit broad host authority and bounded delegation."""
from unittest.mock import AsyncMock, patch

from relay.store import Problem
from squad.host_systemd import execute
from test_grants import GrantTests


class HostSystemdTests(GrantTests):
    async def test_owner_grant_revoke_without_runtime_restart(self):
        member = self.member()
        self.service.mode = 'real'
        execute_mock = AsyncMock(return_value={'exit_code': 0, 'stdout': 'active', 'stderr': ''})
        with patch('squad.host_systemd.execute', execute_mock), patch.object(self.service, '_stop', AsyncMock()) as stop:
            with self.assertRaises(Problem):
                await self.service.host_systemd(member['id'], 'systemctl', ['status'])
            self.assertFalse(execute_mock.called)
            await self.service.configure('owner', member['id'], host_systemd=True)
            stop.assert_not_called()
            result = await self.service.host_systemd(member['id'], 'systemctl', ['status', 'anything.service'])
            self.assertEqual(result['stdout'], 'active')
            await self.service.configure('owner', member['id'], host_systemd=False)
            with self.assertRaises(Problem):
                await self.service.host_systemd(member['id'], 'systemctl', ['status'])
            execute_mock.assert_awaited_once()

    async def test_parent_can_delegate_owned_host_authority(self):
        parent = self.member()
        child = self.member('child', 1, parent['id'])
        await self.service.configure('owner', parent['id'], host_systemd=True)
        await self.service.configure(parent['id'], child['id'], host_systemd=True)
        self.assertTrue(self.service.store.agent(child['id'])['config']['host_systemd'])
        for invalid in (1, 'true', []):
            with self.subTest(value=invalid), self.assertRaises(Problem):
                await self.service.configure('owner', child['id'], host_systemd=invalid)
        with self.assertRaises(Problem):
            await self.service.host_systemd(parent['id'], 'systemctl', ['status'])  # fake mode

    async def test_fixed_program_user_manager_environment_and_literal_arguments(self):
        proc = AsyncMock()
        proc.returncode = 0
        proc.stdout.read = AsyncMock(side_effect=[b'active\n', b''])
        proc.stderr.read = AsyncMock(return_value=b'')
        with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=proc)) as spawn:
            result = await execute('systemctl', ['show', 'literal;$(x).service'])
        self.assertEqual(result['stdout'], 'active\n')
        args, kwargs = spawn.call_args
        self.assertEqual(args, ('/usr/bin/systemctl', '--user', 'show', 'literal;$(x).service'))
        self.assertNotIn('SQUAD_MODEL_KEY', kwargs['env'])
        self.assertIn('DBUS_SESSION_BUS_ADDRESS', kwargs['env'])
        self.assertTrue(kwargs['start_new_session'])
        self.assertNotIn('shell', kwargs)

    async def test_reject_other_managers_and_invalid_input_before_spawn(self):
        cases = [('bash', ['-c', 'true']), ('systemctl', ['--system', 'status']),
                 ('systemctl', ['--host=example', 'status']), ('journalctl', ['-Mcontainer']),
                 ('systemctl', ['--root', '/tmp', 'enable', 'x']), ('systemctl', ['\0'])]
        with patch('asyncio.create_subprocess_exec', AsyncMock()) as spawn:
            for program, args in cases:
                with self.subTest(program=program, args=args), self.assertRaises(Problem):
                    await execute(program, args)
            with self.assertRaises(Problem):
                await execute('systemctl', ['status'], 121)
            spawn.assert_not_called()
