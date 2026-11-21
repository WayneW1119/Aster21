"""Legacy v1 archive/effective pagination acceptance, kept as a historical script.

Current advertised tools use numbered/stable target addressing. Use
scripts/navigation_check.py for current live acceptance. Legacy Python handlers
remain compatible, but these old model-facing prompts are no longer current.
"""

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
    run = ROOT/'test_runs'/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-history-query-agent-'+uuid4().hex[:8])
    workspace = run/'workspace'
    workspace.mkdir(parents=True)
    (run/'traces').mkdir()
    print(f'测试目录：{run}', flush=True)
    for name in ('HARNESS_MODEL', 'HARNESS_RESPONSES_URL', 'OPENAI_API_KEY'):
        os.environ.pop(name, None)
    load_env_file(ROOT/'.env')
    manager = ThreadManager(run/'threads')
    checks, messages, phases, all_events = {}, [], [], []
    thread_id, cursor, seed_id, initial_ids = None, None, None, []
    marker = 'archive-marker-'+uuid4().hex
    for phase in (1, 2):
        trace_path = run/'traces'/f'phase-{phase}.jsonl'
        trace = TimedTrace(trace_path)
        client = ResponsesClient(os.environ['HARNESS_RESPONSES_URL'], os.environ['HARNESS_MODEL'],
                                 os.environ['OPENAI_API_KEY'], trace=trace)
        session = (manager.create(client, workspace, trace=trace, send_message=messages.append)
                   if phase == 1 else manager.resume(client, thread_id, trace=trace, send_message=messages.append))
        thread_id = session.thread_id
        if phase == 1:
            session.record_items([{'type':'message', 'role':'user', 'text':marker}])
            seed_id = session.state.history.ids()[-1]
            prompt = ('这是历史查询工具验收。请实际调用 query_history 两次：分别查询 archive 和 effective，'
                      f'两次都使用 start_id="{seed_id}"、cursor=null、limit=1、max_chars=1024。'
                      '观察回执，确认两份条目的 ID 和原文一致。不要继续翻页，也不要调用其他工具；'
                      '最后通过 send_user_message 汇报查到的标记和 ID。')
        else:
            checks['ids_survive_resume'] = session.state.history.ids()[:len(initial_ids)] == initial_ids
            prompt = ('继续历史查询验收。请实际调用一次 query_history，view="archive"、start_id=null、'
                      f'cursor={json.dumps(cursor)}、limit=50、max_chars=24000。'
                      '这是上次查询留下的游标。确认恢复后仍能继续读取固定快照，'
                      '并通过 send_user_message 汇报 snapshot_revision、返回条目数量和 next_cursor 是否为空。'
                      '只需这一次查询和最后发消息。')
        (run/f'phase-{phase}-task.txt').write_text(prompt, encoding='utf-8')
        status = {'phase':phase, 'status':'running'}
        try:
            await session.post_user_message(prompt)
            await asyncio.wait_for(session.run_until_settled(), timeout=120)
            status['status'] = 'completed'
        except Exception as error:
            status.update(status='failed', error=repr(error))
        finally:
            await session.close()
        phases.append(status)
        (run/f'phase-{phase}-full.md').write_text(render(trace_path), encoding='utf-8')
        events = [json.loads(line) for line in trace_path.read_text().splitlines()]
        all_events.extend(events)
        calls = [e['call'] for e in events if e['event']=='tool_started' and e['call']['name']=='query_history']
        results = {e['result']['call_id']:e['result']['output'] for e in events if e['event']=='tool_result'}
        pages = [(json.loads(c['arguments']), json.loads(results[c['call_id']])) for c in calls]
        if phase == 1:
            archive = next((p for a,p in pages if a['view']=='archive'), {})
            effective = next((p for a,p in pages if a['view']=='effective'), {})
            checks['both_views_queried'] = len(calls)==2 and bool(archive) and bool(effective)
            checks['same_seed_item'] = (archive.get('entries')==effective.get('entries') and
                bool(archive.get('entries')) and archive['entries'][0]['id']==seed_id and
                json.loads(archive['entries'][0]['item_json'])['text']==marker)
            cursor = archive.get('next_cursor')
            revision = archive.get('snapshot_revision')
            checks['continuation_created'] = isinstance(cursor, str)
            initial_ids = session.state.history.ids()
        else:
            checks['saved_cursor_used'] = len(pages)==1 and pages[0][0]['cursor']==cursor
            page = pages[0][1] if pages else {}
            checks['fixed_snapshot_continued'] = (page.get('snapshot_revision')==revision and
                page.get('next_cursor') is None and bool(page.get('entries')))
            if page.get('entries'):
                expected_ids = initial_ids[initial_ids.index(seed_id)+1:revision]
                checks['continuation_exact_ids'] = [e['id'] for e in page['entries']]==expected_ids
        checks[f'phase_{phase}_bounded'] = all(len(results[c['call_id']])<=json.loads(c['arguments'])['max_chars'] for c in calls)
        print(json.dumps(status, ensure_ascii=False), flush=True)
        if status['status']!='completed' or (phase==1 and not cursor):
            break
    bodies = [e['body'] for e in all_events if e['event']=='api_request']
    checks['completed'] = len(phases)==2 and all(p['status']=='completed' for p in phases)
    checks['messages_sent'] = len(messages)==2 and marker in messages[0]
    checks['no_errors'] = not any(e['event']=='session_error' for e in all_events)
    checks['metadata_not_in_api_items'] = all('history_id' not in item and 'revision' not in item
        for body in bodies for item in body['input'])
    report = {'passed':all(checks.values()), 'checks':checks, 'phases':phases,
              'model_requests':len(bodies), 'messages':messages}
    (run/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (run/'REPORT.md').write_text('# 历史查询真实模型验收\n\n完整请求、响应、工具调用见 phase-N-full.md 和 traces/。\n\n```json\n'+
        json.dumps(report, ensure_ascii=False, indent=2)+'\n```\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print('完整报告：'+str(run/'REPORT.md'), flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
