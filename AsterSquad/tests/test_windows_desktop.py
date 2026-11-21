"""Native desktop lifecycle; no paid model requests or Codex image jobs."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.name == "nt", "Windows desktop supervisor")
class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aster-desktop-test-")
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, DEEPSEEK_API_KEY="test-only-not-a-real-key", PYTHONUTF8="1")
        self.command("init")
        config = json.loads((self.root / "desktop.json").read_text(encoding="utf-8"))
        sockets = [socket.socket() for _ in config["ports"]]
        try:
            for sock in sockets:
                sock.bind(("127.0.0.1", 0))
            config["ports"] = dict(zip(config["ports"], [s.getsockname()[1] for s in sockets]))
        finally:
            for sock in sockets:
                sock.close()
        config["images_enabled"] = False
        from squad.windows_desktop import write
        write(self.root / "desktop.json", config)
        self.config = config

    def command(self, action, *arguments, success=True):
        result = subprocess.run([sys.executable, "-P", "-m", "squad.windows_desktop", action,
                                 "--root", str(self.root), *arguments], env=self.env, capture_output=True,
                                text=True, encoding="utf-8", timeout=210)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def tearDown(self):
        try:
            self.command("stop")
        finally:
            self.temp.cleanup()

    def test_state_write_waits_for_a_short_lived_reader(self):
        from squad.windows_desktop import read, write
        path = self.root / "state-fixture.json"
        write(path, {"status": "starting"})
        reader = path.open("rb")
        release = threading.Timer(.1, reader.close)
        release.start()
        try:
            write(path, {"status": "running"})
        finally:
            release.join()
            reader.close()
        self.assertEqual(read(path), {"status": "running"})
        self.assertEqual(list(self.root.glob("state-fixture.json.*.tmp")), [])

    def test_state_write_preserves_last_state_when_reader_remains(self):
        from squad.windows_desktop import read, write
        path = self.root / "state-fixture.json"
        write(path, {"status": "starting"})
        with path.open("rb"), self.assertRaises(PermissionError):
            write(path, {"status": "running"})
        self.assertEqual(read(path), {"status": "starting"})
        self.assertEqual(list(self.root.glob("state-fixture.json.*.tmp")), [])

    def test_lifecycle_encrypted_key_and_duplicate_start(self):
        self.assertNotIn(self.env["DEEPSEEK_API_KEY"].encode(), (self.root / "credentials.dpapi").read_bytes())
        self.assertNotIn(self.env["DEEPSEEK_API_KEY"], (self.root / "desktop.json").read_text())
        first = json.loads(self.command("start").stdout)
        self.assertEqual(first["status"], "running")
        self.command("start", success=False)
        self.assertEqual(json.loads(self.command("status").stdout)["pid"], first["pid"])
        second = json.loads(self.command("restart").stdout)
        self.assertEqual(second["status"], "running")
        self.assertNotEqual(second["pid"], first["pid"])
        stopped = json.loads(self.command("stop").stdout)
        self.assertEqual(stopped["status"], "stopped")
        self.assertTrue(all(s["exit_code"] == 0 for s in stopped["services"].values()))

    @unittest.skipUnless(os.environ.get("ASTER_MXC_TEST_EXECUTOR"), "native MXC executor required")
    def test_opt_in_start_and_restart(self):
        executor = os.environ["ASTER_MXC_TEST_EXECUTOR"]
        first = json.loads(self.command("start", "--mxc-executor", executor).stdout)
        self.assertEqual(first["sandbox_backend"], "mxc")
        self.command("restart", "--mxc-executor", str(self.root / "missing.exe"), success=False)
        self.assertEqual(json.loads(self.command("status").stdout)["pid"], first["pid"])
        from squad.windows_desktop import write
        self.config["mxc_executor"] = executor
        write(self.root / "desktop.json", self.config)
        second = json.loads(self.command("restart").stdout)
        self.assertEqual(second["sandbox_backend"], "mxc")
        self.assertNotEqual(second["pid"], first["pid"])

    def test_port_conflict_preserves_existing_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", self.config["ports"]["console"]))
            listener.listen()
            result = self.command("start", success=False)
            self.assertIn("Port occupied: console", result.stderr)
            self.assertEqual(json.loads(self.command("status").stdout)["status"], "stopped")
            with socket.create_connection(listener.getsockname(), timeout=2):
                pass
            self.assertFalse((self.root / "desktop-state.json").exists())


class StateWriteErrorTests(unittest.TestCase):
    def test_unrelated_errors_fail_immediately_and_preserve_existing_state(self):
        from squad.windows_desktop import read, write
        errors = [PermissionError("POSIX permission denied"), OSError("disk error")]
        other_windows_error = PermissionError("unrelated Windows error")
        other_windows_error.winerror = 87
        errors.append(other_windows_error)
        with tempfile.TemporaryDirectory() as folder, patch('squad._windows.private_file'):
            path = Path(folder) / "state.json"
            write(path, {"status": "starting"})
            for error in errors:
                with self.subTest(error=str(error)), patch('squad.windows_desktop.os.replace', side_effect=error), patch('squad.windows_desktop.time.sleep') as sleep:
                    with self.assertRaises(type(error)) as raised:
                        write(path, {"status": "running"})
                    self.assertIs(raised.exception, error)
                    sleep.assert_not_called()
                    self.assertEqual(read(path), {"status": "starting"})
                    self.assertEqual(list(Path(folder).glob("*.tmp")), [])
