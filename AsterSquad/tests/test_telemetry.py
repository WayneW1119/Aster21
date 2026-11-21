"""Public trace projection, honest cache denominators and durable summaries."""
import json
import tempfile
import unittest
from pathlib import Path

from squad.telemetry import usage_counts, trace_update, merge
from squad.store import Store


class TelemetryTests(unittest.TestCase):
    def test_provider_fields_missing_values_and_invalid_counts(self):
        expected={'input_tokens':100,'output_tokens':5,'cached_tokens':80}
        self.assertEqual(usage_counts({'input_tokens':100,'output_tokens':5,'input_tokens_details':{'cached_tokens':80}}),expected)
        self.assertEqual(usage_counts({'prompt_tokens':100,'completion_tokens':5,'prompt_cache_hit_tokens':80}),expected)
        self.assertIsNone(usage_counts({'input_tokens':100})['cached_tokens'])
        self.assertEqual(usage_counts({'input_tokens':100,'input_tokens_details':{'cached_tokens':0}})['cached_tokens'],0)
        for value in (True,-1,2.5,'3',None):
            self.assertIsNone(usage_counts({'input_tokens':value})['input_tokens'])
        self.assertIsNone(usage_counts({'input_tokens':1,'input_tokens_details':{'cached_tokens':2}})['cached_tokens'])

    def sample(self,inputs,cached,model='model',previous=None):
        return merge(previous,{'usage':usage_counts({'input_tokens':inputs,'input_tokens_details':{'cached_tokens':cached}}),'observed_at':10,'step':3},model)

    def test_weighted_window_excludes_unreported_and_is_bounded(self):
        state=self.sample(100,50)
        state=self.sample(900,810,previous=state)
        state=self.sample(10000,None,previous=state)
        self.assertEqual(state['cache_window'],{'requests':3,'reported':2,'input_tokens':1000,'cached_tokens':860,'hit_rate':.86})
        self.assertIsNone(state['latest']['cached_tokens'])
        for _ in range(30):state=self.sample(100,0,previous=state)
        self.assertEqual(len(state['recent']),20)
        self.assertEqual(state['cache_window']['hit_rate'],0)
        state=self.sample(30,10,'other-model',state)
        self.assertEqual(state['cache_window']['requests'],1)

    def test_projection_contains_no_prompt_tool_arguments_results_or_stream_content(self):
        secret='NEVER FORWARD THIS'
        self.assertIsNone(trace_update('api_stream_event',{'text':secret}))
        request=trace_update('model_request',{'prompt':{'secret':secret},'step':1})
        self.assertEqual(request['phase'],'thinking')
        tool=trace_update('tool_started',{'call':{'name':'exec_command','arguments':secret},'step':1})
        self.assertEqual(tool['phase'],'tools')
        self.assertEqual(tool['tool'],'exec_command')
        self.assertEqual(trace_update('tool_started',{'call':{'name':'wait_for_event'}})['phase'],'waiting')
        self.assertNotIn(secret,json.dumps([request,tool]))

    def test_compaction_does_not_present_old_input_measurement_as_current(self):
        state=self.sample(100,80)
        state=merge(state,trace_update('history_compacted',{'step':3}),'model')
        self.assertTrue(state['context_compacted'])
        self.assertEqual(state['latest']['input_tokens'],100)
        state=merge(state,trace_update('session_waiting',{'step':3}),'model')
        self.assertTrue(state['context_compacted'])
        state=self.sample(50,20,previous=state)
        self.assertFalse(state['context_compacted'])

    def test_summary_survives_store_reopen_without_changing_identity_or_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'squad.db';store=Store(path)
            member,_=store.create('test',None,0,{'model':'model'},'test')
            state=self.sample(100,80)
            store.update(member['id'],telemetry=state)
            store.db.close();store=Store(path)
            try:
                current=store.agent(member['id'])
                self.assertEqual(current['telemetry'],state)
                self.assertEqual(current['config'],member['config'])
            finally:store.db.close()
