"""Offline Windows preview acceptance; Linux-only tests remain in the full suite."""
from pathlib import Path
import os
import unittest

if os.name != 'nt':
    raise SystemExit('Run this acceptance suite using native Windows Python.')
root = Path(__file__).resolve().parents[1]
suite = unittest.TestSuite()
for pattern in ('test_rollout_streaming.py', 'test_dependency_boundaries.py', 'test_public_host_api.py', 'test_windows_jobs.py', 'test_windows_root_policy.py', 'test_mxc_role_grants.py', 'test_windows_runtime.py', 'test_windows_mxc_policy.py',
                'test_windows_mxc_runtime.py',
                'test_chatgpt_auth.py', 'test_code_mode.py', 'test_compact.py',
                'test_memory_projection.py', 'test_history_navigation.py',
                'test_context_budget.py', 'test_world_state_lifecycle.py'):
    suite.addTests(unittest.defaultTestLoader.discover(str(root/'tests'), pattern=pattern))
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
