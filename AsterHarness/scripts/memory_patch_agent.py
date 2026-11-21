"""Exercise real memory patches, incremental history and Thread resume."""

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agent_harness.config import load_env_file
from agent_harness.core.client import ResponsesClient
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.render_trace import render
from performance_agent import TimedTrace


async def main():
    run = ROOT/'test_runs'/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-memory-patch-'+uuid4().hex[:8])
    workspace = run/'workspace'
    workspace.mkdir(parents=True)
    (run/'traces').mkdir()
    print(f'测试目录：{run}', flush=True)
    for name in ('HARNESS_MODEL','HARNESS_RESPONSES_URL','OPENAI_API_KEY'):
        os.environ.pop(name, None)
    load_env_file(ROOT/'.env')
    manager = ThreadManager(run/'threads')
    prompts = [
        '这是记忆补丁工具的真实验收。请先 read_memory 读取 index.md，再使用 apply_memory_patch '
        '将其默认的 No saved memory yet. 替换为三行：语言：中文、验收状态：pending、详情：topics/patch-check.md（每项单独一行）。'
        '同时创建记忆文件 topics/patch-check.md，包含 status: pending 和 unchanged-sentinel: KEEP-7D14 两行。'
        '观察成功回执后，再调用 apply_memory_patch，只将两份文件中 pending 改为 verified，保留其他内容。'
        '必须实际通过这个新工具局部修改，不使用 write_memory 或 shell 重写。最后 read_memory 分别读取两份文件确认，'
        '通过 send_user_message 报告实际结果。所有这些文件都在私有记忆目录，工具路径不用 memory/ 前缀。',
        '现在已恢复同一个 Thread。先核对记忆，再使用 apply_memory_patch 仅将 index.md 的 '
        '验收状态：verified 改成 验收状态：resumed；保留语言、详情路径和详细记忆文件。'
        'read_memory 确认后用 send_user_message 汇报。不要用完整文件重写，也不要重新创建旧记忆。',
    ]
    thread_id, phases, messages = None, [], []
    checks = {}
    for i,prompt in enumerate(prompts,1):
        trace_path = run/'traces'/f'phase-{i}.jsonl'
        trace = TimedTrace(trace_path)
        client = ResponsesClient(os.environ['HARNESS_RESPONSES_URL'],os.environ['HARNESS_MODEL'],os.environ['OPENAI_API_KEY'],trace=trace)
        session = (manager.create(client, workspace, trace=trace, send_message=messages.append)
                   if thread_id is None else manager.resume(client,thread_id,trace=trace,send_message=messages.append))
        thread_id = session.thread_id
        (run/f'phase-{i}-task.txt').write_text(prompt,encoding='utf-8')
        phase = {'phase':i,'initial_step':session.state.step_number,'status':'running'}
        try:
            await session.post_user_message(prompt)
            await asyncio.wait_for(session.run_until_settled(),timeout=120)
            phase['status']='completed'
        except Exception as error:
            phase.update(status='failed',error=repr(error))
        finally:
            await session.close()
            phase['final_step']=session.state.step_number
            phases.append(phase)
        (run/f'phase-{i}-full.md').write_text(render(trace_path),encoding='utf-8')
        index=session.agent_context.read_memory('index.md')
        detail_path=session.agent_context.memory_root/'topics/patch-check.md'
        detail=detail_path.read_text(encoding='utf-8') if detail_path.exists() else ''
        checks[f'phase_{i}_files']=all(value in index for value in (
            '语言：中文','验收状态：'+('verified' if i==1 else 'resumed'),'详情：topics/patch-check.md')) and (
            'status: verified' in detail and 'unchanged-sentinel: KEEP-7D14' in detail)
        print(json.dumps(phase,ensure_ascii=False),flush=True)
        if phase['status']!='completed':
            break
    events=[json.loads(line) for path in sorted((run/'traces').glob('*.jsonl')) for line in path.read_text().splitlines()]
    requests=[e['body'] for e in events if e['event']=='api_request']
    checks['request_prefixes_stable']=bool(requests) and all(
        nxt['input'][:len(prev['input'])]==prev['input'] and nxt['instructions']==prev['instructions'] and nxt['tools']==prev['tools']
        for prev,nxt in zip(requests,requests[1:]))
    calls=[e['call'] for e in events if e['event']=='tool_started']
    checks['patch_tool_used']=sum(c['name']=='apply_memory_patch' for c in calls)>=3
    checks['no_full_rewrites']=not any(c['name'] in ('write_memory','exec_command','apply_patch') for c in calls)
    checks['both_sessions_completed']=len(phases)==2 and all(p['status']=='completed' for p in phases)
    checks['messages_sent']=len(messages)>=2
    checks['no_runtime_errors']=not any(e['event'] in ('session_error','tool_rejected','protocol_error') for e in events)
    initial=[i for i in session.state.history.snapshot() if i.get('text','').startswith('<agent_memory>\n')]
    checks['single_initial_snapshot']=len(initial)==1 and 'No saved memory yet.' in initial[0]['text']
    rollout=[json.loads(line) for line in session.rollout.path.read_text().splitlines()]
    snapshots=[row['payload'] for row in rollout if row['type']=='world_state']
    checks['world_state_persisted']=bool(snapshots) and snapshots[-1]==session.state.world_state_baseline.to_dict()
    checks['world_state_not_sent_to_api']=all(
        'world_state' not in body and all('world_state' not in item for item in body['input'])
        for body in requests)
    checks['only_patch_receipts']=all(
        next(e['result']['output'] for e in events if e['event']=='tool_result' and e['result']['call_id']==c['call_id']).startswith('Success. Updated the following memory files:\n')
        for c in calls if c['name']=='apply_memory_patch')
    report=dict(passed=all(checks.values()),checks=checks,phases=phases,model_requests=len(requests),
                tools=[c['name'] for c in calls],messages=messages,
                usage=[e['body'].get('usage',{}) for e in events if e['event']=='api_response'])
    (run/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (run/'REPORT.md').write_text('# 真实记忆补丁验收\n\n每阶段完整请求、响应及工具过程见 phase-N-full.md，原始数据在 traces/。\n\n```json\n'+json.dumps(report,ensure_ascii=False,indent=2)+'\n```\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    print('完整报告：'+str(run/'REPORT.md'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
