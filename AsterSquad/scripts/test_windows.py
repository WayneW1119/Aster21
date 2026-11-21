"""Offline Windows preview acceptance. Full Linux suites remain unchanged."""
from pathlib import Path
import os
import unittest

if os.name != 'nt':
    raise SystemExit('Run with native Windows Python.')
root=Path(__file__).resolve().parents[1]
suite=unittest.TestSuite()
for pattern in ('test_autostart.py', 'test_autostart_system.py', 'test_dependency_boundaries.py', 'test_desktop_configuration.py', 'test_privileged_identity.py', 'test_windows_jobs.py', 'test_windows_system.py','test_windows_desktop.py','test_windows_admin.py','test_browser_auth.py',
                'test_configuration_errors.py','test_models.py','test_mcp_services.py',
                'test_safe_stop.py','test_sandbox_toggle.py','test_telemetry.py'):
    suite.addTests(unittest.defaultTestLoader.discover(str(root/'tests'),pattern=pattern))
result=unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
