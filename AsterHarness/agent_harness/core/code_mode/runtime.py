"""Session-owned cells; nested results go to JS, outer receipts remain queue events."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field, replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

from ..tools.context import ToolInvocation
from ..tools.names import is_default_namespace
from ..tools.router import ToolRouter
from ..tools.result import ToolResult

BLOCKED = frozenset({'write_memory', 'apply_memory_patch', 'compact_history', 'tool_search',
                     'manage_tool_group', 'manage_custom_tool', 'wait_for_event'})


@dataclass
class Cell:
    cell_id: str
    catalog: dict
    invocation: object
    status: str = 'running'
    output: list = field(default_factory=list)
    error: str | None = None
    process: object = None
    task: object = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    observed: bool = False
    generator_id: str | None = None
    calls: int = 0
    outcome_unknown: bool = False


class CompletionGenerator:
    kind = 'code_execution'

    def __init__(self, cell):
        self.cell = cell

    def describe(self):
        return {'cell_id': self.cell.cell_id}

    async def run(self, emit):
        await self.cell.done.wait()
        if not self.cell.observed:
            await emit('code_execution_completed', {'cell_id': self.cell.cell_id,
                'status': self.cell.status, 'has_unread_output': bool(self.cell.output),
                'outcome_unknown': self.cell.outcome_unknown})


class CodeModeRuntime:
    MAX_ACTIVE = 8
    MAX_CELLS = 128

    def __init__(self, session):
        self.session = session
        self.cells = {}
        self.store = {}
        self.closed = False

    def trace(self, event, data):
        self.session.trace(event, {'session_id': self.session.session_id,
            'step': self.session.state.step_number, **data})

    def catalog(self):
        definitions = self.session.router.registry.all_definitions()
        catalog = {}
        for entry in definitions:
            namespace, spec = entry['namespace'], entry['spec']
            name = spec.get('name')
            if (namespace == 'functions' and name in {'exec','wait'} or spec.get('type') != 'function'
                    or is_default_namespace(namespace) and name in BLOCKED):
                continue
            candidate = (namespace + '__' if not is_default_namespace(namespace) else '') + name
            candidate = re.sub(r'[^A-Za-z0-9_]', '_', candidate)
            if candidate[0].isdigit():
                candidate = '_' + candidate
            if candidate in catalog:
                candidate += '__' + hashlib.sha256(json.dumps([namespace, name]).encode()).hexdigest()[:12]
            if candidate in catalog:
                raise ValueError('Code Mode tool name collision')
            catalog[candidate] = {'namespace': namespace, 'name': name,
                'metadata': {'name': candidate, 'description': spec.get('description', ''),
                             'deferred': entry['deferred']}}
        return catalog

    @staticmethod
    def wait_time(value):
        if type(value) is not int or not 0 <= value <= 30000:
            raise ValueError('yield_time_ms must be 0-30000')
        return value / 1000

    async def execute(self, code, yield_time_ms=1000, timeout_ms=30000):
        if self.closed:
            raise ValueError('Code Mode runtime is closed')
        if not isinstance(code, str) or not code.strip() or len(code) > 100000:
            raise ValueError('code must contain 1-100000 characters of JavaScript')
        delay = self.wait_time(yield_time_ms)
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 300000:
            raise ValueError('timeout_ms must be 100-300000')
        if importlib.util.find_spec('py_mini_racer') is None:
            raise ValueError('Code Mode requires: python -m pip install ".[code-mode]"')
        invocation = self.session.tool_runtime.current_invocation
        if invocation is None:
            raise ValueError('Code Mode exec requires a captured tool invocation')
        if sum(c.status == 'running' for c in self.cells.values()) >= self.MAX_ACTIVE:
            raise ValueError('Too many active Code Mode cells')
        if len(self.cells) >= self.MAX_CELLS:
            for ident, cell in list(self.cells.items()):
                if cell.done.is_set() and cell.observed:
                    del self.cells[ident]
                    break
            else:
                raise ValueError('Read completed Code Mode results before starting more cells')
        cell = Cell('cell_' + uuid4().hex, self.catalog(), invocation)
        self.cells[cell.cell_id] = cell
        cell.task = asyncio.create_task(self._run(cell, code, timeout_ms))
        await self._observe(cell, delay)
        if cell.status == 'running':
            try:
                record = self.session.generator_manager.register(CompletionGenerator(cell))
                cell.generator_id = record['generator_id']
            except BaseException:
                cell.task.cancel()
                await asyncio.gather(cell.task, return_exceptions=True)
                raise
        return self._receipt(cell)

    async def wait(self, cell_id, yield_time_ms=1000, terminate=False):
        delay = self.wait_time(yield_time_ms)
        if type(terminate) is not bool:
            raise ValueError('terminate must be a boolean')
        cell = self.cells.get(cell_id) if isinstance(cell_id, str) else None
        if cell is None:
            raise ValueError('Unknown Code Mode cell; cells do not survive Session restart')
        if terminate and not cell.done.is_set():
            cell.task.cancel()
            await asyncio.gather(cell.task, return_exceptions=True)
        else:
            await self._observe(cell, delay)
        return self._receipt(cell)

    async def _observe(self, cell, delay):
        if not cell.done.is_set() and delay:
            try:
                await asyncio.wait_for(cell.done.wait(), delay)
            except TimeoutError:
                pass

    def _receipt(self, cell):
        output = deepcopy(cell.output)
        cell.output.clear()  # Consumed output is already retained in trace/tool receipts.
        cell.observed |= cell.status != 'running'
        metadata = {'cell_id': cell.cell_id, 'status': cell.status, 'nested_calls': cell.calls}
        if cell.generator_id:
            metadata['generator_id'] = cell.generator_id
        if cell.outcome_unknown:
            metadata['outcome_unknown'] = True
        if cell.error:
            metadata['error'] = cell.error[:128]
        self.trace('code_mode_observed', {**metadata, 'output': output})
        protected = {key:value for key,value in metadata.items() if key in {'cell_id','status','generator_id','outcome_unknown'}}
        return ToolResult([{'type':'input_text', 'text':json.dumps(metadata, ensure_ascii=False)}] + output,
                          protected_fields={'code_execution':protected})

    async def _invoke(self, cell, message):
        if self.closed:
            raise ValueError('Code Mode runtime is stopping')
        target = cell.catalog.get(message.get('name'))
        if target is None or is_default_namespace(target['namespace']) and target['name'] in BLOCKED:
            raise ValueError('Tool is not available through Code Mode; call state-management tools directly')
        arguments = message.get('arguments')
        if not isinstance(arguments, dict):
            raise ValueError('Tool arguments must be an object')
        cell.calls += 1
        if cell.calls > 128:
            raise ValueError('Code Mode cell exceeds 128 nested calls')
        call = {'type':'function_call', 'call_id':f'{cell.cell_id}_{message["id"]}',
                'name':target['name'], 'arguments':json.dumps(arguments, ensure_ascii=False)}
        if target['namespace']:
            call['namespace'] = target['namespace']
        # Every newly dispatched call checks current availability and loaded schemas.
        # Already dispatched calls keep their exact handler; never replay a failed call.
        session = self.session
        router = ToolRouter(session.router.registry.snapshot(), mcp_binding=session.deferred_binding(),
            loaded_mcp=session.mcp_projection.loaded_definitions() if session.mcp_projection else {})
        nested = ToolInvocation(call, replace(cell.invocation.step, tool_router=router), None)
        self.trace('code_mode_nested_call', {'cell_id':cell.cell_id, 'call':call})
        try:
            with session.tool_runtime.invocation_scope(nested):
                result = await router.invoke_async(call)
        except asyncio.CancelledError:
            self.trace('code_mode_nested_cancelled', {'cell_id':cell.cell_id, 'call':call,
                                                     'outcome_unknown':True})
            raise
        try:
            raw = result.output if isinstance(result.output, str) else next(
                p['text'] for p in result.output if p['type'] == 'input_text')
            payload = json.loads(raw)
            if isinstance(payload, dict):
                error = payload.get('error')
                cell.outcome_unknown |= (payload.get('outcome_unknown') is True or
                    isinstance(error, dict) and error.get('outcome_unknown') is True)
        except (ValueError, StopIteration):
            pass
        self.trace('code_mode_nested_result', {'cell_id':cell.cell_id, 'call_id':call['call_id'],
            'output':result.output, 'raw_output':result.raw_output})
        return {'output':result.output}

    async def _run(self, cell, code, timeout_ms):
        pending = set()
        write_lock = asyncio.Lock()

        async def rpc(message):
            try:
                response = {'id':message['id'], 'result':await self._invoke(cell, message)}
            except asyncio.CancelledError:
                raise
            except Exception as error:
                response = {'id':message['id'], 'error':f'{type(error).__name__}: {error}'}
            if cell.process.returncode is None:
                async with write_lock:
                    cell.process.stdin.write((json.dumps(response, ensure_ascii=True) + '\n').encode())
                    await cell.process.stdin.drain()

        self.trace('code_mode_started', {'cell_id':cell.cell_id, 'code':code})
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                worker = Path(__file__).with_name('worker.py')
                # The worker receives no model credentials or live event-emission channel.
                env = {key:os.environ[key] for key in ('PATH','LANG','LC_ALL') if key in os.environ}
                if os.name == 'nt':
                    for key in ('SystemRoot','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE'):
                        if key in os.environ:env[key]=os.environ[key]
                spawn = asyncio.create_subprocess_exec
                if os.name == 'nt':
                    from agent_harness._windows import spawn
                cell.process = await spawn(sys.executable, '-I', str(worker),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, env=env, limit=48*1024*1024)
                initial = {'code':code, 'catalog':[v['metadata'] for v in cell.catalog.values()],
                           'store':deepcopy(self.store)}
                cell.process.stdin.write((json.dumps(initial, ensure_ascii=True) + '\n').encode())
                await cell.process.stdin.drain()
                while True:
                    line = await cell.process.stdout.readline()
                    if not line:
                        raise RuntimeError('JavaScript worker exited without a result')
                    message = json.loads(line)
                    kind = message.get('type')
                    if kind == 'call':
                        task = asyncio.create_task(rpc(message))
                        pending.add(task)
                        def finished(completed):
                            pending.discard(completed)
                            if not completed.cancelled() and completed.exception() is not None:
                                self.trace('code_mode_bridge_error', {'cell_id':cell.cell_id,
                                    'error':str(completed.exception())})
                        task.add_done_callback(finished)
                    elif kind == 'output':
                        part = message['part']
                        ToolResult([part])  # Validate native content before retaining it.
                        cell.output.append(part)
                    elif kind == 'store':
                        candidate = {**self.store, message['key']:message['value']}
                        if len(json.dumps(candidate, ensure_ascii=False).encode()) > 4*1024*1024:
                            raise ValueError('Temporary store exceeds 4 MiB')
                        self.store = candidate
                    elif kind in {'completed','failed'}:
                        cell.status = kind
                        cell.error = message.get('error')
                        if any(not task.done() for task in pending):
                            cell.status = 'failed'
                            cell.error = 'Script ended with unawaited tool calls; dispatched effects may have unknown outcomes'
                        break
                    else:
                        raise ValueError('Invalid JavaScript worker message')
        except asyncio.CancelledError:
            cell.status, cell.error = 'cancelled', 'Execution stopped; already dispatched effects may have unknown outcomes'
        except Exception as error:
            cell.status, cell.error = 'failed', f'{type(error).__name__}: {error}'[:2000]
        finally:
            cell.outcome_unknown |= any(not task.done() for task in pending)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if cell.process is not None:
                if os.name == 'nt':
                    from agent_harness._windows import terminate_tree
                    await terminate_tree(cell.process)
                if cell.process.returncode is None:
                    try:
                        cell.process.kill()
                    except ProcessLookupError:
                        pass
                await cell.process.wait()
            cell.invocation = None
            cell.catalog.clear()  # Completed cells must not retain entire Step contexts/handlers.
            cell.done.set()
            self.trace('code_mode_finished', {'cell_id':cell.cell_id, 'status':cell.status,
                                            'error':cell.error, 'nested_calls':cell.calls,
                                            'outcome_unknown':cell.outcome_unknown})

    async def close(self):
        self.closed = True
        tasks = [cell.task for cell in self.cells.values() if not cell.done.is_set()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.store.clear()
