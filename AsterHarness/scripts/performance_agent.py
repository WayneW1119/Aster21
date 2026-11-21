"""Longer real-model acceptance run, with timestamped full traces and measurements."""

import asyncio
import csv
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agent_harness.config import load_env_file
from agent_harness.core.client import ResponsesClient
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.context_manager.memory import MemoryProjection
from agent_harness.render_trace import render


class TimedTrace:
    def __init__(self, path):
        self.path = path
        self.started = time.perf_counter()
        self.lock = threading.Lock()

    def __call__(self, event, data):
        with self.lock:
            row = {'event': event, **data,
                   'recorded_at': datetime.now(timezone.utc).isoformat(),
                   'elapsed_seconds': time.perf_counter() - self.started}
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')


def memory_snapshots_valid(events):
    """Check the separate baseline and consumed receipts against committed memory."""
    current, thread, requests = None, None, 0
    try:
        for event in events:
            if event['event'] == 'session_initialized' and event.get('thread_id') != thread:
                thread, current = event.get('thread_id'), None
            if event['event'] == 'memory_version_committed':
                current = event['memory']['current']
            if event['event'] != 'model_request':
                continue
            requests += 1
            prompt = event['prompt']
            items, memory = prompt['input'], prompt.get('memory')
            if not isinstance(memory, dict) or memory.get('role') != 'user':
                return False
            text = memory.get('text', '')
            if not text.startswith('<agent_memory>\n') or not text.endswith('\n</agent_memory>'):
                return False
            if any(item.get('text', '').startswith('<agent_memory>\n') for item in items):
                return False
            baseline = json.loads(text.splitlines()[-2])
            if current is None:
                current = baseline
            if MemoryProjection(baseline).reconstruct(items) != current:
                return False
            calls = {item['call_id'] for item in items if item.get('type') == 'function_call'
                     and item.get('name') in ('read_memory', 'write_memory', 'apply_memory_patch')}
            observed = sum(item.get('type') == 'function_call_output' and item.get('call_id') in calls
                           for item in items)
            if event['world_state']['sections']['memory']['observations'] != observed:
                return False
    except (KeyError, TypeError, ValueError, RuntimeError, AttributeError):
        return False
    return requests > 0


def expected_summary(rows):
    result = dict(valid_rows=0, invalid_rows=0, total_quantity=0, total_amount_cents=0, by_category={})
    for row in rows:
        category = row['category'].strip()
        quantity, price = row['quantity'], row['unit_cents']
        if not category or not quantity.isascii() or not quantity.isdecimal() or int(quantity) <= 0 or not price.isascii() or not price.isdecimal():
            result['invalid_rows'] += 1
            continue
        quantity, price = int(quantity), int(price)
        result['valid_rows'] += 1
        result['total_quantity'] += quantity
        result['total_amount_cents'] += quantity * price
        group = result['by_category'].setdefault(category, {'quantity': 0, 'amount_cents': 0})
        group['quantity'] += quantity
        group['amount_cents'] += quantity * price
    return result


def write_csv(path, rows):
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['category', 'quantity', 'unit_cents'])
        writer.writeheader()
        writer.writerows(rows)


def run_workspace_tests(workspace):
    # unittest does not recurse into a non-package tests/ on Python 3.11.
    start = workspace / 'tests' if (workspace / 'tests').is_dir() else workspace
    return subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(start), '-v'],
                          cwd=workspace, capture_output=True, text=True, timeout=30)


