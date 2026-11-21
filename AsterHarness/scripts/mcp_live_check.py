#!/usr/bin/env python3
"""Real-model acceptance against separately installed, published MCP servers.

Use --run pointing at an isolated test_runs directory containing setup.json.
--live explicitly enables billable model calls. Never attaches to the resident.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_harness.config import load_env_file
from agent_harness.core.client import ResponsesClient
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.trace import JsonlTrace


class RecordingTrace:
    def __init__(self, path):
        self.sink = JsonlTrace(path)
        self.rows = []
        self.lock = threading.Lock()

    def __call__(self, event, data):
        with self.lock:
            self.sink(event, data)
            self.rows.append({"event": event, **data})
            if event == "tool_started":
                print(f"step={data['step']} tool={data['call'].get('name')}", flush=True)
            elif event == "api_response":
                usage = data["body"].get("usage", {})
                print(f"response input={usage.get('input_tokens')} output={usage.get('output_tokens')}", flush=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def service_state():
    return subprocess.check_output(["systemctl", "--user", "show", "aster-chat", "aster-gateway",
        "--property=Id", "--property=MainPID", "--property=ActiveState"], text=True)


async def check(run, env_file):
    assert run.is_relative_to(ROOT / "test_runs"), "Acceptance must run below project test_runs"
    setup = json.loads((run / "setup.json").read_text())
    workspace = run / "workspace"
    workspace.mkdir(exist_ok=True)
    data_root = workspace / "mcp-files"
    data_root.mkdir(exist_ok=True)
    source = data_root / "inventory.json"
    assert not source.exists(), "Do not overwrite a previous acceptance; use a fresh directory"
    inventory = {"items": [{"sku": "A", "stock": 12, "reserved": 3},
                           {"sku": "B", "stock": 8, "reserved": 2},
                           {"sku": "C", "stock": 5, "reserved": 1}]}
    source.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n")
    original_hash = digest(source)
    servers = {
        "filesystem": {"command": setup["node_path"],
            "args": [str(run / "tools/filesystem/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js"),
                     str(data_root)], "required": True, "startup_timeout_sec": 60,
            "tool_timeout_sec": 30,
            "enabled_tools": ["list_allowed_directories", "list_directory", "read_text_file",
                "write_file", "edit_file", "create_directory", "search_files"]},
        "fetch": {"command": str(run / "tools/fetch-venv/bin/python"),
            "args": ["-m", "mcp_server_fetch"], "required": True,
            "startup_timeout_sec": 60, "tool_timeout_sec": 60, "enabled_tools": ["fetch"]}}
    config = SessionConfiguration(max_consecutive_steps=40, mcp_servers=servers)
    (run / "mcp-config.json").write_text(json.dumps(servers, indent=2) + "\n")
    toml = []
    for name, server in servers.items():
        toml.append(f"[mcp_servers.{name}]")
        toml.extend(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in server.items())
        toml.append("")
    (run / "mcp-config.toml").write_text("\n".join(toml))
    load_env_file(env_file)
    traces, sessions = [], []
    report = {"setup": setup, "checks": {}, "stages": [], "mobile_before": service_state()}
    (run / "mobile-instance.sha256").write_text(digest(ROOT / ".harness/resident/mobile/instance.json"))
    manager = ThreadManager(run / "threads")

    def client(trace):
        return ResponsesClient(os.environ["HARNESS_RESPONSES_URL"], os.environ["HARNESS_MODEL"],
                               os.environ["OPENAI_API_KEY"], allow_http=True, trace=trace)

    def trace(name):
        item = RecordingTrace(run / (name + ".jsonl"))
        traces.append(item)
        return item

    rules = (f"这是独立的真实 MCP 验收。工作目录 {workspace}，文件数据目录 {data_root}。"
        "不要访问其他目录、凭据或其他 Agent，不要更改任何服务配置。"
        "文件读取和修改必须通过 mcp__filesystem__ 开头的工具完成；"
        "网页读取通过 mcp__fetch__fetch 完成，不要用 exec_command 或内置文件工具绕过 MCP。"
        "工具失败可以根据真实回执修正；不要把失败当作成功。任务完成后用 send_user_message"
        "简短报告（wake_model=false），然后等待，不做额外工作。\n")

    async def drive(session, name, prompt=None):
        started = time.monotonic()
        if prompt is not None:
            await session.post_user_message(rules + prompt)
        await asyncio.wait_for(session.run_until_idle(), timeout=450)
        report["stages"].append({"stage": name, "seconds": round(time.monotonic() - started, 3),
                                 "step": session.state.step_number, "status": session.state.status})
        print(f"stage={name} finished step={session.state.step_number}", flush=True)

    def verify_inventory(expected):
        summary = json.loads((data_root / "summary.json").read_text())
        assert summary["total_available"] == sum(expected.values()), summary
        assert {row["sku"]: row["available"] for row in summary["items"]} == expected, summary
        assert digest(source) == original_hash, "Original inventory changed"

    try:
        first_trace = trace("phase1-filesystem")
        first = manager.create(client(first_trace), workspace, configuration=config, trace=first_trace)
        sessions.append(first)
        report["thread_id"] = first.thread_id
        await drive(first, "filesystem", "先检查 MCP 文件服务允许的目录和目录内容。"
            "故意读取一次不存在的 missing-for-test.txt，观察错误后继续。读取 inventory.json，"
            "逐项计算 available=stock-reserved，生成 summary.json，格式为"
            '{"items":[{"sku":"A","available":数字},...],"total_available":数字}。'
            "另写 summary.md，初始标题为“库存初检”，写入各项计算结果；然后必须使用"
            "MCP edit_file 把标题改为“库存核对记录”，再读取两个输出文件核实。"
            "写 audit.txt，内容恰好为 phase1 加一个换行。保留 inventory.json 原文。")
        verify_inventory({"A": 9, "B": 6, "C": 4})
        assert "库存核对记录" in (data_root / "summary.md").read_text()
        assert (data_root / "audit.txt").read_text() == "phase1\n"
        report["checks"]["filesystem_outputs_independently_verified"] = True

        await drive(first, "fetch", "用 Fetch MCP 读取 https://example.com/ 的正文，"
            "然后再次用 start_index=100 分页读取。再请求一次"
            " https://www.python.org/this-page-does-not-exist-aster-mcp-check ，观察实际错误。"
            "用 MCP 文件工具写 web-notes.md，记录正文要点（未返回标题就说明）、两次分页是否成功、"
            "失败请求的真实原因及来源 URL，读取笔记确认。")
        notes = (data_root / "web-notes.md").read_text()
        assert "example.com" in notes and any(word in notes for word in ("documentation", "文档", "示例")), notes
        report["checks"]["web_notes_independently_verified"] = True

        await first.post_user_message(rules + "这是暂停前已入队、需要恢复后处理的任务："
            "读取 corrections.json（外部会在暂停期间写好），按其中 reserved 修正值重新计算"
            " summary.json 和 summary.md，但不要修改 inventory.json。读取 audit.txt，"
            "在原文后追加且只追加一行 resume。核实结果后报告。")
        pending = first.input_queue.snapshot()
        step = first.state.step_number
        await first.suspend()
        prefix = first.rollout.path.read_bytes()
        (data_root / "corrections.json").write_text('{"sku":"B","reserved":3}\n')
        last_trace = trace("phase3-resume")
        last = manager.resume(client(last_trace), first.thread_id, trace=last_trace)
        sessions.append(last)
        assert last.state.step_number == step
        assert last.input_queue.snapshot() == pending
        assert last.rollout.path.read_bytes().startswith(prefix)
        assert not any(row["event"] == "api_request" for row in last_trace.rows)
        await drive(last, "resume-correction")
        verify_inventory({"A": 9, "B": 5, "C": 4})
        assert (data_root / "audit.txt").read_text() == "phase1\nresume\n"
        await last.suspend()
        report["checks"]["same_thread_pending_input_and_resume"] = True
        report["checks"]["old_tool_effects_not_replayed"] = True
    finally:
        for session in reversed(sessions):
            await session.close()
        rows = [row for item in traces for row in item.rows]
        calls = [row["call"] for row in rows if row["event"] == "tool_started"]
        receipts = {row["result"]["call_id"]: row["result"]["output"]
                    for row in rows if row["event"] == "tool_result"}
        mcp_calls = [call for call in calls if call.get("name", "").startswith("mcp__")]
        successful, failed = [], []
        for item in mcp_calls:
            body = json.loads(receipts.get(item["call_id"], '{"isError":true}'))
            (failed if body.get("isError") or body.get("error") else successful).append(item["name"])
        usage = [row["body"].get("usage", {}) for row in rows if row["event"] == "api_response"]
        report.update({"model": os.environ.get("HARNESS_MODEL"), "model_requests":
            sum(row["event"] == "api_request" for row in rows), "mcp_calls": len(mcp_calls),
            "mcp_successes": successful, "mcp_errors": failed,
            "input_tokens": sum(item.get("input_tokens", 0) for item in usage),
            "output_tokens": sum(item.get("output_tokens", 0) for item in usage),
            "cached_tokens": sum(item.get("input_tokens_details", {}).get("cached_tokens", 0) for item in usage),
            "mobile_after": service_state(), "non_mcp_tools": sorted(set(call["name"] for call in calls
                if not call["name"].startswith("mcp__")))})
        report["checks"]["mobile_services_not_restarted"] = report["mobile_before"] == report["mobile_after"]
        report["checks"]["mobile_instance_config_unchanged"] = digest(
            ROOT / ".harness/resident/mobile/instance.json") == (run / "mobile-instance.sha256").read_text()
        report["checks"]["no_file_tool_bypass"] = not any(call["name"] in {
            "exec_command", "read_file", "write_file", "list_directory", "apply_patch"} for call in calls)
        report["checks"]["both_published_services_used"] = any(name.startswith("mcp__fetch__") for name in successful) and any(
            name.startswith("mcp__filesystem__") for name in successful)
        fetch_pages = [json.loads(item["arguments"]).get("start_index", 0) for item in mcp_calls
            if item["name"] == "mcp__fetch__fetch"
            and "example.com" in json.loads(item["arguments"]).get("url", "")]
        report["checks"]["fetch_pagination_requested"] = 0 in fetch_pages and 100 in fetch_pages
        report["checks"]["mcp_file_edit_used"] = "mcp__filesystem__edit_file" in successful
        report["passed"] = len(report["stages"]) == 3 and all(report["checks"].values())
        (run / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({key: report[key] for key in ("passed", "model_requests", "mcp_calls", "checks")}, ensure_ascii=False), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required to enable real model calls")
    sys.exit(0 if asyncio.run(check(args.run.resolve(), args.env_file)) else 1)
