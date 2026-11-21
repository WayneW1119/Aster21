"""ChatGPT plan transport: OAuth plus wire-only tool-search compatibility.

The Session retains its native events/history and deferred-tool lifecycle.
"""

from copy import deepcopy
from dataclasses import replace
import json
import re
from uuid import UUID, uuid4

from ..auth.chatgpt import ChatGPTAccount, ChatGPTAuthError, RESOURCE
from .client import ResponsesClient
from .client_common import ModelResponse
from .model_protocol import ModelReply, ModelRequest


def _clean_group(group):
    result = deepcopy(group)
    for tool in result.get('tools', []):
        tool.pop('defer_loading', None)
    return result


def project_plan_prompt(prompt):
    tools, functions = [], []
    for spec in prompt.tools:
        spec = deepcopy(spec)
        if spec['type'] == 'tool_search':
            functions.append({'type': 'function', 'name': 'tool_search', 'strict': False,
                'description': spec['description'], 'parameters': spec['parameters']})
        elif spec['type'] in ('function', 'custom'):
            spec.pop('defer_loading', None)
            functions.append(spec)
        elif spec['type'] == 'namespace':
            if spec['name'] == 'functions':
                functions.extend(_clean_group(spec)['tools'])
            else:
                tools.append(_clean_group(spec))
        else:
            raise ChatGPTAuthError('This ChatGPT plan adapter supports local function/custom tools only')
    if functions:
        tools.insert(0, {'type': 'namespace', 'name': 'functions',
            'description': 'Built-in Aster tools.', 'tools': functions})
    items = []
    for original in prompt.input:
        item = deepcopy(original)
        # Responses-compatible providers may retain response-only metadata.
        # The public plan input schema rejects status on historical calls.
        item.pop('status', None)
        if item.get('type') == 'reasoning':
            try:
                UUID(item.get('id', ''))
                foreign_reasoning = True
            except (ValueError, TypeError, AttributeError):
                foreign_reasoning = False
            if foreign_reasoning:
                # A provider UUID/opaque state cannot be decrypted by OpenAI.
                # Preserve visible text as assistant history, never rewrite the archive.
                parts = (item.get('summary') or []) + (item.get('content') or [])
                text = '\n'.join(part['text'] for part in parts
                                 if isinstance(part, dict) and isinstance(part.get('text'), str))
                if text:
                    items.append({'type': 'message', 'role': 'assistant',
                                  'text': 'Previous model reasoning (historical):\n' + text})
                continue
        elif item.get('type') == 'message' and not str(item.get('id', '')).startswith('msg'):
            # Other providers' IDs are not OpenAI message references. Keep the
            # archive unchanged and omit only this wire-level identifier.
            item.pop('id', None)
        elif item.get('type') in {'function_call', 'custom_tool_call'}:
            item.pop('id', None)
            # A compatible provider may put a qualified tool name in `name`.
            # The plan schema requires its namespace and name separately.
            # This is history projection only; receipts and the archive stay intact.
            qualified = re.fullmatch(r'([A-Za-z0-9_-]+)::([A-Za-z0-9_-]+)', item.get('name', ''))
            if qualified and item.get('namespace') in (None, '', qualified[1]):
                item.update(namespace=qualified[1], name=qualified[2])
        if item.get('type') == 'message' and item.get('role') == 'system':
            raise ChatGPTAuthError('ChatGPT plan requests require instructions or developer messages')
        if item.get('type') == 'tool_search_call':
            item = {'type': 'function_call', 'namespace': 'functions', 'name': 'tool_search',
                    'call_id': item['call_id'], 'arguments': json.dumps(item['arguments'], ensure_ascii=False)}
        elif item.get('type') == 'tool_search_output':
            definitions = [_clean_group(group) for group in item['tools']]
            items.append({'type': 'function_call_output', 'call_id': item['call_id'],
                'output': json.dumps({'loaded_namespaces': [group['name'] for group in definitions],
                    'tool_count': sum(len(group['tools']) for group in definitions)}, ensure_ascii=False)})
            if definitions:
                items.append({'type': 'additional_tools', 'role': 'developer', 'tools': definitions})
            continue
        items.append(item)
    return replace(prompt, input=items, tools=tools)


class ChatGPTResponsesClient:
    def __init__(self, home, model, *, profile='default', trace=None, cache_session_id=None):
        self.home, self.model, self.profile = home, model, profile
        self.account = ChatGPTAccount(home, profile)
        self.trace = trace or (lambda *_: None)
        # Direct callers without a Session still keep affinity across requests/clones.
        self.cache_session_id = cache_session_id if cache_session_id is not None else str(uuid4())

    def for_step(self):
        return type(self)(self.home, self.model, profile=self.profile, trace=self.trace,
                          cache_session_id=self.cache_session_id)

    def generate(self, request: ModelRequest) -> ModelReply:
        return ModelReply.from_response(self.respond(request.to_prompt()))

    def respond(self, prompt):
        projected = project_plan_prompt(prompt)
        transport = ResponsesClient(RESOURCE + '/responses', self.model,
            self.account.access_token(), stream=True, expect_sse=True,
            allow_empty_output=True, trace=self.trace,
            cache_session_id=projected.cache_session_id or self.cache_session_id)
        response = transport.respond(projected)
        output = []
        for original in response.output:
            item = deepcopy(original)
            if (item.get('type') == 'function_call' and item.get('name') == 'tool_search'
                    and item.get('namespace') in (None, '', 'functions')):
                try:
                    arguments = json.loads(item['arguments'])
                except (TypeError, ValueError):
                    raise RuntimeError('Model emitted invalid tool_search arguments') from None
                if not isinstance(arguments, dict):
                    raise RuntimeError('Model emitted invalid tool_search arguments')
                item = {'type': 'tool_search_call', 'call_id': item['call_id'],
                        'arguments': arguments, 'execution': 'client'}
            output.append(item)
        return ModelResponse(output, response.usage)
