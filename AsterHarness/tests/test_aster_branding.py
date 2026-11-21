"""Aster entry points preserve existing public imports and existing data stores."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_harness.agent_config import aster_home, harness_home


class AsterBrandingTests(unittest.TestCase):
    def test_public_imports_alias_existing_runtime(self):
        from aster import Aster, Agent, Harness, ModelConfig
        from agent_harness import Aster as legacy_aster, Harness as legacy_harness
        from aster.events import emit_event, state_directory
        from agent_harness.events import emit_event as legacy_emit

        self.assertIs(Aster, Harness)
        self.assertIs(Aster, legacy_aster)
        self.assertIs(Aster, legacy_harness)
        self.assertIs(emit_event, legacy_emit)
        self.assertEqual(Aster.__name__, "Aster")

    def test_new_home_default_and_legacy_store_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("pathlib.Path.home", return_value=root),
            ):
                self.assertEqual(aster_home(), root / ".aster")
                (root / ".harness").mkdir()
                self.assertEqual(aster_home(), root / ".harness")
                (root / ".aster").mkdir()
                self.assertEqual(aster_home(), root / ".aster")
                self.assertEqual(harness_home(), aster_home())

    def test_new_environment_variable_has_priority_and_old_variable_works(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(
                os.environ,
                {"ASTER_HOME": str(root / "new"), "HARNESS_HOME": str(root / "old")},
            ):
                self.assertEqual(aster_home(), root / "new")
            with patch.dict(os.environ, {}, clear=True):
                os.environ["HARNESS_HOME"] = str(root / "old")
                self.assertEqual(aster_home(), root / "old")

    def test_new_module_cli_and_legacy_cli_share_configuration_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "work"
            work.mkdir()
            env = {
                **os.environ,
                "ASTER_HOME": str(root / "home"),
                "HARNESS_HOME": str(root / "unused"),
            }
            command = [
                sys.executable,
                "-m",
                "aster",
                "agent",
                "init",
                "sample",
                "--workspace",
                str(work),
                "--set",
                "sandbox.enabled=false",
            ]
            result = subprocess.run(
                command, env=env, text=True, capture_output=True, timeout=10
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "home/agents/sample/agent.toml").exists())
            for module in ("aster", "agent_harness"):
                result = subprocess.run(
                    [sys.executable, "-m", module, "agent", "list"],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "sample")
            self.assertFalse((root / "unused").exists())

    def test_new_event_helper_import_stays_lightweight(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import aster.events,sys;assert 'agent_harness.sdk' not in sys.modules",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_trace_module_help(self):
        for module in ('aster.compact_trace', 'aster.render_trace'):
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, '-m', module, '--help'],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('--output', result.stdout)
