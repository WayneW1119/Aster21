"""Replay large journals with bounded parser memory and unchanged validation."""
import io
import json
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from uuid import uuid4

from agent_harness.core.rollout import RolloutRecorder
from agent_harness.core.rollout.reader import JournalRows
from agent_harness.profiles import saved_configuration


class StreamingRolloutTests(unittest.TestCase):
    def test_unicode_crlf_indexing_slicing_and_reverse_match_decoded_rows(self):
        rows = [{'text': value} for value in ('中文 🌿', 'second', '末尾')]
        for newline in ('\n', '\r\n', '\r'):
            with self.subTest(newline=repr(newline)), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'journal.jsonl'
                path.write_bytes(''.join(json.dumps(row, ensure_ascii=False) + newline for row in rows).encode())
                with path.open(encoding='utf-8') as stream:
                    indexed = JournalRows(stream)
                    self.assertEqual(list(indexed), rows)
                    self.assertEqual(indexed[-1], rows[-1])
                    self.assertEqual(list(indexed[1:]), rows[1:])
                    self.assertEqual(list(indexed[::-1]), rows[::-1])
                    self.assertEqual(list(reversed(indexed[:2])), list(reversed(rows[:2])))
                    self.assertEqual(list(indexed[1:][:1]), rows[1:2])
                    with self.assertRaises(IndexError):
                        indexed[len(rows)]

    def test_large_superseded_host_snapshots_do_not_stay_in_parser_memory(self):
        thread = str(uuid4())
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / thread
            directory.mkdir()
            path = directory / 'rollout.jsonl'
            def row(kind, payload):
                return json.dumps({'timestamp': '2026-01-01T00:00:00Z', 'type': kind, 'payload': payload}) + '\n'
            with path.open('w', encoding='utf-8') as stream:
                stream.write(row('session_meta', {'format_version': 1, 'thread_id': thread,
                    'cwd': str(Path(folder).resolve()), 'configuration': {
                        'base_instructions': 'example', 'max_consecutive_steps': None}}))
                snapshot = row('host_configuration', {'profile': {}, 'instructions': 'x' * 65536})
                for _ in range(256):
                    stream.write(snapshot)
                stream.write(row('response_item', {'type': 'message', 'role': 'user', 'text': '保留的历史'}))
                stream.write(row('session_closed', {'clean': True, 'step_number': 0}))
            tracemalloc.start()
            try:
                with path.open(encoding='utf-8') as stream:
                    restored = RolloutRecorder._read_stream(stream, thread)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 4 * 1024**2)  # Journal is >16 MiB; no complete decoded copy.
            self.assertEqual(restored.history.snapshot(), [
                {'type': 'message', 'role': 'user', 'text': '保留的历史'}])
            # SDK/CLI configuration validation must use the same bounded reader.
            tracemalloc.start()
            try:
                meta = saved_configuration(directory)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(meta, restored.meta)
            self.assertLess(peak, 4 * 1024**2)

    def test_stream_rejects_corruption_and_partial_last_record_without_changes(self):
        for value in ('', '{}', '{}\n{bad json}\n', '[]\n'):
            with self.subTest(value=value):
                stream = io.StringIO(value)
                with self.assertRaises(ValueError):
                    RolloutRecorder._read_stream(stream, str(uuid4()))
                self.assertEqual(stream.getvalue(), value)
