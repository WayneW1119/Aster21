"""Real model writes, registers, updates and resumes its own script tool."""

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
from scripts.mcp_trace_report import build


async def check():
    run = ROOT / "test_runs" / (datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S") + "-custom-tools-live")
    workspace = run / "workspace"
    workspace.mkdir(parents=True)
    load_env_file(ROOT / ".env")
    manager = ThreadManager(run / "threads")
    traces, sessions = [], []
    phone = ROOT / ".harness/resident/mobile/instance.json"
    phone_hash, before = digest(phone), service_state()
    report = {"run": str(run), "checks": {}, "stages": []}
    started = time.monotonic()

    def open_session(thread=None):
        trace = RecordingTrace(run / f"session-{len(traces) + 1}.jsonl")
        traces.append(trace)
        client = ResponsesClient(os.environ["HARNESS_RESPONSES_URL"], os.environ["HARNESS_MODEL"],
                                 os.environ["OPENAI_API_KEY"], allow_http=True, trace=trace)
        session = (manager.create(client, workspace, trace=trace,
            configuration=SessionConfiguration(max_consecutive_steps=25)) if thread is None
            else manager.resume(client, thread, trace=trace))
        sessions.append(session)
        return session

    async def ask(session, stage, message):
        await session.post_user_message(message)
        await asyncio.wait_for(session.run_until_idle(), 180)
        session.mcp_projection.validate(session.state.history.snapshot())
        report["stages"].append({"stage": stage, "step": session.state.step_number})
        print("STAGE", stage, session.state.step_number, flush=True)

    session = open_session()
    report["thread_id"] = session.thread_id
    try:
        await ask(session, "create-register-search-call", f"在独立工作区 {workspace} 做自定义工具验收，"
            "只在此工作区和自己的私有记忆操作，不访问其他工作区、网络或凭据。"
            "先用 apply_patch 编写 scripts/calculate.py：从 stdin 读取 JSON 对象 {numbers:[整数,...]}，"
            "计算并输出 JSON {sum:总和,count:数量}，同时将同一个结果写到当前工作目录 calculation.json。"
            "用 manage_tool_group 创建 arithmetic，描述说明这是本地数值处理，提供必要使用说明；"
            "用 manage_custom_tool 将脚本注册为 summarize，定义严格的对象参数 schema，"
            f"执行解释器使用 {sys.executable}，cwd 为工作区。"
            "确认注册生效后用 tool_search 搜索并加载，通过 local_arithmetic.summarize 调用 numbers=[2,5,8]。"
            "不要用 exec_command 代替自定义工具运行计算。核对结果，把已验证状态记入记忆，"
            "通过 send_user_message 汇报，然后 wait_for_event。")
        first = json.loads((workspace / "calculation.json").read_text())
        assert first == {"sum": 15, "count": 3}, first
        assert "summarize" in session.custom_tools.inspect("arithmetic")["tools"]
        (run / "first-calculation.json").write_text(json.dumps(first))
        report["checks"]["agent_created_registered_searched_and_called"] = True

        await ask(session, "edit-update-reload-call", "现在扩展这个工具。用 apply_patch 修改原始脚本，"
            "输入增加 operation，支持 sum 和 product；输出改为 {operation,result,count}，"
            "仍写 calculation.json。先更新工具组的描述和使用说明，说明支持求和与求积，"
            "再使用 manage_custom_tool update 显式启用新脚本及参数 schema，不要重新注册同名工具。"
            "确认生效后重新 tool_search 加载新定义，用自定义工具计算 numbers=[2,3,4],operation=product。"
            "核对并记忆已验证结果，汇报后等待。")
        second = json.loads((workspace / "calculation.json").read_text())
        assert second == {"operation": "product", "result": 24, "count": 3}, second
        (run / "second-calculation.json").write_text(json.dumps(second))
        assert session.custom_tools.inspect("arithmetic", "summarize")["version"] >= 4
        report["checks"]["agent_updated_group_script_and_schema"] = True

        # Explicitly host-controlled whole compaction; not claimed as model autonomy.
        ids = session.state.history.ids()
        session._compact_boundary = set(ids)
        session.compact_history(ids[0], ids[-1], session.state.history.generation,
            "Agent created arithmetic.summarize and updated it to support sum/product. "
            "Verified product [2,3,4]=24. Script reads JSON stdin and writes calculation.json. "
            "Loaded definitions are retained. Continue using the active tool after resume.")
        saved = session.mcp_projection.to_dict()
        await session.suspend()
        session = open_session(session.thread_id)
        assert session.mcp_projection.to_dict() == saved
        await ask(session, "compact-resume-existing-tool", "继续验收。使用已有且已加载的 summarize 工具，"
            "计算 numbers=[10,20,30],operation=sum；无需重新编写或注册工具。"
            "核对结果，更新记忆，汇报并等待。")
        third = json.loads((workspace / "calculation.json").read_text())
        assert third == {"operation": "sum", "result": 60, "count": 3}, third
        report["checks"]["loaded_tool_works_after_compact_and_resume"] = True
        report["checks"]["no_mcp_service_required"] = not session.configuration.mcp_servers
        rows = [row for trace in traces for row in trace.rows]
        calls = [row["call"] for row in rows if row["event"] == "tool_started"]
        report["checks"]["three_real_custom_invocations"] = sum(
            c.get("namespace") == "local_arithmetic" and c.get("name") == "summarize" for c in calls) >= 3
        custom_ids = {c["call_id"] for c in calls if c.get("namespace") == "local_arithmetic"}
        actual_results = [json.loads(json.loads(row["result"]["output"])["stdout"])
            for row in rows if row["event"] == "tool_result" and row["result"].get("call_id") in custom_ids
            and json.loads(row["result"]["output"]).get("success")]
        report["checks"]["all_expected_results_returned_by_custom_tools"] = all(
            wanted in actual_results for wanted in [first, second, third])
    except asyncio.CancelledError:
        report["error"] = "CancelledError: test interrupted before completion"
        raise
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        for session in reversed(sessions):
            await session.suspend()
        report["checks"]["phone_unchanged"] = before == service_state() and phone_hash == digest(phone)
        rows = [row for trace in traces for row in trace.rows]
        usage = [row["body"].get("usage", {}) for row in rows if row["event"] == "api_response"]
        report.update(seconds=round(time.monotonic() - started, 3), steps=len(usage),
            input_tokens=sum(u.get("input_tokens", 0) for u in usage),
            output_tokens=sum(u.get("output_tokens", 0) for u in usage))
        report["passed"] = "error" not in report and all(report["checks"].values())
        (run / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        build(run)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required for billable model calls")
    sys.exit(0 if asyncio.run(check()) else 1)
