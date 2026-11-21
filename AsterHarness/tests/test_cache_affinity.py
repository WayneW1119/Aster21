"""Offline HTTP-boundary checks for ChatGPT cache affinity and Thread recovery."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_harness.auth.chatgpt import ChatGPTAccount
from agent_harness.core.chatgpt_client import ChatGPTResponsesClient
from agent_harness.core.client import ResponsesClient
from agent_harness.core.client_common import Prompt
from agent_harness.core.session.step_context import StepContext
from agent_harness.core.thread_manager import ThreadManager


class Response(io.BytesIO):
    headers = {'Content-Type': 'text/event-stream'}


def completed():
    return Response(('data: ' + json.dumps({'type': 'response.completed', 'response': {
        'status': 'completed', 'output': [], 'usage': {'input_tokens': 1}}}) + '\n\n').encode())


class CacheAffinityTests(unittest.IsolatedAsyncioTestCase):
    async def test_thread_identity_survives_clone_and_resume_and_isolates_other_threads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = ThreadManager(root / 'threads')
            requests = []

            def request(req, **kwargs):
                requests.append(req)
                return completed()

            def client():
                return ChatGPTResponsesClient(root, 'test-model')

            def respond(session):
                step = StepContext.capture(session)
                step.client.respond(step.prompt)

            with patch.object(ChatGPTAccount, 'access_token', return_value='offline-sentinel'), \
                 patch('agent_harness.core.client.urlopen', side_effect=request):
                session = manager.create(client(), root)
                thread_id, runtime_id = session.thread_id, session.session_id
                try:
                    respond(session)
                    respond(session)
                finally:
                    await session.close()
                resumed = manager.resume(client(), thread_id)
                try:
                    self.assertNotEqual(resumed.session_id, runtime_id)
                    respond(resumed)
                finally:
                    await resumed.close()
                other = manager.create(client(), root)
                try:
                    self.assertNotEqual(other.thread_id, thread_id)
                    respond(other)
                finally:
                    await other.close()
            keys = []
            for req in requests:
                body = json.loads(req.data)
                headers = {k.lower(): v for k, v in req.header_items()}
                self.assertEqual(headers['session-id'], body['prompt_cache_key'])
                self.assertIs(body['store'], False)
                self.assertNotIn('prompt_cache_retention', body)
                self.assertNotIn('prompt_cache_options', body)
                keys.append(body['prompt_cache_key'])
            self.assertEqual(keys[:3], [thread_id] * 3)
            self.assertNotEqual(keys[3], thread_id)

    async def test_ephemeral_session_identity_is_stable_and_separate(self):
        from agent_harness.core.session import Session
        from agent_harness.core.client import ScriptedClient
        from agent_harness.core.tools.registry import ToolRegistry
        with tempfile.TemporaryDirectory() as folder:
            sessions = [Session(ScriptedClient([]), ToolRegistry(), workspace=Path(folder))
                        for _ in range(2)]
            try:
                first = StepContext.capture(sessions[0]).prompt.cache_session_id
                self.assertEqual(first, StepContext.capture(sessions[0]).prompt.cache_session_id)
                self.assertNotEqual(first, StepContext.capture(sessions[1]).prompt.cache_session_id)
            finally:
                for session in sessions:
                    await session.close()

    async def test_ordinary_responses_does_not_enable_chatgpt_affinity_from_prompt_hint(self):
        client = ResponsesClient('https://example.invalid/responses', 'other-provider', 'sentinel')
        with patch('agent_harness.core.client.urlopen', return_value=completed()) as send:
            client.respond(Prompt('rules', [], [], cache_session_id='thread-hint'))
        req = send.call_args.args[0]
        self.assertNotIn('prompt_cache_key', json.loads(req.data))
        self.assertNotIn('session-id', {k.lower() for k, _ in req.header_items()})

    async def test_direct_client_clones_keep_fallback_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            client = ChatGPTResponsesClient(Path(folder), 'test-model')
            other = ChatGPTResponsesClient(Path(folder), 'test-model')
            with patch.object(ChatGPTAccount, 'access_token', return_value='offline-sentinel'), \
                 patch('agent_harness.core.client.urlopen', side_effect=lambda *a, **k: completed()) as send:
                for transport in (client, client.for_step(), client.for_step().for_step()):
                    transport.respond(Prompt('rules', [], []))
            keys = [json.loads(c.args[0].data)['prompt_cache_key'] for c in send.call_args_list]
            self.assertEqual(keys, [client.cache_session_id] * 3)
            self.assertNotEqual(keys[0], other.cache_session_id)

    async def test_cache_identity_rejects_header_injection(self):
        for invalid in ('', 'x\r\nAuthorization: other', 'space id', 'x' * 65, '中文', 7):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                ResponsesClient('https://example.invalid/responses', 'model', 'sentinel',
                                cache_session_id=invalid)
        original = ResponsesClient('https://example.invalid/responses', 'model', 'sentinel',
                                   cache_session_id='stable-id')
        self.assertEqual(original.for_step().cache_session_id, 'stable-id')
