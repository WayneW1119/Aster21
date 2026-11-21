import copy
import json
from pathlib import Path
import tempfile
import unittest

from agent_harness.compact_trace import pack_body, pack_requests, unpack_request, render_compact


class CompactTraceTests(unittest.TestCase):
    def test_round_trip_and_changes(self):
        bodies = [dict(instructions='完整固定提示\n```', tools=[{'name': 'x'}],
                       input=[{'text': '很长的动态内容' * 1000}], model='m', stream=False)]
        bodies.append(copy.deepcopy(bodies[0]))
        bodies.append(dict(instructions='变化后的完整提示', tools=[], input=[], extra={'a': 1}))
        rows = [{'event': 'api_request', 'endpoint': 'http://example.invalid/v1/responses',
                 'body': b} for b in bodies]
        before = copy.deepcopy(rows)
        definitions, requests = pack_requests(rows)
        self.assertEqual(len(definitions), 4)
        self.assertEqual([unpack_request(definitions, r) for r in requests], bodies)
        self.assertEqual(requests[0]['request_metadata']['endpoint'], 'http://example.invalid/v1/responses')
        self.assertEqual(rows, before)

    def test_render_retains_full_dynamic_and_final_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace.jsonl'
            rows = [{'event': 'api_request', 'body': {'instructions': 'fixed-unique',
                     'tools': [], 'input': [{'text': 'full-input'}]}},
                    {'event': 'api_response', 'body': {'instructions': 'fixed-unique',
                     'tools': [], 'output': [{'text': 'full-output'}]}},
                    {'event': 'tool_result', 'result': {'output': 'last-nonwaking-receipt'}}]
            path.write_text('\n'.join(json.dumps(r) for r in rows))
            output = render_compact([path])
            for value in ('full-input', 'full-output', 'last-nonwaking-receipt'):
                self.assertIn(value, output)
            self.assertEqual(output.count('fixed-unique'), 1)

    def test_response_echo_is_also_lossless(self):
        value = {'instructions': 'echo', 'tools': [{'name': 'x'}], 'usage': {'input_tokens': 12},
                 'output': [{'type': 'reasoning', 'encrypted_content': 'opaque'}]}
        definitions = {}
        self.assertEqual(unpack_request(definitions, pack_body(value, definitions)), value)