def independent_verification(workspace, run):
    checks = {}
    test_cases = {
        'original': list(csv.DictReader((workspace/'data/sales.csv').open(encoding='utf-8'))),
        'empty': [],
        'edge_cases': [dict(category=' 茶 ', quantity='002', unit_cents='0'),
                       dict(category='茶', quantity='7', unit_cents='119'),
                       dict(category='coffee', quantity='1', unit_cents='999999999'),
                       dict(category='x', quantity='0', unit_cents='2'),
                       dict(category='x', quantity='1.5', unit_cents='2'),
                       dict(category=' ', quantity='3', unit_cents='2'),
                       dict(category='x', quantity='2', unit_cents='-3'),
                       dict(category='x', quantity='-2', unit_cents='3'),
                       dict(category='x', quantity='1', unit_cents='abc')],
    }
    verification = run/'independent_checks'
    verification.mkdir()
    for name, rows in test_cases.items():
        source, output = verification/(name+'.csv'), verification/(name+'.json')
        write_csv(source, rows)
        result = subprocess.run([sys.executable, str(workspace/'report.py'), '--input', str(source), '--output', str(output)],
                                cwd=workspace, capture_output=True, text=True, timeout=15)
        actual = json.loads(output.read_text()) if output.exists() else None
        checks[name] = dict(passed=result.returncode == 0 and actual == expected_summary(rows),
                            returncode=result.returncode, expected=expected_summary(rows), actual=actual,
                            stdout=result.stdout, stderr=result.stderr)
    tests = run_workspace_tests(workspace)
    checks['agent_tests'] = dict(passed=tests.returncode == 0 and 'Ran 0 tests' not in tests.stderr,
                                stdout=tests.stdout, stderr=tests.stderr)
    (verification/'results.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
    return checks


async def main():
    run = ROOT/'test_runs'/(datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')+'-performance-'+uuid4().hex[:8])
    workspace = run/'workspace'
    (workspace/'data').mkdir(parents=True)
    (run/'traces').mkdir()
    print(f'测试目录：{run}', flush=True)
    marker = 'PERF-' + uuid4().hex[:16]
    boundary_marker = 'BOUNDARY-' + uuid4().hex[:16]
    rows = [dict(category=['book','coffee','tea','茶'][i % 4], quantity=str(i % 7 + 1), unit_cents=str(25 + (i*37) % 2000)) for i in range(240)]
    rows += [dict(category=c, quantity=q, unit_cents=p) for c,q,p in [('bad','0','2'),('bad','-1','3'),('bad','1.5','2'),(' ','1','2'),('bad','1','oops'),('bad','1','-2')]]
    write_csv(workspace/'data/sales.csv', rows)
    spec = '''# CSV 汇总器验收要求
用 Python 标准库实现 report.py，CLI: python report.py --input INPUT.csv --output OUTPUT.json。
输入 UTF-8 CSV 列为 category,quantity,unit_cents。category 去除首尾空白后不能为空。
quantity 和 unit_cents 必须由一个或多个 ASCII 数字组成；quantity > 0，unit_cents >= 0。
不合法的整行跳过，计入 invalid_rows；有效行计入 valid_rows。金额全程按整数分计算。
输出 JSON 恰有 valid_rows, invalid_rows, total_quantity, total_amount_cents, by_category 五个键。
by_category 是去空白后的类别映射，每个值恰有 quantity 和 amount_cents 两个键。
金额 = 数量乘单价。只有表头的 CSV 返回所有总计为 0、空 by_category。
应支持任意输入输出路径，同一输入多次执行结果一致。写至少 5 个 unittest 用例，包含边界和非法行。
当前 report.py 有缺陷，需要检查并修复。
'''
    (workspace/'SPEC.md').write_text(spec, encoding='utf-8')
    (workspace/'report.py').write_text('''import argparse, csv, json
p = argparse.ArgumentParser()
p.add_argument('--input', required=True)
p.add_argument('--output', required=True)
a = p.parse_args()
rows = list(csv.DictReader(open(a.input)))
json.dump({'total_quantity': sum(int(r['quantity']) for r in rows),
           'total_amount_cents': sum(int(r['unit_cents']) for r in rows)}, open(a.output, 'w'))
''', encoding='utf-8')
    (workspace/'emit_once.py').write_text('''import time
from agent_harness.events import emit_event

time.sleep(0.5)
receipt = emit_event('acceptance_signal', {'marker': 'program-ok', 'value': 42})
print(receipt, flush=True)
if receipt.get('status') != 'accepted':
    raise RuntimeError('Event rejected')
''', encoding='utf-8')
    prompts = [
        '这是一次稍长的真实验收。第一阶段：读取 SPEC.md 和现有 report.py，按需求修复；'
        '用 apply_patch 修改代码，写至少 5 个有意义的 unittest 测试，实际运行测试，再对 data/sales.csv 运行 CLI 生成 summary.json。'
        '遇到失败请根据实际输出修复，不能跳过非法行需求。完成后用 write_memory 保存项目实现要点与验收口令 '+marker+'，'
        '记忆中保留独立的一行 status: code-verified，后续阶段将用补丁修改它。'
        '最后 send_user_message 汇报真实测试与汇总结果。所有业务文件只放在当前工作区。',
        '第二阶段：检查真实事件驱动能力。请创建一个 12000ms 的一次性 timer（消息 timer-ok）。'
        '用 exec_command 启动一个约 3 秒后在当前工作区写入 async_done.txt（内容 async-ok）的 Python 命令，'
        'yield_time_ms=250、notify_on_completion=true，实际等待 command_completed 事件。'
        '另请先读取 emit_once.py，使用 test_program_generator 验证，再 register_program_generator(mode="once") 真正注册。'
        '收到实际 timer_fired、command_completed 和 program_event 后，核对后台文件内容、程序事件 marker=program-ok/value=42，'
        '再创建 event_checks.json，内容恰为 {"timer":true,"command":true,"program":true}，'
        '用 apply_memory_patch 将 index.md 的 status: code-verified 改为 status: events-verified，保留验收口令及其他内容，再 send_user_message 汇报。'
        '注意 self-test 回执不算真实 program_event。不要通过 sleep 或反复轮询工具等待事件；没有新事件时可先发进度消息，让调度器等待。',
        '第三阶段：现在已经关闭旧 Session 并恢复同一个 Thread。请核对持久记忆中保存的验收口令以及上阶段事实。'
        '读取必要文件，重新实际运行 unittest 和汇总 CLI；用 list_event_generators(include_finished=true) 检查恢复后的发生器状态，'
        '不要重新注册或重新执行先前的事件源。创建 restored.json，键为 marker（从持久记忆获取的原口令）、'
        'summary（当前 summary.json 完整对象）、events（event_checks.json 完整对象）。'
        '用 apply_memory_patch 将主记忆 status: events-verified 改为 status: resumed-verified，保留口令与其余内容。'
        '最后 send_user_message 汇报恢复和复验结果。',
        '第四阶段是暂停边界验收：本次响应只调用一次 apply_memory_patch，不调用其他工具，也不发消息。'
        '用 *** Update File: index.md 和 @@ 后仅有 + 行的 Codex 补丁，在主记忆末尾追加一行 boundary_marker: '+boundary_marker+'。'
        '不需要重写旧内容。测试控制器会在模型请求进行期间请求暂停，允许这次工具执行完，但把回执留在队列。',
        '第五阶段：已恢复 Thread，并开始消费暂停前已执行工具的回执。不要重复追加任何标记，不要重新执行旧工具。'
        'read_memory 核对 index.md 中上一阶段的 boundary_marker 恰好出现一次，status 仍为 resumed-verified，且原验收口令仍在。'
        '在工作区用 apply_patch 创建 boundary_verified.json，包含 marker（该 boundary_marker 的完整值）和 occurrences（实际出现次数）。'
        '最后 send_user_message 汇报恢复与核对结果。本阶段无需修改记忆。',
    ]
    for i, prompt in enumerate(prompts,1):
        (run/f'phase-{i}-task.txt').write_text(prompt, encoding='utf-8')
    for name in ('HARNESS_MODEL', 'HARNESS_RESPONSES_URL', 'OPENAI_API_KEY'):
        os.environ.pop(name, None)
    load_env_file(ROOT/'.env')
    manager = ThreadManager(run/'threads')
    # Explicit allowance for this multi-part acceptance task, not a production default change.
    configuration = SessionConfiguration(max_consecutive_steps=32)
    start = time.perf_counter()
    lag_samples = []
    async def monitor_loop():
        while True:
            now = time.perf_counter()
            await asyncio.sleep(0.05)
            lag_samples.append(max(0, time.perf_counter()-now-0.05))
    monitor = asyncio.create_task(monitor_loop())
    phases, thread_id, boundary = [], None, {}
    try:
        for i,prompt in enumerate(prompts,1):
            trace_path = run/'traces'/f'phase-{i}.jsonl'
            timed_trace = TimedTrace(trace_path)
            request_started = asyncio.Event()
            loop = asyncio.get_running_loop()
            def trace(event, data):
                timed_trace(event, data)
                if event == 'api_request':
                    loop.call_soon_threadsafe(request_started.set)
            client = ResponsesClient(os.environ['HARNESS_RESPONSES_URL'], os.environ['HARNESS_MODEL'], os.environ['OPENAI_API_KEY'], trace=trace)
            def send(text, phase=i):
                with (run/'messages.log').open('a', encoding='utf-8') as stream:
                    stream.write(f'\nPhase {phase}\n{text}\n')
            session = (manager.create(client, workspace, configuration=configuration, trace=trace, send_message=send) if thread_id is None
                       else manager.resume(client, thread_id, trace=trace, send_message=send))
            thread_id = session.thread_id
            phase_start = time.perf_counter()
            info = dict(phase=i, session_id=session.session_id, thread_id=thread_id,
                        initial_step=session.state.step_number, started_at=datetime.now().astimezone().isoformat())
            print(f'阶段 {i} 开始；Thread {thread_id}，起始 Step {session.state.step_number}', flush=True)
            try:
                if i == 5:
                    boundary['restored_pending_events'] = session.input_queue.snapshot()
                    boundary['restored_baseline'] = session.state.world_state_baseline.to_dict()
                await session.post_user_message(prompt)
                if i == 2:
                    worker = asyncio.create_task(session.run_until_settled())
                    try:
                        await asyncio.wait_for(request_started.wait(), timeout=15)
                        info['injected_user_message_during_request'] = not worker.done()
                        additional = '补充验收要求：保留记忆中的验收口令和已验证结果，继续完成本阶段的三类事件检查。'
                        (run/'phase-2-additional-message.txt').write_text(additional,encoding='utf-8')
                        await session.post_user_message(additional)
                        await asyncio.wait_for(worker, timeout=180)
                    finally:
                        if not worker.done():
                            worker.cancel()
                            await asyncio.gather(worker, return_exceptions=True)
                elif i == 4:
                    boundary['before_baseline'] = session.state.world_state_baseline.to_dict()
                    worker = asyncio.create_task(session.run_until_settled())
                    try:
                        await asyncio.wait_for(request_started.wait(), timeout=15)
                        boundary['suspend_while_running'] = session._running and not worker.done()
                        await session.suspend()
                        await asyncio.wait_for(worker, timeout=180)
                    finally:
                        if not worker.done():
                            worker.cancel()
                            await asyncio.gather(worker, return_exceptions=True)
                    boundary['pending_events'] = session.input_queue.snapshot()
                    boundary['after_baseline'] = session.state.world_state_baseline.to_dict()
                    boundary['disk_after_suspend'] = session.agent_context.read_memory('index.md')
                else:
                    await asyncio.wait_for(session.run_until_settled(), timeout=180)
                info['status'] = 'completed'
            except Exception as error:
                info.update(status='failed', error=repr(error), traceback=traceback.format_exc())
            finally:
                await session.close()
                info.update(seconds=time.perf_counter()-phase_start, final_step=session.state.step_number,
                            final_status=session.state.status)
                phases.append(info)
                (run/'phases.json').write_text(json.dumps(phases, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'阶段 {i}：{info["status"]}，{info["seconds"]:.2f}s，Step {info["initial_step"]} → {info["final_step"]}', flush=True)
            (run/f'phase-{i}-full.md').write_text(render(trace_path), encoding='utf-8')
            if info['status'] != 'completed':
                break
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
    runtime = time.perf_counter()-start
    checks = independent_verification(workspace, run)
    all_events = []
    requests, tool_times, queue_lags = [], [], []
    for path in sorted((run/'traces').glob('*.jsonl')):
        events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        all_events += events
        request, tools, queued = None, {}, {}
        for event in events:
            t, kind = event['elapsed_seconds'], event['event']
            if kind == 'api_request':
                request = dict(phase=path.stem, started=t, model=event['body']['model'], request_bytes=len(json.dumps(event['body']).encode()), input_items=len(event['body']['input']))
            elif kind == 'api_stream_event' and request is not None:
                request.setdefault('first_event_seconds', t-request['started'])
                if event.get('data',{}).get('type') in ('response.output_text.delta','response.function_call_arguments.delta','response.reasoning_text.delta'):
                    request.setdefault('first_delta_seconds', t-request['started'])
            elif kind == 'api_response' and request is not None:
                request.update(seconds=t-request['started'], usage=event['body'].get('usage',{}), status=event['body'].get('status'))
                requests.append(request)
                request = None
            elif kind == 'tool_started':
                tools[event['call']['call_id']] = (t,event['call']['name'])
            elif kind == 'tool_result':
                if event['result']['call_id'] in tools:
                    began,name=tools[event['result']['call_id']]
                    tool_times.append(dict(name=name, seconds=t-began, phase=path.stem))
            elif kind == 'event_enqueued':
                queued[event['queued_event']['event_id']] = t
            elif kind == 'step_started':
                for item in event['events']:
                    if item['event_id'] in queued:
                        queue_lags.append(dict(kind=item['kind'],seconds=t-queued[item['event_id']]))
    kinds = [event['queued_event']['kind'] for event in all_events if event['event']=='event_enqueued']
    checks['real_events'] = dict(passed=all(k in kinds for k in ('timer_fired','command_completed','program_event')), queued_kinds=kinds)
    try:
        event_checks=json.loads((workspace/'event_checks.json').read_text())
        checks['event_acknowledgement'] = dict(passed=event_checks==dict(timer=True,command=True,program=True))
        restored=json.loads((workspace/'restored.json').read_text())
        checks['resume_artifact'] = dict(passed=restored==dict(marker=marker,summary=expected_summary(rows),events=dict(timer=True,command=True,program=True)))
        checks['async_file'] = dict(passed=(workspace/'async_done.txt').read_text().strip()=='async-ok')
    except (OSError,ValueError) as error:
        checks['event_and_resume_files'] = dict(passed=False,error=str(error))
    checks['sessions'] = dict(passed=len(phases)==5 and all(p['status']=='completed' and p['final_status']=='stopped' for p in phases))
    checks['no_protocol_error'] = dict(passed=not any(e['event'] in ('protocol_error','tool_rejected','session_error') for e in all_events))
    checks['concurrent_user_input'] = dict(passed=any(p.get('injected_user_message_during_request') for p in phases))
    checks['transport_projection_exercised'] = dict(passed=any(
        e['event']=='api_transport' and e.get('mode')=='tool_outputs_before_input_messages' for e in all_events))
    api_requests = [e['body'] for e in all_events if e['event']=='api_request']
    checks['stable_request_prefixes'] = dict(passed=bool(api_requests) and all(
        after['input'][:len(before['input'])]==before['input']
        and after['instructions']==before['instructions'] and after['tools']==before['tools']
        for before,after in zip(api_requests,api_requests[1:])))
    checks['world_state_not_in_api'] = dict(passed=all(
        'world_state' not in body and all('world_state' not in item for item in body['input'])
        for body in api_requests))
    snapshots_valid = memory_snapshots_valid(all_events)
    checks['baseline_matches_consumed_memory_receipts'] = dict(passed=snapshots_valid and bool(api_requests))
    pending = boundary.get('pending_events',[])
    restored_pending = boundary.get('restored_pending_events',[])
    checks['pending_receipt_suspend_resume'] = dict(passed=(
        boundary.get('suspend_while_running',False) and len(pending)==1 and pending==restored_pending
        and pending[0]['kind']=='tool_result' and boundary.get('before_baseline')==boundary.get('after_baseline')
        and boundary.get('restored_baseline')==boundary.get('before_baseline')
        and boundary.get('disk_after_suspend','').count(boundary_marker)==1))
    try:
        boundary_output = json.loads((workspace/'boundary_verified.json').read_text())
        memory = session.agent_context.read_memory('index.md')
        checks['boundary_result'] = dict(passed=(boundary_output==dict(marker=boundary_marker,occurrences=1)
            and memory.count(boundary_marker)==1 and marker in memory and 'status: resumed-verified' in memory))
    except (OSError,ValueError) as error:
        checks['boundary_result'] = dict(passed=False,error=str(error))
    if pending:
        call_id = pending[0]['item'].get('call_id')
        rows = [json.loads(line) for line in session.rollout.path.read_text().splitlines()]
        recorded = [row['payload'] for row in rows if row['type']=='response_item']
        checks['boundary_tool_not_replayed'] = dict(passed=(
            sum(item.get('type')=='function_call' and item.get('call_id')==call_id for item in recorded)==1
            and sum(item.get('type')=='function_call_output' and item.get('call_id')==call_id for item in recorded)==1))
        (run/'boundary-check.json').write_text(json.dumps(boundary,ensure_ascii=False,indent=2),encoding='utf-8')
    tokens = {key:sum(r['usage'].get(key,0) for r in requests) for key in ('input_tokens','output_tokens','total_tokens')}
    tokens['cached_input_tokens'] = sum(r['usage'].get('input_tokens_details',{}).get('cached_tokens',0) for r in requests)
    api_seconds = [r['seconds'] for r in requests]
    def distribution(values):
        ordered=sorted(values)
        return dict(count=len(values),mean=statistics.mean(values),median=statistics.median(values),p95=ordered[max(0, int(len(ordered)*0.95+0.999)-1)],maximum=max(values)) if values else {}
    metrics = dict(passed=all(c['passed'] for c in checks.values()),model=os.environ['HARNESS_MODEL'],
                   max_consecutive_steps=configuration.max_consecutive_steps,
                   runtime_seconds=runtime,phases=phases,checks=checks,tokens=tokens,
                   api_seconds=distribution(api_seconds),api_total_seconds=sum(api_seconds),
                   first_delta_seconds=distribution([r['first_delta_seconds'] for r in requests if 'first_delta_seconds' in r]),
                   tool_seconds=distribution([r['seconds'] for r in tool_times]),
                   event_loop_lag_seconds=distribution(lag_samples),
                   queue_delay_seconds=queue_lags,requests=requests,tools=tool_times,
                   event_counts={kind:sum(e['event']==kind for e in all_events) for kind in sorted({e['event'] for e in all_events})})
    (run/'metrics.json').write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
    report=['# 较完整的真实 Agent 验收与性能记录','',
            f'模型：{metrics["model"]}；通过：{metrics["passed"]}；Agent 运行总耗时：{runtime:.2f}s。',
            '', '每阶段完整 Prompt、请求体、流事件、响应、历史和工具结果见 phase-N-full.md；原始记录在 traces/。',
            '本次 trace 额外记录 UTC 时间戳与 perf_counter 相对秒数。认证请求头未记录。',
            '首 delta 耗时指首次文本/函数参数/推理文本 delta；不是网络首字节时间。',
            'API 耗时包含网络、服务端、流传输与本地 trace 写入，不是纯模型推理耗时。',
            'Token 用量累加每次 API 报告值，包含重复发送的上下文；缓存命中数单列。',
            '这是单次顺序任务验收，不是并发压测或严格模型质量基准。','',
            '本次长任务显式配置连续 Step 上限为 32；生产默认值未修改。', '',
            '## 阶段','']
    report += [f'- 阶段 {p["phase"]}：{p["status"]}，{p["seconds"]:.2f}s，Step {p["initial_step"]} → {p["final_step"]}。' for p in phases]
    report += ['','## 独立校验','']+[f'- {name}：{value["passed"]}' for name,value in checks.items()]
    if requests:
        report += [
            '', '## 性能解读', '',
            f'- 共 {len(requests)} 次 API 请求、{len(tool_times)} 次工具调用；API 累计 {sum(api_seconds):.2f}s。',
            f'- 请求耗时：均值 {statistics.mean(api_seconds):.3f}s，中位数 {statistics.median(api_seconds):.3f}s，P95 {metrics["api_seconds"]["p95"]:.3f}s，最长 {max(api_seconds):.3f}s。',
            f'- 累计请求输入 {tokens["input_tokens"]:,} Token、输出 {tokens["output_tokens"]:,} Token；输入包含每步重复发送的上下文。',
            f'- 缓存输入 {tokens["cached_input_tokens"]:,} Token，占累计输入的 {tokens["cached_input_tokens"]/max(1,tokens["input_tokens"]):.1%}。',
            f'- 单次输入从 {requests[0]["usage"].get("input_tokens",0):,} 增长至 {requests[-1]["usage"].get("input_tokens",0):,} Token；驻留时间更长时，上下文持续增长值得优先关注。',
            f'- 工具调用累计等待 {sum(t["seconds"] for t in tool_times):.3f}s；后台命令在初次返回后的运行时间不计入该值。',
            '- 事件排队延迟包含等待当前 Step 模型响应完成的时间，不能当作事件循环阻塞时间。',
            '- 事件循环延迟采样覆盖记录写入与阶段间 Markdown 渲染，最大值不能单独归因于调度器。',
            '- 运行总耗时包含阶段间报告渲染，不包含之后的独立校验；所有日志与报告均完整保留。',
        ]
    report += ['','## 完整指标','', '```json',json.dumps(metrics,ensure_ascii=False,indent=2),'```']
    (run/'REPORT.md').write_text('\n'.join(report)+'\n',encoding='utf-8')
    print(json.dumps({k:metrics[k] for k in ('passed','runtime_seconds','tokens','api_seconds','first_delta_seconds','event_loop_lag_seconds','checks')},ensure_ascii=False,indent=2),flush=True)
    print('完整报告：'+str(run/'REPORT.md'),flush=True)
    return 0 if metrics['passed'] else 1


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
