"""Run a real-model smoke task and retain the full trace under test_runs/."""

import csv
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    run = ROOT / 'test_runs' / (datetime.now().astimezone().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8])
    workspace = run / 'workspace'
    workspace.mkdir(parents=True)
    (workspace / 'sales.csv').write_text(
        'item,quantity,unit_price\napple,3,4\npear,5,6\norange,2,7\n', encoding='utf-8')
    prompt = (
        '请完成一个小任务，所有任务文件都在当前 workspace 中。先用 read_file 读取 sales.csv，'
        '然后使用 apply_patch 创建 summarize.py：用 Python 标准库 csv 读取 sales.csv，'
        '计算总数量和总金额，将结果写入 summary.json，键为 total_quantity 和 total_amount。'
        '再用 exec_command 实际运行这个脚本，并用 Python 断言输出等于手算的期望值。'
        '看到命令执行成功后，通过 send_user_message 用中文报告结果。'
        '不要只给出代码，实际创建文件并运行验证。'
    )
    (run / 'task.txt').write_text(prompt, encoding='utf-8')
    command = [sys.executable, '-m', 'agent_harness', '--workspace', str(workspace),
               '--thread-dir', str(run / 'threads'), '--trace-dir', str(run / 'traces'),
               '--prompt', prompt]
    metadata = {'started_at': datetime.now().astimezone().isoformat(),
                'cwd': str(ROOT), 'command': command}
    (run / 'run.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'测试目录：{run}', flush=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    # This check explicitly uses the project's .env, without recording secrets.
    for name in ('HARNESS_MODEL', 'HARNESS_RESPONSES_URL', 'OPENAI_API_KEY'):
        env.pop(name, None)
    with (run / 'stdout.log').open('w', encoding='utf-8') as out, (run / 'stderr.log').open('w', encoding='utf-8') as err:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=out, stderr=err)
    metadata.update(ended_at=datetime.now().astimezone().isoformat(), returncode=result.returncode)
    traces = sorted((run / 'traces').glob('*.jsonl'))
    for trace in traces:
        with (run / 'render.log').open('a', encoding='utf-8') as log:
            rendered = subprocess.run([sys.executable, '-m', 'agent_harness.render_trace',
                                       str(trace), '--output', str(run / (trace.stem + '.md'))],
                                      cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        if rendered.returncode:
            metadata['render_error'] = True
    events = [json.loads(line) for trace in traces for line in trace.read_text(encoding='utf-8').splitlines() if line.strip()]
    checks = {'cli_exit_zero': result.returncode == 0,
              'read_file_called': False, 'apply_patch_called': False, 'exec_command_called': False,
              'command_exit_zero': False, 'user_message_sent': False,
              'tool_result_returned_to_model': False, 'summary_correct': False,
              'clean_close': False, 'readable_record_created': bool(traces) and not metadata.get('render_error', False)}
    for event in events:
        if event['event'] == 'tool_started':
            name = event['call']['name']
            if name + '_called' in checks:
                checks[name + '_called'] = True
            if name == 'exec_command':
                call_id = event['call']['call_id']
                for following in events:
                    if following['event'] == 'tool_result' and following['result']['call_id'] == call_id:
                        try:
                            checks['command_exit_zero'] |= json.loads(following['result']['output']).get('exit_code') == 0
                        except (ValueError, AttributeError):
                            pass
        if event['event'] == 'user_message_sent':
            checks['user_message_sent'] = True
        if event['event'] == 'model_request':
            checks['tool_result_returned_to_model'] |= any(
                item.get('type') == 'function_call_output' for item in event['prompt']['input'])
    try:
        with (workspace / 'sales.csv').open(encoding='utf-8', newline='') as stream:
            rows = list(csv.DictReader(stream))
        expected = {'total_quantity': sum(int(r['quantity']) for r in rows),
                    'total_amount': sum(int(r['quantity']) * int(r['unit_price']) for r in rows)}
        actual = json.loads((workspace / 'summary.json').read_text(encoding='utf-8'))
        checks['summary_correct'] = actual == expected
        metadata.update(expected=expected, actual=actual)
    except (OSError, ValueError) as error:
        metadata['verification_error'] = str(error)
    for path in (run / 'threads').glob('*/rollout.jsonl'):
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        checks['clean_close'] |= bool(rows and rows[-1].get('type') == 'session_closed' and rows[-1].get('payload', {}).get('clean'))
    metadata.update(checks=checks, passed=all(checks.values()),
                    model_requests=sum(e['event'] == 'api_request' for e in events),
                    tools=[e['call']['name'] for e in events if e['event'] == 'tool_started'])
    (run / 'run.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(metadata, ensure_ascii=False, indent=2), flush=True)
    return 0 if metadata['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
