from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from agent_harness.core.context_manager.memory import MemoryProjection
from scripts.performance_agent import memory_snapshots_valid, run_workspace_tests


class PerformanceMemoryChecks(unittest.TestCase):
    def request(self, baseline=None, items=None, observations=0):
        baseline = baseline or {'version': 0, 'text': '# Memory\n'}
        return {'event': 'model_request',
                'prompt': {'memory': MemoryProjection(baseline).item(), 'input': items or []},
                'world_state': {'sections': {'memory': {'observations': observations}}}}

    def test_separate_baseline_and_completed_update_survive_new_session(self):
        initial = self.request()
        updated = {'version': 1, 'text': 'verified work\n'}
        call = {'type': 'function_call', 'name': 'write_memory', 'call_id': 'm1',
                'arguments': json.dumps({'path': 'index.md', 'text': updated['text']})}
        receipt = {'type': 'function_call_output', 'call_id': 'm1',
                   'output': json.dumps({'memory_update': {'version': 1, 'source': 'call'}})}
        events = [{'event': 'session_initialized', 'thread_id': 'one'}, initial,
                  {'event': 'memory_version_committed', 'memory': {'current': updated}},
                  {'event': 'session_initialized', 'thread_id': 'one'},
                  self.request(items=[call, receipt], observations=1)]
        self.assertTrue(memory_snapshots_valid(events))
        self.assertFalse(memory_snapshots_valid(events[:-1] + [self.request()]))

    def test_baseline_must_be_present_outside_history(self):
        missing = self.request()
        missing['prompt']['memory'] = None
        self.assertFalse(memory_snapshots_valid([missing]))
        duplicated = self.request()
        duplicated['prompt']['input'].append(deepcopy(duplicated['prompt']['memory']))
        self.assertFalse(memory_snapshots_valid([duplicated]))

    def test_malformed_baseline_and_wrong_observation_count_are_rejected(self):
        malformed = self.request()
        malformed['prompt']['memory']['text'] = '<agent_memory>\ninvalid\n</agent_memory>'
        self.assertFalse(memory_snapshots_valid([malformed]))
        self.assertFalse(memory_snapshots_valid([self.request(observations=1)]))
        self.assertFalse(memory_snapshots_valid([]))


class PerformanceTestDiscovery(unittest.TestCase):
    def test_root_and_nonpackage_tests_directory_both_execute_tests(self):
        for folder in ('', 'tests'):
            with self.subTest(folder=folder), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                target = workspace / folder
                target.mkdir(exist_ok=True)
                (target / 'test_fixture.py').write_text(
                    'import unittest\nclass Check(unittest.TestCase):\n'
                    '    def test_result(self): self.assertEqual(2 + 2, 4)\n')
                result = run_workspace_tests(workspace)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Ran 1 test', result.stderr)

    def test_failure_exit_status_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / 'test_fixture.py').write_text(
                'import unittest\nclass Check(unittest.TestCase):\n'
                '    def test_result(self): self.fail("fixture failure")\n')
            result = run_workspace_tests(workspace)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('FAILED (failures=1)', result.stderr)
