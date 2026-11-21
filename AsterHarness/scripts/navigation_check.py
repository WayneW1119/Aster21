"""Single-run live directory compact / resume / distant archival recall check."""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_harness.config import load_env_file
from agent_harness.core.client import ResponsesClient
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.session.step import build_prompt
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.trace import JsonlTrace
from agent_harness.render_trace import render


async def main():
    run = ROOT / "test_runs" / ("navigation-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                                 + "-" + uuid4().hex[:8])
    workspace = run / "workspace"
    workspace.mkdir(parents=True)
    print("ARTIFACTS=" + str(run), flush=True)
    for key in ("HARNESS_MODEL", "HARNESS_RESPONSES_URL", "OPENAI_API_KEY"):
        os.environ.pop(key, None)
    load_env_file(ROOT / ".env")
    manager = ThreadManager(run / "threads")
    paths, report = [], {"passed": False}
    marker = "CEDAR-7Q9-K2"

    def client(trace):
        return ResponsesClient(os.environ["HARNESS_RESPONSES_URL"], os.environ["HARNESS_MODEL"],
                               os.environ["OPENAI_API_KEY"], allow_http=True, trace=trace)

    async def phase(number, prompt, thread=None):
        trace = JsonlTrace(run / f"phase-{number}.jsonl")
        paths.append(trace.path)
        (run / f"phase-{number}-task.txt").write_text(prompt, encoding="utf-8")
        session = (manager.create(client(trace), workspace,
                    configuration=SessionConfiguration(max_consecutive_steps=18), trace=trace)
                   if thread is None else manager.resume(client(trace), thread, trace=trace))
        if thread is None:
            session.record_items([{"type": "message", "role": "user", "text":
                "合成归档验收材料，不代表真实业务：回滚暗号是" + marker
                + ", 发布日期未知，发布未授权。此条用于测试远期信息召回。"}])
        else:
            assert marker not in json.dumps(build_prompt(session).input, ensure_ascii=False), \
                "Exact old fact was still visible before archival retrieval"
        try:
            await session.post_user_message(prompt)
            await session.run_until_idle()
        finally:
            await session.close()
        return session

    try:
        first = await phase(1,
            "这是隔离的编号历史验收，只操作当前工作区和自己的记忆，不访问网络或其他目录，"
            "不创建发生器、不修改 instructions。先用 exec_command 真正执行 Python 的 "
            "assert 2+2==4 并打印 ok。接着用 query_history_directory 获取目录，"
            "使用返回的 view_id 和编号独立调用 compact_history, "
            "把从最早历史到这次检查的完整回执压成摘要，无需 query_history 重读已有正文。"
            "为专门测试远期召回，摘要仅注明原始合成材料有'回滚暗号”可搜索，不保留其确切值；"
            "其他任务约束、发布未授权、发布日期未知和实际检查结果照常保留。"
            "成功后用 query_compaction(target=新摘要稳定ID) 查看结构。最后用 send_user_message "
            "简短报告检查和压缩结果，不要在报告中复述暗号值。")
        h = first.state.history
        assert h.generation >= 1, "No directory-based compact"
        assert marker not in json.dumps(h.snapshot(), ensure_ascii=False), "Old fact leaked into summary/report"
        summary = next(i for i in h.ids() if h.is_compaction(i))
        second = await phase(2,
            "继续隔离验收，不运行外部命令、不访问文件或网络。之前的摘要ID是" + summary +
            ", 现在想起合成材料中的‘回滚暗号'，但不要靠猜。先用 search_history 搜索‘回滚暗号'，"
            "target=这篇摘要的稳定ID，找到原始证据后用 query_history 按命中的稳定ID读取全文。"
            "然后用 read_compaction_range 读取该摘要 children 模式的第1-2条，并用 originals "
            "模式读取第1-2条， max_chars=2000，offset=0；如输出不完整，按返回位置继续。"
            "最后用 send_user_message 告诉我从归档查到的确切暗号，并明确这是合成验收数据。"
            "不发布、不修改任何文件。", first.thread_id)
        assert any(marker in text for text in second.state.sent_messages), "Agent failed distant fact recall"
        report.update(passed=True, thread_id=first.thread_id, summary_id=summary,
                      hidden_fact_recovered=True)
    except BaseException as error:
        report["failure"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        rows = [json.loads(line) for path in paths for line in path.read_text().splitlines()]
        errors = []
        for r in rows:
            if r["event"] != "tool_result":
                continue
            try:
                value = json.loads(r["result"]["output"])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and (value.get("error") or value.get("exit_code") not in (None, 0)):
                errors.append(r)
        tools = [r["call"]["name"] for r in rows if r["event"] == "tool_started"]
        missing = ({"exec_command", "compact_history", "query_history_directory", "query_compaction", "search_history",
                    "query_history", "read_compaction_range", "send_user_message"} - set(tools)
                   if report["passed"] else set())
        if missing:
            report.update(passed=False, failure={"type": "AssertionError", "message": "Missing tools: " + str(sorted(missing))})
        report.update(model_requests=sum(r["event"] == "model_request" for r in rows),
                      tools=tools, tool_errors=errors,
                      models=sorted({r["body"].get("model", "") for r in rows if r["event"] == "api_response"}),
                      usage=[r["body"].get("usage") for r in rows if r["event"] == "api_response"])
        for path in paths:
            path.with_suffix(".md").write_text(render(path), encoding="utf-8")
        (run / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k not in ("usage", "tool_errors", "tools")}
                         | {"tool_error_count": len(errors)}, ensure_ascii=False), flush=True)
        if missing:
            raise AssertionError("Missing expected tools; see result.json")


if __name__ == "__main__":
    asyncio.run(main())
