"""Exercise the privileged-worker transport without asking for elevation."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.name == "nt", "Windows worker transport")
class WindowsAdminTests(unittest.IsolatedAsyncioTestCase):
    def test_launcher_requests_uac_for_fixed_worker_entrypoint(self):
        from win32com.shell import shell
        from squad.windows_admin import _launch

        handle = object()
        with patch.object(shell, "ShellExecuteEx", return_value={"hProcess": handle}) as runas:
            self.assertIs(_launch(Path("request.json")), handle)
        self.assertEqual(runas.call_args.kwargs["lpVerb"], "runas")
        self.assertEqual(Path(runas.call_args.kwargs["lpFile"]).name.lower(), "pythonw.exe")
        self.assertIn("-I -m squad.windows_admin", runas.call_args.kwargs["lpParameters"])

    async def test_authenticated_controller_round_trip(self):
        import win32api
        from squad import windows_admin

        child_script = """
import asyncio, json, os, sys
from squad import windows_admin, worker
from squad._windows import StdinLines
windows_admin.is_admin = lambda: True
async def fixture_worker():
    stdin = StdinLines()
    line = json.loads(await stdin.readline())
    print(json.dumps({'event': line['op'], 'fixture': os.environ['FIXTURE']}), flush=True)
worker.main = lambda: asyncio.run(fixture_worker())
windows_admin.main()
"""
        processes = []

        def launch(request):
            process = subprocess.Popen([sys.executable, "-I", "-c", child_script, str(request)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(process)
            return win32api.OpenProcess(0x101000, False, process.pid)

        with TemporaryDirectory(prefix="aster-admin-test-") as temp:
            config = Path(temp).resolve() / "worker.json"
            config.write_text("{}", encoding="utf-8")
            try:
                with patch.object(windows_admin, "_launch", side_effect=launch):
                    worker = await asyncio.wait_for(
                        windows_admin.spawn_admin(config, {"FIXTURE": "private-env"}), 20,
                    )
                worker.stdin.write(b'{"op":"stop"}\n')
                await worker.stdin.drain()
                line = await asyncio.wait_for(worker.stdout.readline(), 10)
                self.assertEqual(json.loads(line), {"event": "stop", "fixture": "private-env"})
                self.assertEqual(await asyncio.wait_for(worker.wait(), 10), 0)
                self.assertEqual({p.name for p in config.parent.iterdir()}, {"worker.json", "worker.log"})
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)
