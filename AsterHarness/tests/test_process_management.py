"""Owned host control across command sandboxes; never arbitrary PID control."""

import asyncio
import json
from pathlib import Path
import shlex
import socket
import sys
import tempfile
import unittest
from urllib.request import urlopen

from agent_harness.core.client import ScriptedClient
from agent_harness.core.event_generators.command_completion import CommandCompletionGenerator
from agent_harness.core.sandbox import FilesystemSandbox
from agent_harness.core.session import Session
from agent_harness.core.tools.handlers import build_file_tools
from agent_harness.core.unified_exec import UnifiedExecProcessManager


def command(code):
    return 'exec ' + shlex.quote(sys.executable) + ' -u -c ' + shlex.quote(code)


def call(name, ident, **arguments):
    return {'type': 'function_call', 'name': name, 'call_id': ident,
            'arguments': json.dumps(arguments)}


class ProcessManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = self.manager_for()

    def manager_for(self, sandbox=None):
        manager = UnifiedExecProcessManager(self.root, sandbox)
        self.addAsyncCleanup(manager.close)
        return manager

    async def test_non_tty_ctrl_c_is_an_interrupt_not_stdin(self):
        result = await self.manager.exec_command(command(
            "import signal,time,sys; "
            "signal.signal(signal.SIGINT,lambda *_:(print('INTERRUPTED'),sys.exit(7))); "
            "print('READY'); time.sleep(60)"), yield_time_ms=250, login=False)
        final = await self.manager.write_stdin(result['session_id'], '\x03', yield_time_ms=1000)
        self.assertEqual(final['exit_code'], 7)
        self.assertIn('INTERRUPTED', final['output'])
        self.assertNotIn(result['session_id'], self.manager.processes)

    async def test_list_metadata_does_not_consume_output_and_stop_invalidates_handle(self):
        result = await self.manager.exec_command(command(
            "import signal,time,sys; signal.signal(signal.SIGTERM,lambda *_:(print('STOPPED'),sys.exit(0))); "
            "print('READY'); time.sleep(60)"), yield_time_ms=250, login=False)
        listing = await self.manager.list_processes()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]['session_id'], result['session_id'])
        self.assertEqual(listing[0]['workdir'], str(self.root))
        self.assertFalse(listing[0]['tty'])
        self.assertNotIn('pid', listing[0])
        final = await self.manager.stop_process(result['session_id'])
        self.assertEqual(final['status'], 'stopped')
        self.assertEqual(final['exit_code'], 0)
        self.assertIn('STOPPED', final['output'])
        self.assertEqual(await self.manager.list_processes(), [])
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            await self.manager.stop_process(result['session_id'])

    async def test_cross_session_handle_or_host_pid_cannot_control_owner(self):
        result = await self.manager.exec_command('sleep 60', yield_time_ms=250, login=False)
        entry = self.manager.processes[result['session_id']]
        other = self.manager_for()
        self.assertEqual(await other.list_processes(), [])
        for ident in (result['session_id'], entry.process.pid):
            with self.assertRaisesRegex(ValueError, 'Unknown'):
                await other.stop_process(ident)
        self.assertIsNone(entry.process.returncode)
        await self.manager.stop_process(result['session_id'])

    async def test_stop_interrupts_long_poll_and_cleanup_is_idempotent(self):
        result = await self.manager.exec_command('sleep 60', tty=True, yield_time_ms=250, login=False)
        entry = self.manager.processes[result['session_id']]
        poll = asyncio.create_task(self.manager.write_stdin(result['session_id'], yield_time_ms=300000))
        await asyncio.sleep(.02)
        stopped = await asyncio.wait_for(self.manager.stop_process(result['session_id']), 3)
        polled = await asyncio.wait_for(poll, 1)
        self.assertEqual(stopped['status'], 'stopped')
        self.assertIsNotNone(polled['exit_code'])
        await asyncio.gather(entry.close(), entry.close(), self.manager.close())
        self.assertIsNone(entry.master_fd)

    async def test_stop_escalates_for_ignored_term_and_notifies_completion(self):
        result = await self.manager.exec_command(command(
            "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN); print('READY');time.sleep(60)"),
            yield_time_ms=250, login=False)
        events = []
        async def emit(kind, data):
            events.append((kind, data))
        entry = self.manager.processes[result['session_id']]
        monitor = asyncio.create_task(CommandCompletionGenerator(result['session_id'], entry).run(emit))
        stopped = await self.manager.stop_process(result['session_id'])
        await asyncio.wait_for(monitor, 1)
        self.assertEqual(stopped['exit_code'], -9)
        self.assertEqual(events[0][0], 'command_completed')
        self.assertEqual(events[0][1]['exit_code'], -9)

    async def test_host_started_command_is_discoverable_and_tools_use_events(self):
        session = Session(ScriptedClient([]), build_file_tools(self.root), workspace=self.root)
        self.addAsyncCleanup(session.close)
        started = await session.process_manager.exec_command('sleep 60', yield_time_ms=250, login=False)
        session.client = ScriptedClient([
            [call('list_processes', 'list')],
            [call('stop_process', 'stop', session_id=started['session_id'])],
            [call('send_user_message', 'done', text='Stopped owned command.', wake_model=False)]])
        await session.post_user_message('Stop my background service.')
        await session.run_until_idle()
        outputs = {i['call_id']: i for i in session.state.history.snapshot()
                   if i.get('type') == 'function_call_output'}
        self.assertEqual(json.loads(outputs['list']['output'])[0]['session_id'], started['session_id'])
        self.assertEqual(json.loads(outputs['stop']['output'])['status'], 'stopped')
        self.assertEqual(session.state.sent_messages, ['Stopped owned command.'])
        self.assertEqual(session.state.step_number, 3)

    @unittest.skipUnless(sys.platform.startswith('linux') and Path('/usr/bin/bwrap').exists(), 'Linux bubblewrap required')
    async def test_real_sandbox_http_service_stop_frees_port_without_affecting_other_session(self):
        sandbox = FilesystemSandbox(self.root, {})
        owner = self.manager_for(sandbox)
        other = self.manager_for(FilesystemSandbox(self.root, {}))
        server_code = ("import http.server; s=http.server.HTTPServer(('127.0.0.1',0),http.server.SimpleHTTPRequestHandler); "
                       "print('PORT='+str(s.server_port));s.serve_forever()")
        first = await owner.exec_command(command(server_code), yield_time_ms=1000, login=False)
        second = await other.exec_command(command(server_code), yield_time_ms=1000, login=False)
        ports = [int(r['output'].split('PORT=')[1].splitlines()[0]) for r in (first, second)]
        for port in ports:
            with await asyncio.to_thread(urlopen, f'http://127.0.0.1:{port}/', timeout=3) as response:
                self.assertEqual(response.status, 200)
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            await other.stop_process(first['session_id'])
        stopped = await owner.stop_process(first['session_id'])
        self.assertEqual(stopped['status'], 'stopped')
        with socket.socket() as connection:
            self.assertNotEqual(connection.connect_ex(('127.0.0.1', ports[0])), 0)
        with await asyncio.to_thread(urlopen, f'http://127.0.0.1:{ports[1]}/', timeout=3) as response:
            self.assertEqual(response.status, 200)
        final = await other.write_stdin(second['session_id'], '\x03', yield_time_ms=1000)
        self.assertIn('exit_code', final)
        with socket.socket() as connection:
            self.assertNotEqual(connection.connect_ex(('127.0.0.1', ports[1])), 0)
