"""Regression for real DeepSeek rejection of an event between a call and receipt."""

from copy import deepcopy
from datetime import datetime
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from agent_harness.core.client import ResponsesClient, order_tool_outputs
from agent_harness.core.client_common import Prompt
from agent_harness.core.trace import JsonlTrace
from agent_harness.render_trace import render

ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = None


def setUpModule():
    global RUN_ROOT
    RUN_ROOT=ROOT/'test_runs'/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-response-order-'+uuid4().hex[:8])
    RUN_ROOT.mkdir(parents=True)
    print(f'Response ordering artifacts: {RUN_ROOT}',flush=True)


def tool(identifier):
    return {'type':'function_call','call_id':identifier,'name':'read_file','arguments':'{}'}


def receipt(identifier):
    return {'type':'function_call_output','call_id':identifier,'output':'actual-output'}


def message(text,role='user'):
    return {'type':'message','role':role,'text':text}


class ResponseInputOrderTests(unittest.TestCase):
    def check_order(self,original,expected):
        saved=deepcopy(original)
        self.assertEqual(order_tool_outputs(original),expected)
        self.assertEqual(original,saved)
        (RUN_ROOT/(self._testMethodName+'.json')).write_text(
            json.dumps({'original':original,'expected':expected},ensure_ascii=False,indent=2),encoding='utf-8')

    def test_event_before_receipt_is_projected_after_receipt(self):
        call,event,out=tool('a'),message('command_completed'),receipt('a')
        self.check_order([call,event,out],[call,out,event])

    def test_multiple_calls_keep_all_receipts_before_events(self):
        a,b=tool('a'),tool('b')
        first,second=message('timer_fired'),message('program_event')
        oa,ob=receipt('a'),receipt('b')
        self.check_order([a,b,first,oa,second,ob],[a,b,oa,ob,first,second])

    def test_already_ordered_history_is_unchanged(self):
        history=[message('task'),tool('a'),receipt('a'),message('event')]
        self.check_order(history,history)

    def test_missing_receipt_is_not_fabricated(self):
        history=[tool('a'),tool('b'),message('event'),receipt('a')]
        self.check_order(history,history)

    def test_new_request_preserves_previous_request_prefix(self):
        original=[message('task'),tool('a'),message('first-event'),receipt('a')]
        before=order_tool_outputs(original)
        extended=original+[tool('b'),message('second-event'),receipt('b')]
        after=order_tool_outputs(extended)
        self.assertEqual(after[:len(before)],before)
        self.check_order(extended,before+[tool('b'),receipt('b'),message('second-event')])

    def test_actual_client_sends_ordered_body_and_retains_original_prompt(self):
        original=[tool('a'),message('command_completed'),receipt('a')]
        prompt=Prompt('instructions',deepcopy(original),[])
        trace_path=RUN_ROOT/'client-trace.jsonl'
        response=io.BytesIO(json.dumps({'status':'completed','output':[]}).encode())
        response.headers={'Content-Type':'application/json'}
        client=ResponsesClient('https://example.invalid/responses','unit-test-model',
                               'unit-test-key',stream=False,trace=JsonlTrace(trace_path))
        with patch('agent_harness.core.client.urlopen',return_value=response) as mocked:
            client.respond(prompt)
        body=json.loads(mocked.call_args.args[0].data)
        self.assertEqual([item['type'] for item in body['input']],
                         ['function_call','function_call_output','message'])
        self.assertEqual(prompt.input,original)
        trace_path.with_suffix('.md').write_text(render(trace_path),encoding='utf-8')


if __name__=='__main__':
    unittest.main(verbosity=2)
