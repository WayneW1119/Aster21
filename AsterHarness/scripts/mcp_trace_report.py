"""Lossless per-Step model I/O report with shared definitions before the process.

Only complete request/response bodies are compacted; raw JSONL traces (including
stream events) remain untouched. No authorization headers are read or exported.
"""

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Definitions:
    def __init__(self):
        self.values, self.index = {}, {}

    def ref(self, value):
        key = canonical(value)
        if key not in self.index:
            identifier = f"D{len(self.values) + 1:05}"
            self.index[key] = identifier
            self.values[identifier] = deepcopy(value)
        return self.index[key]


def pack(value, pool, fields):
    result = {"fields": deepcopy(value), "shared": {}}
    for name, is_sequence in fields.items():
        if name in result["fields"]:
            content = result["fields"].pop(name)
            result["shared"][name] = ({"sequence": [pool.ref(item) for item in content]}
                                      if is_sequence else {"definition": pool.ref(content)})
    return result


def unpack(value, definitions):
    result = deepcopy(value["fields"])
    for name, reference in value["shared"].items():
        result[name] = ([deepcopy(definitions[identifier]) for identifier in reference["sequence"]]
                        if "sequence" in reference else deepcopy(definitions[reference["definition"]]))
    return result


def fenced(value, language="json"):
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    longest = max((len(match.group()) for match in re.finditer(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    return fence + language + "\n" + content + "\n" + fence + "\n"


def trace_rows(path):
    with path.open() as stream:
        for line in stream:
            yield json.loads(line)


def build(run):
    pool, steps, observations, originals = Definitions(), [], [], []
    trace_files = sorted(run.glob("session-*.jsonl"))
    for path in trace_files:
        current = None
        for row in trace_rows(path):
            event = row["event"]
            if event == "step_started":
                current = {"trace": path.name, "session_id": row["session_id"], "step": row["step"],
                    "received_events": [pool.ref(item) for item in row["events"]], "execution": []}
                steps.append(current)
            elif event == "model_request" and current is not None:
                current["assembled_prompt"] = pack(row["prompt"], pool,
                    {"base_instructions": False, "input": True, "tools": False, "memory": False})
                current["world_state"] = pool.ref(row["world_state"])
            elif event == "api_request" and current is not None:
                current["endpoint"] = row["endpoint"]
                current["request"] = pack(row["body"], pool, {"instructions": False, "input": True, "tools": False})
                current["request_sha256"] = digest(row["body"])
                originals.append((current, "request", row["body"]))
            elif event == "api_response" and current is not None:
                # Responses providers may echo the full instructions/tool catalog.
                current["response"] = pack(row["body"], pool,
                    {"output": True, "instructions": False, "tools": False})
                current["response_sha256"] = digest(row["body"])
                originals.append((current, "response", row["body"]))
            elif event in {"tool_started", "tool_result", "history_compacted", "world_state_updated",
                           "memory_version_committed", "mcp_configuration_applied", "mcp_configuration_rejected",
                           "step_completed", "session_error", "protocol_warning", "user_message_sent"}:
                reference = pool.ref(row)
                if current is not None:
                    current["execution"].append(reference)
                else:
                    observations.append(reference)
            elif event in {"test_stage", "mcp_startup", "session_initialized", "session_stopped"}:
                observations.append(pool.ref(row))
    for step, kind, original in originals:
        assert unpack(step[kind], pool.values) == original, (step["step"], kind)
        assert digest(unpack(step[kind], pool.values)) == step[kind + "_sha256"]
    summary = json.loads((run / "result.json").read_text()) if (run / "result.json").exists() else {}
    result = {"format": "aster-model-io-with-definitions-v1", "summary": summary,
        "notes": ["definitions 在所有 Step 之前定义，每个引用替换为对应原始 JSON 值即可完整还原。",
                  "sequence 表示按原顺序拼接的数组；fields 中的值原样保留。",
                  "SHA-256 针对规范化 JSON 内容，不是网络报文的字节哈希。",
                  "assembled_prompt 是 Harness 组装值；request 是实际发送的 API body，包含记忆和传输排序。",
                  "response 是完成后的完整回复 body；流式中间事件见原始 session JSONL。",
                  "不会导出 Authorization 请求头或 .env；原始轨迹没有改写。"],
        "definitions": pool.values, "observations": observations, "steps": steps,
        "verification": {"bodies_reconstructed_exactly": len(originals),
                         "requests": sum(kind == "request" for _, kind, _ in originals),
                         "responses": sum(kind == "response" for _, kind, _ in originals),
                         "unshared_bodies_bytes": sum(len(canonical(value).encode()) for _, _, value in originals)}}
    target = run / "process.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    lines = ["# MCP 运行时来源变化：完整测试过程\n\n",
             "## 阅读方式\n\n每次组装输入、实际请求、完整回复与执行记录都在后文按 Step 给出。重复内容在前面的定义表只写一次，D 编号引用保持原始顺序。机器文件是 process.json，可用本脚本 expand 子命令完整展开；生成时已逐条与原始轨迹比对。原始 JSONL 保留全部流事件。\n\n",
             "## 验收与统计\n\n", fenced(summary), "\n## 公共与复用定义（完整原值）\n\n"]
    for identifier, value in pool.values.items():
        lines.extend([f"### {identifier}\n\n", fenced(value, "text" if isinstance(value, str) else "json"), "\n"])
    lines.extend(["## 启动、阶段与服务观察记录\n\n", fenced(observations),
                  "\n## 按 Step 的完整输入与输出\n\n"])
    for step in steps:
        lines.extend([f"### Step {step['step']} · {step['trace']}\n\n", fenced(step), "\n"])
    (run / "process.md").write_text("".join(lines))
    write_html(run / "process.html", result)
    return result


def write_html(path, report):
    """Standalone inspector: store shared data once, expand one Step on demand."""
    payload = json.dumps(report, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    template = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MCP 长任务 · 完整输入输出</title>
<style>
body{font:15px/1.6 system-ui,sans-serif;margin:0;background:#f6f7fa;color:#18202f}
main{max-width:1250px;margin:auto;padding:20px}h1{font-size:24px}nav{display:flex;gap:8px;flex-wrap:wrap;position:sticky;top:0;background:#f6f7fa;padding:12px 0}
button,select,input{padding:8px;border:1px solid #bdc6d4;border-radius:6px;background:white;font:inherit}
select{flex:1;min-width:240px}details{background:white;border:1px solid #dbe0e8;border-radius:8px;margin:12px 0;padding:12px}
summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.5 ui-monospace,monospace;max-height:75vh;overflow:auto}
.warning{background:#fff3da;border-left:4px solid #c38313;padding:12px}.hint{color:#58647a}table{border-collapse:collapse;width:100%}td,th{text-align:left;border-bottom:1px solid #dbe0e8;padding:7px}
</style><main><h1>MCP 长任务：完整过程</h1><div id="summary"></div>
<p class="hint">文件内包含完整数据，无外部脚本或网络请求。重复值只存一次；展开时还原原文。JSON 中的所有字段均保留，包括 instructions、工具 schema、推理条目及不透明内容。</p>
<details><summary>公共定义：按编号查阅完整原值</summary><input id="definition" placeholder="例如 D00001"><button id="lookup">查看</button><pre id="definition-value"></pre></details>
<nav><button id="prev">上一步</button><select id="steps"></select><button id="next">下一步</button></nav>
<div id="step"></div><details><summary>独立核验结果与统计</summary><pre id="verification"></pre></details>
</main><script id="data" type="application/json">__PAYLOAD__</script><script>
'use strict';
const data=JSON.parse(document.getElementById('data').textContent),defs=data.definitions,select=document.getElementById('steps');
const show=v=>JSON.stringify(v,null,2),get=id=>defs[id];
function expand(p){const value={...p.fields};for(const [key,r] of Object.entries(p.shared))value[key]=r.sequence?r.sequence.map(get):get(r.definition);return value}
function section(label,loader){const d=document.createElement('details'),s=document.createElement('summary');s.textContent=label;d.append(s);d.addEventListener('toggle',()=>{if(d.open&&!d.dataset.loaded){const pre=document.createElement('pre');pre.textContent=show(loader());d.append(pre);d.dataset.loaded='1'}});return d}
const summary=document.getElementById('summary'),r=data.summary;
const info=document.createElement('p');info.textContent=`${r.requests??data.steps.length} 个请求 · ${r.tool_calls??'?'} 次工具调用 · ${r.configuration_updates??'?'} 次运行时配置更新 · ${r.seconds??'?'} 秒`;summary.append(info);
if(r.passed===false){const warning=document.createElement('div');warning.className='warning';warning.textContent='这一轮未全通过。请查看独立核验与 report.md 中的问题；完整失败证据已保留。';summary.append(warning)}
data.steps.forEach((s,i)=>{const o=document.createElement('option');o.value=i;const change=s.execution.map(get).some(e=>e.event==='mcp_configuration_applied');o.textContent=`Step ${s.step} · ${s.trace}${change?' · 来源变化':''}`;select.append(o)});
function render(){const s=data.steps[Number(select.value)],root=document.getElementById('step');root.replaceChildren();if(!s)return;const h=document.createElement('h2');h.textContent=`Step ${s.step}`;root.append(h);
root.append(section('本 Step 收到的全部事件',()=>s.received_events.map(get)));
if(s.world_state)root.append(section('WorldState：本次来源及内部状态',()=>get(s.world_state)));
if(s.assembled_prompt)root.append(section('Harness 组装的完整 Prompt',()=>expand(s.assembled_prompt)));
if(s.request)root.append(section('实际 API 请求 body（完整）',()=>expand(s.request)));
if(s.response)root.append(section('API 完整回复 body',()=>expand(s.response)));
root.append(section('工具调用、回执、状态更新与执行记录',()=>s.execution.map(get)));
root.append(section('共享引用及 SHA-256',()=>s));}
select.onchange=render;document.getElementById('prev').onclick=()=>{select.value=Math.max(0,Number(select.value)-1);render()};document.getElementById('next').onclick=()=>{select.value=Math.min(data.steps.length-1,Number(select.value)+1);render()};
document.getElementById('lookup').onclick=()=>{const key=document.getElementById('definition').value.trim();document.getElementById('definition-value').textContent=Object.hasOwn(defs,key)?show(defs[key]):'没有这个定义编号'};
document.getElementById('verification').textContent=show({summary:r,verification:data.verification});render();
</script></html>'''
    path.write_text(template.replace("__PAYLOAD__", payload))


def expand(report, output):
    data = json.loads(report.read_text())
    with output.open("w") as stream:
        for step in data["steps"]:
            value = {"trace": step["trace"], "step": step["step"],
                "events": [data["definitions"][identifier] for identifier in step["received_events"]],
                "execution": [data["definitions"][identifier] for identifier in step["execution"]]}
            for kind in ("assembled_prompt", "request", "response"):
                if kind in step:
                    value[kind] = unpack(step[kind], data["definitions"])
                    if kind != "assembled_prompt":
                        assert digest(value[kind]) == step[kind + "_sha256"]
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("build")
    make.add_argument("run", type=Path)
    restore = sub.add_parser("expand")
    restore.add_argument("report", type=Path)
    restore.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        report = build(args.run)
        print(json.dumps(report["verification"]))
    else:
        expand(args.report, args.output)
