"""Recover complete checkpoints without rewriting history or replaying effects."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_harness import Harness, ModelResponse, ScriptedClient, SessionConfiguration
from agent_harness.core.client_common import RetryableModelError
from agent_harness.core.rollout import RolloutRecorder


def call(name, identifier, **args):
    return {'type': 'function_call', 'name': name, 'call_id': identifier,
            'arguments': json.dumps(args)}


def answer(text):
    return [call('send_user_message', 'answer-' + text, text=text, wake_model=False)]


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.harness = Harness(self.root / 'home')
        self.settings = SessionConfiguration(filesystem_policy=None, max_consecutive_steps=None)

    def create(self, client, **kwargs):
        return self.harness.create(self.work, client=client, configuration=self.settings, **kwargs)

    async def failed(self):
        work = self.work
        class Broken:
            count = 0
            def respond(self, prompt):
                self.count += 1
                if self.count == 1:
                    return ModelResponse([call('exec_command', 'effect',
                        cmd="printf x >> effect.txt")])
                raise RuntimeError('permanent model failure')
        agent = self.create(Broken())
        await agent.start()
        await agent.send('do exactly one operation')
        with self.assertRaisesRegex(RuntimeError, 'permanent'):
            await asyncio.wait_for(agent.wait(), 5)
        with self.assertRaisesRegex(RuntimeError, 'permanent'):
            await agent.close()
        self.assertEqual((work / 'effect.txt').read_text(), 'x')
        return agent

    async def test_public_recovery_preserves_archive_identity_and_completed_effect(self):
        first = await self.failed()
        path = first.thread_path / 'rollout.jsonl'
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            self.harness.resume(first.thread_id, client=ScriptedClient([]))
        result = self.harness.recover(first.thread_id, reason='Operator confirmed transient auth failure')
        self.assertTrue(result['recovered'])
        self.assertTrue(path.read_bytes().startswith(original))
        self.assertFalse(self.harness.recover(first.thread_id, reason='Already recovered')['recovered'])
        second = self.harness.resume(first.thread_id, client=ScriptedClient([answer('continued')]))
        self.assertEqual(first.thread_id, second.thread_id)
        async with second.messages() as messages, second:
            await second.send('continue from history')
            self.assertEqual((await asyncio.wait_for(anext(messages), 3)).text, 'continued')
        self.assertEqual((self.work / 'effect.txt').read_text(), 'x')
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(sum(r['type'] == 'response_item' and
            r['payload'].get('call_id') == 'effect' and
            r['payload'].get('type') == 'function_call' for r in rows), 1)
        third = self.harness.resume(first.thread_id, client=ScriptedClient([]))
        await third.close()

    async def test_recovery_refuses_live_writer(self):
        agent = self.create(ScriptedClient([]))
        try:
            with self.assertRaisesRegex(ValueError, 'already open'):
                self.harness.recover(agent.thread_id, reason='must refuse')
        finally:
            await agent.close()

    async def test_corruption_and_unfinished_calls_are_never_repaired(self):
        agent = await self.failed()
        path = agent.thread_path / 'rollout.jsonl'
        original = path.read_text()
        rows = [json.loads(line) for line in original.splitlines()]
        cases = [original[:-1], '\n'.join(original.splitlines()[:-1]) + '\n']
        missing_receipt = [r for r in rows if not (r['type'] == 'response_item' and
            r['payload'].get('type') == 'function_call_output' and r['payload'].get('call_id') == 'effect')]
        cases.append(''.join(json.dumps(r) + '\n' for r in missing_receipt))
        for damaged in cases:
            with self.subTest(damaged=damaged[-80:]):
                path.write_text(damaged)
                with self.assertRaises(ValueError):
                    self.harness.recover(agent.thread_id, reason='must refuse')
                self.assertEqual(path.read_text(), damaged)
        path.write_text(original)
        self.harness.recover(agent.thread_id, reason='valid checkpoint')
        repaired = [json.loads(line) for line in path.read_text().splitlines()]
        repaired[-1]['payload']['closure_hash'] = 'tampered'
        path.write_text(''.join(json.dumps(r) + '\n' for r in repaired))
        with self.assertRaisesRegex(ValueError, 'Recovery record'):
            self.harness.resume(agent.thread_id, client=ScriptedClient([]))

    async def test_transient_failure_retries_same_prompt_without_duplicate_effect(self):
        from urllib.error import HTTPError
        retry_events = []
        class Flaky:
            prompts = []
            def respond(self, prompt):
                self.prompts.append(prompt)
                if len(self.prompts) == 1:
                    return ModelResponse([call('exec_command', 'effect', cmd='printf x >> effect.txt')])
                if len(self.prompts) == 2:
                    try:
                        raise HTTPError('https://secret.example/responses', 503, 'secret body', {}, None)
                    except HTTPError as cause:
                        raise RetryableModelError('secret provider text') from cause
                return ModelResponse(answer('retried'))
        client = Flaky()
        agent = self.create(client, on_trace=lambda e, d: retry_events.append(d) if e == 'model_retrying' else None)
        async with agent.messages() as messages, agent:
            await agent.send('one operation')
            self.assertEqual((await asyncio.wait_for(anext(messages), 5)).text, 'retried')
        self.assertEqual(client.prompts[1], client.prompts[2])
        self.assertEqual((self.work / 'effect.txt').read_text(), 'x')
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0]['http_status'], 503)
        self.assertEqual(retry_events[0]['cause_type'], 'HTTPError')
        self.assertIsInstance(retry_events[0]['observed_at'], float)
        self.assertGreaterEqual(retry_events[0]['elapsed_seconds'], 0)
        self.assertNotIn('secret', json.dumps(retry_events))

    async def test_pause_during_retry_checkpoints_a_wakeup_and_preserves_input(self):
        retrying = asyncio.Event()
        delays = []
        class Unavailable:
            def respond(self, prompt):
                raise RetryableModelError('service overloaded', retry_after_seconds=328)
        def trace(kind, data):
            if kind == 'model_retrying':
                delays.append(data['delay_seconds'])
                retrying.set()
        first = self.create(Unavailable(), on_trace=trace)
        await first.start()
        await first.send('original request')
        await asyncio.wait_for(retrying.wait(), 2)
        self.assertEqual(delays, [328])
        await asyncio.wait_for(first.pause(), 2)
        history = RolloutRecorder._read((first.thread_path / 'rollout.jsonl').read_text(), first.thread_id)
        self.assertEqual([e['kind'] for e in history.pending_events], ['model_retry_pending'])
        second = self.harness.resume(first.thread_id, client=ScriptedClient([answer('resumed')]))
        async with second.messages() as messages, second:
            self.assertEqual((await asyncio.wait_for(anext(messages), 3)).text, 'resumed')
        rows = [json.loads(line) for line in (first.thread_path / 'rollout.jsonl').read_text().splitlines()]
        inputs = [r for r in rows if r['type'] == 'response_item' and
                  r['payload'].get('text') == 'original request']
        self.assertEqual(len(inputs), 1)

    async def test_retry_limit_stops_requests_and_keeps_a_recoverable_thread(self):
        class Unavailable:
            calls = 0
            def respond(self, prompt):
                self.calls += 1
                raise RetryableModelError('temporary failure')
        client = Unavailable()
        events = []
        agent = self.create(client, on_trace=lambda e, d: events.append((e, d)))
        with patch('agent_harness.core.session.step.MAX_MODEL_RETRIES', 2):
            await agent.start()
            await agent.send('preserve this request')
            with self.assertRaisesRegex(RuntimeError, 'retry limit reached'):
                await asyncio.wait_for(agent.wait(), 5)
            with self.assertRaisesRegex(RuntimeError, 'retry limit reached'):
                await agent.close()
        self.assertEqual(client.calls, 3)  # One initial request, two retries.
        await asyncio.sleep(.05)
        self.assertEqual(client.calls, 3)
        self.assertEqual([d['attempt'] for e, d in events if e == 'model_retrying'], [1, 2])
        self.assertFalse(any(e == 'model_response' or e == 'tool_started' for e, _ in events))
        self.assertTrue(self.harness.recover(agent.thread_id, reason='Retry budget test')['recovered'])

    async def test_retry_advice_exceeding_budget_stops_without_requesting_early(self):
        class Unavailable:
            calls = 0
            def respond(self, prompt):
                self.calls += 1
                raise RetryableModelError('overloaded', retry_after_seconds=328)
        client = Unavailable()
        agent = self.create(client)
        with patch('agent_harness.core.session.step.MODEL_RETRY_BUDGET_SECONDS', .1):
            await agent.start()
            await agent.send('preserve input')
            with self.assertRaisesRegex(RuntimeError, 'retry time budget exhausted'):
                await asyncio.wait_for(agent.wait(), 2)
            with self.assertRaisesRegex(RuntimeError, 'retry time budget exhausted'):
                await agent.close()
        self.assertEqual(client.calls, 1)

    async def test_transient_auth_error_uses_same_bounded_model_retry(self):
        from agent_harness.auth.chatgpt import ChatGPTTransientAuthError
        class Flaky:
            calls = 0
            def respond(self, prompt):
                self.calls += 1
                if self.calls == 1:
                    raise ChatGPTTransientAuthError('key lookup unavailable')
                return ModelResponse(answer('verified'))
        client = Flaky()
        agent = self.create(client)
        async with agent.messages() as messages, agent:
            await agent.send('continue')
            self.assertEqual((await asyncio.wait_for(anext(messages), 3)).text, 'verified')
        self.assertEqual(client.calls, 2)
