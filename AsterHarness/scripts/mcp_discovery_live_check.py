"""Isolated real-model native MCP discovery, resources, compact and resume check."""

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_harness.config import load_env_file
from agent_harness.core.client import ResponsesClient
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.thread_manager import ThreadManager
from scripts.mcp_live_check import RecordingTrace, digest, service_state


async def check(tools_from):
    run = ROOT / "test_runs" / (datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S") + "-mcp-discovery-live")
    workspace = run / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "numbers.json").write_text(json.dumps({"numbers": [7, 11, 13]}))
    config = json.loads((tools_from / "mcp-config.json").read_text())
    config.pop("fetch", None)
    config["filesystem"]["args"][-1] = str(workspace)
    config["fixture"] = {"command": sys.executable, "args": [str(ROOT / "tests/fixtures/mcp_server.py"),
                         "--state-dir", str(run / "fixture-state")], "required": True}
    load_env_file(ROOT / ".env")
    manager = ThreadManager(run / "threads")
    traces, sessions = [], []
    before = service_state()
    mobile_hash = digest(ROOT / ".harness/resident/mobile/instance.json")
    result = {"run": str(run), "model": os.environ["HARNESS_MODEL"], "checks": {}}
    started = time.monotonic()

    def open_session(thread=None):
        trace = RecordingTrace(run / f"session-{len(traces) + 1}.jsonl")
        traces.append(trace)
        client = ResponsesClient(os.environ["HARNESS_RESPONSES_URL"], os.environ["HARNESS_MODEL"],
                                 os.environ["OPENAI_API_KEY"], allow_http=True, trace=trace)
        session = (manager.create(client, workspace,
            configuration=SessionConfiguration(max_consecutive_steps=50, mcp_servers=config), trace=trace)
            if thread is None else manager.resume(client, thread, trace=trace))
        sessions.append(session)
        return session

    async def ask(session, text):
        await session.post_user_message(text)
        await asyncio.wait_for(session.run_until_idle(), 180)
        session.mcp_projection.validate(session.state.history.snapshot())

    session = open_session()
    result["thread_id"] = session.thread_id
    try:
        await ask(session, f"独立验收，业务文件操作只限 {workspace}，不要访问其他工作区或凭据。"
            "只能使用 MCP 工具读写业务文件，记忆用专用记忆工具。先发现可用 MCP 能力："
            "使用通用资源工具列出 fixture 的资源与资源模板，并读取 fixture://guide 和"
            " fixture://items/P01。通过工具搜索发现 filesystem 工具，读取 numbers.json，"
            "将 {sum:合计,count:数量} 写到 result.json 并回读核对；另写 resources.md 记录实际"
            "资源内容、模板 URI 和来源，不杜撰。把已验证状态和约束记录到私有记忆，"
            "通过 send_user_message 汇报，然后 wait_for_event。")
        assert json.loads((workspace / "result.json").read_text()) == {"sum": 31, "count": 3}
        notes = (workspace / "resources.md").read_text()
        assert "fixture://guide" in notes and "P01" in notes
        result["checks"]["resource_reads_and_mcp_file_task"] = True
        await ask(session, "整理并更新记忆，然后查询历史目录，使用 compact_history 压缩初始任务和"
            "已经完成的工具工作，保留来源、约束和已验证结果。压缩工具独占响应，完成后等待。")
        assert session.state.history.generation > 0
        result["checks"]["model_initiated_compaction"] = True
        # Exercise full baseline rebase too, independent of the model's chosen range.
        ids = session.state.history.ids()
        session._compact_boundary = set(ids)
        session.compact_history(ids[0], ids[-1], session.state.history.generation,
            "Verified sum=31, count=3. Fixture resources and filesystem tools were used. "
            "Continue with the user's update; business files only via MCP, private memory via memory tools.")
        assert not session.mcp_projection.loaded_definitions()
        assert not session.mcp_projection.baseline["searches"]
        result["checks"]["full_compaction_drops_discovered_definitions"] = True
        saved = session.mcp_projection.to_dict()
        await session.suspend()
        (workspace / "numbers.json").write_text(json.dumps({"numbers": [7, 11, 13, 17]}))
        session = open_session(session.thread_id)
        assert session.mcp_projection.to_dict() == saved
        result["checks"]["same_thread_projection_resumed"] = True
        await ask(session, "numbers.json 已更新。重新搜索加载 filesystem 的读取和写入工具，"
            "重新计算并更新 result.json，回读核对；再通过 fixture 的资源读取确认 P01。"
            "更新记忆并汇报。先前搜索定义已随 compact 移除，来源和连接仍在，"
            "请通过 tool_search 重新加载所需定义。完成后等待。")
        assert json.loads((workspace / "result.json").read_text()) == {"sum": 48, "count": 4}
        result["checks"]["tools_work_after_compact_and_resume"] = True
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        for session in reversed(sessions):
            await session.suspend()
        rows = [row for trace in traces for row in trace.rows]
        requests = [row["body"] for row in rows if row["event"] == "api_request"]
        calls = [row["call"] for row in rows if row["event"] == "tool_started"]
        usage = [row["body"].get("usage", {}) for row in rows if row["event"] == "api_response"]
        result["checks"]["deferred_catalog_not_in_initial_tools"] = bool(requests) and not any(
            spec.get("type") == "namespace" or spec.get("name", "").startswith("mcp__")
            for spec in requests[0]["tools"])
        result["checks"]["native_search_used"] = any(call["type"] == "tool_search_call" for call in calls)
        result["checks"]["no_native_business_file_bypass"] = not any(not call.get("namespace") and call.get("name") in
            {"exec_command", "read_file", "write_file", "apply_patch", "list_directory"} for call in calls)
        result["checks"]["phone_unchanged"] = before == service_state() and mobile_hash == digest(
            ROOT / ".harness/resident/mobile/instance.json")
        result.update(seconds=round(time.monotonic() - started, 3), requests=len(usage),
            tool_calls=len(calls), native_searches=sum(call["type"] == "tool_search_call" for call in calls),
            resumed_searches=sum(row["event"] == "tool_started" and row["call"]["type"] == "tool_search_call"
                                 for row in traces[-1].rows) if len(traces) > 1 else None,
            input_tokens=sum(item.get("input_tokens", 0) for item in usage),
            output_tokens=sum(item.get("output_tokens", 0) for item in usage),
            cached_tokens=sum(item.get("input_tokens_details", {}).get("cached_tokens", 0) for item in usage))
        result["passed"] = "error" not in result and all(result["checks"].values())
        (run / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return result["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools-from", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required for billable model calls")
    sys.exit(0 if asyncio.run(check(args.tools_from.resolve())) else 1)
