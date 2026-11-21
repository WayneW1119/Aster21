"""Lossless API transcript: fixed request fields expanded once, dynamic input in full."""

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .render_trace import block


def encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def pack_body(value, definitions):
    body, refs = deepcopy(value), {}
    for key in ('instructions', 'tools'):
        if key in body:
            fixed = body.pop(key)
            canonical = json.dumps(fixed, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            reference = key + '-' + digest
            definitions[reference] = fixed
            refs[key] = reference
    return {'body': body, 'fixed_refs': refs}


def pack_requests(rows):
    """Return an exactly reversible request dictionary; never summarize wire input."""
    definitions, requests = {}, []
    for row in rows:
        if row['event'] != 'api_request':
            continue
        packed = pack_body(row['body'], definitions)
        packed['request_metadata'] = {key: deepcopy(value) for key, value in row.items()
                                      if key not in ('event', 'body')}
        requests.append(packed)
    return definitions, requests


def unpack_request(definitions, request):
    body = deepcopy(request['body'])
    for field, reference in request['fixed_refs'].items():
        body[field] = deepcopy(definitions[reference])
    return body


def render_compact(sources):
    grouped = [(p, [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()])
               for p in sources]
    rows = [row for _, entries in grouped for row in entries]
    definitions, requests = pack_requests(rows)
    # Compatible gateways often echo the whole instructions/tools in responses.
    # These must use the same dictionary too, otherwise fixed text repeats anyway.
    for row in rows:
        if row['event'] == 'api_response':
            pack_body(row['body'], definitions)
    result = ['# 完整输入输出记录（固定字段去重版）\n',
              f'共 {len(requests)} 次实际 API 请求、'
              f'{sum(row["event"] == "api_response" for row in rows)} 次完整响应。'
              '失败而未收到响应时保留实际错误，不补造输出。\n',
              '每次实际 API 请求的 input 与其他参数、每次 API 响应均完整保留。'
              'instructions 和 tools 按内容寻址：相同内容只展开一次，变化后新增定义。'
              '将 fixed_refs 的值按字段名放回 body，即可还原该请求/响应的完整 JSON。'
              '不是输入差分，也不是对历史做摘要。API 认证头不记录。\n',
              '为避免同一输入输出重复展示，省去内部 model_request/model_response、'
              '完整历史快照和逐 token 传输事件；原始 JSONL 仍保留全部日志。'
              '工具回执和调度/发生器事件另行完整列出，包含末次未触发下一请求的回执。\n',
              '## 固定定义\n']
    for reference, value in definitions.items():
        result.extend([f'### {reference}\n', block(value if isinstance(value, str) else encode(value),
                                                'text' if isinstance(value, str) else 'json')])
    request_index = 0
    omitted = {'history_updated', 'history_recorded', 'world_state_updated',
               'model_request', 'model_response', 'api_stream_event', 'step_context'}
    for source, entries in grouped:
        result.append(f'## 阶段：{source}\n')
        step = 0
        for row in entries:
            step = row.get('step', step)
            event = row['event']
            if event in omitted:
                continue
            if event == 'api_request':
                request_index += 1
                result.extend([f'### 请求 {request_index} · Step {step}\n',
                               f'<details><summary>完整输入：{len(row["body"].get("input", []))} 个 wire items</summary>\n',
                               block(encode(requests[request_index - 1]), 'json'), '</details>\n'])
            elif event == 'api_response':
                calls = [item.get('name') for item in row['body'].get('output', [])
                         if item.get('type') == 'function_call']
                result.extend([f'输出工具调用：{", ".join(calls) or "无"}。\n',
                               f'<details><summary>请求 {request_index}：完整输出</summary>\n',
                               block(encode({'event': event, **pack_body(row['body'], definitions),
                                            'response_metadata': {k: v for k, v in row.items()
                                                                  if k not in ('event', 'body')}}), 'json'),
                               '</details>\n'])
            else:
                result.extend([f'<details><summary>Step {step} · {event}</summary>\n',
                               block(encode(row), 'json'), '</details>\n'])
    return '\n'.join(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sources', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with args.output.open('x', encoding='utf-8') as stream:
        stream.write(render_compact(args.sources))
    print(args.output)


if __name__ == '__main__':
    main()
