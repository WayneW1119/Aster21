import argparse
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness.__main__ import StdinReader, run_cli
from agent_harness.core.client import ScriptedClient
from agent_harness.core.session import Session
from agent_harness.core.tools.handlers import build_file_tools


class CliTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        read_fd, self.write_fd = os.pipe()
        self.stdin = os.fdopen(read_fd, encoding="utf-8")
        self.addCleanup(self.stdin.close)
        self.addCleanup(os.close, self.write_fd)

    async def test_buffered_paste_unicode_and_partial_final_line(self):
        with patch("sys.stdin", self.stdin):
            reader = StdinReader()
            os.write(self.write_fd, "第一条\n第二条\n".encode())
            self.assertEqual(await reader.read_line(), "第一条\n")
            self.assertEqual(await asyncio.wait_for(reader.read_line(), 1), "第二条\n")

    async def test_interactive_session_stays_alive_across_messages_then_quits(self):
        with tempfile.TemporaryDirectory() as directory:
            sent = asyncio.Queue()
            responses = [[{"type": "function_call", "name": "send_user_message",
                           "call_id": f"send_{index}", "arguments": '{"text": "ok", "wake_model": false}'}]
                         for index in range(2)]
            session = Session(ScriptedClient(responses), build_file_tools(Path(directory)),
                              send_message=sent.put_nowait)
            with patch("sys.stdin", self.stdin), patch("builtins.print"):
                worker = asyncio.create_task(run_cli(session, argparse.Namespace(prompt=None, demo=False)))
                os.write(self.write_fd, b"A\n")
                self.assertEqual(await asyncio.wait_for(sent.get(), 2), "ok")
                await asyncio.sleep(0)
                self.assertFalse(worker.done())
                os.write(self.write_fd, b"B\n")
                self.assertEqual(await asyncio.wait_for(sent.get(), 2), "ok")
                os.write(self.write_fd, b"/quit\n")
                await asyncio.wait_for(worker, 2)
            self.assertEqual(session.state.status, "stopped")
            self.assertEqual(session.state.step_number, 2)


class CliConfigurationTests(unittest.TestCase):
    def test_env_file_defaults_to_callers_directory_and_accepts_override(self):
        from agent_harness.__main__ import main
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            for extra, expected in (([], workspace / '.env'),
                                    (['--env-file', str(workspace / 'custom.env')], workspace / 'custom.env')):
                with self.subTest(extra=extra), \
                     patch('sys.argv', ['aster', '--workspace', directory, *extra]), \
                     patch('agent_harness.__main__.Path.cwd', return_value=workspace), \
                     patch('agent_harness.__main__.load_env_file', side_effect=SystemExit('config probe')) as load:
                    with self.assertRaisesRegex(SystemExit, 'config probe'):
                        main()
                    load.assert_called_once_with(expected)
