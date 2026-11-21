"""Short real-model MCP task with business data in a server-only tmpfs mount."""

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
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


def serve(business, node, server):
    # Executed only inside bwrap. No business input file exists in the host mount.
    business = Path(business)
    business.mkdir()
    (business / "numbers.json").write_text(json.dumps({"numbers": [7, 11, 13, 17]}))
    os.execv(node, [node, server, str(business)])


def boundary_checks(rows, business):
    outputs = {row["result"]["call_id"]: row["result"] for row in rows if row["event"] == "tool_result"}
    checks = {"native_read_rejected": False, "native_shell_cannot_read": False}
    for row in rows:
        if row["event"] != "tool_started":
            continue
        call = row["call"]
        if call.get("namespace") or call.get("name") not in {"read_file", "exec_command"}:
            continue
        args = json.loads(call["arguments"])
        result = json.loads(outputs[call["call_id"]]["output"])
        if call["name"] == "read_file" and args.get("path") == business + "/numbers.json":
            checks["native_read_rejected"] = (
                result.get("error") == "ValueError" and result.get("message") == "Path must stay inside the workspace")
        if call["name"] == "exec_command" and args.get("cmd") == "cat " + shlex.quote(business + "/numbers.json"):
            checks["native_shell_cannot_read"] = (
                result.get("exit_code") == 1 and "No such file or directory" in result.get("output", ""))
    return checks


async def check(tools_from):
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    run = ROOT / "test_runs" / (stamp + "-mcp-isolated-files")
    workspace = run / "workspace"
    workspace.mkdir(parents=True)
    business = "/tmp/aster-business-" + stamp
    config = json.loads((tools_from / "mcp-config.json").read_text())["filesystem"]
    node, server = config["command"], config["args"][0]
    servers = {"filesystem": {
        "command": "/usr/bin/bwrap",
        "args": ["--die-with-parent", "--ro-bind", "/", "/", "--unshare-user",
                 "--unshare-pid", "--unshare-net", "--tmpfs", "/tmp",
                 sys.executable, str(Path(__file__).resolve()), "--serve", business,
                 "--node", node, "--server", server],
        "required": True, "startup_timeout_sec": 60, "tool_timeout_sec": 30,
        "enabled_tools": ["list_allowed_directories", "list_directory", "read_text_file", "write_file"],
    }}
    (run / "mcp-config.json").write_text(json.dumps(servers, indent=2))
    before = service_state()
    mobile = ROOT / ".harness/resident/mobile/instance.json"
    mobile_hash = digest(mobile)
    load_env_file(ROOT / ".env")
    trace = RecordingTrace(run / "session-1.jsonl")
    client = ResponsesClient(os.environ["HARNESS_RESPONSES_URL"], os.environ["HARNESS_MODEL"],
                             os.environ["OPENAI_API_KEY"], allow_http=True, trace=trace)
    session = ThreadManager(run / "threads").create(client, workspace,
        configuration=SessionConfiguration(max_consecutive_steps=30, mcp_servers=servers), trace=trace)
    report = {"run": str(run), "workspace": str(workspace), "business_path_in_mcp": business,
              "thread_id": session.thread_id, "checks": {}}
    started = time.monotonic()
    try:
        await session.post_user_message(
            f"这是隔离文件访问的短测试。你的普通工作目录是 {workspace}。"
            f"filesystem MCP 服务持有独立临时文件系统，业务目录是 {business}，其中有 numbers.json。"
            "先验证访问边界：用内置 read_file 尝试读该文件一次；再用 exec_command 执行以下命令一次："
            f"cat {shlex.quote(business + '/numbers.json')}。两次失败是预期结果，不要尝试绕过隔离、"
            "遍历进程或访问 /proc，也不要创建同名本地文件。"
            "然后搜索 MCP 的文件工具，通过 MCP 确认允许目录，读取 numbers.json，计算总和与数量，"
            "把 {sum:合计,count:数量} 写到业务目录 result.json，再通过 MCP 回读核对。"
            "用记忆工具记录已验证结果。通过 send_user_message 用中文汇报直接访问和 MCP 操作的实际结果，"
            "完成后 wait_for_event。不要修改原始 numbers.json，不访问其他工作区、网络或凭据。")
        await asyncio.wait_for(session.run_until_idle(), 150)
        mcp = dict(session.services.mcp.binding.servers)["filesystem"]
        # Independent host verification also uses the MCP boundary, not /proc or a host copy.
        evidence = {}
        for name in ("numbers.json", "result.json"):
            response = await mcp.call_tool("read_text_file", {"path": business + "/" + name})
            evidence[name] = response
            assert not response.get("isError"), response
            value = json.loads("\n".join(item["text"] for item in response["content"] if item["type"] == "text"))
            assert value == ({"numbers": [7, 11, 13, 17]} if name == "numbers.json" else {"sum": 48, "count": 4})
        (run / "mcp-verification.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        report["checks"]["mcp_result_correct_and_source_unchanged"] = True
        report["checks"]["business_path_absent_on_host_while_server_running"] = not Path(business).exists()
        rows = trace.rows
        report["checks"].update(boundary_checks(rows, business))
        (run / "boundary-evidence.json").write_text(json.dumps([
            row for row in rows if row["event"] in {"tool_started", "tool_result"}
        ], ensure_ascii=False, indent=2))
        calls = [row["call"] for row in rows if row["event"] == "tool_started"]
        report["checks"]["model_attempted_native_read"] = any(
            not c.get("namespace") and c.get("name") == "read_file" for c in calls)
        report["checks"]["model_attempted_native_command"] = any(
            not c.get("namespace") and c.get("name") == "exec_command" for c in calls)
        report["checks"]["model_searched_and_called_mcp"] = (
            any(c["type"] == "tool_search_call" for c in calls)
            and any(c.get("namespace") for c in calls))
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        await session.suspend()
        report["checks"]["phone_unchanged"] = before == service_state() and mobile_hash == digest(mobile)
        usage = [row["body"].get("usage", {}) for row in trace.rows if row["event"] == "api_response"]
        report.update(seconds=round(time.monotonic() - started, 3), steps=len(usage),
            input_tokens=sum(u.get("input_tokens", 0) for u in usage),
            output_tokens=sum(u.get("output_tokens", 0) for u in usage))
        report["passed"] = "error" not in report and all(report["checks"].values())
        (run / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        build(run)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve")
    parser.add_argument("--node")
    parser.add_argument("--server")
    parser.add_argument("--tools-from", type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.serve:
        serve(args.serve, args.node, args.server)
    elif args.live and args.tools_from:
        sys.exit(0 if asyncio.run(check(args.tools_from.resolve())) else 1)
    else:
        parser.error("Use --live --tools-from DIR for a billable real-model test")
