"""Explicit live-model compact -> resume -> provenance -> nested compact evaluation.

No retries. All prompts, stream events, receipts and failures remain in test_runs.
Only a fresh fixture workspace and a fresh Thread are placed in scope.
"""

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
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.trace import JsonlTrace
from agent_harness.render_trace import render


async def main():
    run = ROOT / "test_runs" / ("compact-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                                 + "-" + uuid4().hex[:8])
    workspace = run / "workspace"
    workspace.mkdir(parents=True)
    print("ARTIFACTS=" + str(run), flush=True)
    # These are explicitly fictional input notes, not invented verified results.
    notes = ("# Compact evaluation fixture\n"
             "This document is synthetic test input. Owner: Lin. Project: Cedar.\n"
             "Release date: UNKNOWN. Never invent a year. Publication is NOT authorized.\n"
             "Implement double(x)=x*2. None support has NOT been implemented or verified.\n"
             "Original lookup marker: CEDAR-ORIGINAL-7Q9.\n\n"
             + "\n".join(f"Reference note {i}: preserve evidence, distinguish verified from proposed work."
                          for i in range(60)))
    (workspace / "notes.md").write_text(notes, encoding="utf-8")
    for name in ("HARNESS_MODEL", "HARNESS_RESPONSES_URL", "OPENAI_API_KEY"):
        os.environ.pop(name, None)
    load_env_file(ROOT / ".env")
    manager = ThreadManager(run / "threads")
    traces = []
    session = None
    report = {"passed": False}

    def client(trace):
        return ResponsesClient(os.environ["HARNESS_RESPONSES_URL"], os.environ["HARNESS_MODEL"],
                               os.environ["OPENAI_API_KEY"], allow_http=True, trace=trace)

    async def phase(number, prompt, thread_id=None):
        nonlocal session
        (run / f"phase-{number}-task.txt").write_text(prompt, encoding="utf-8")
        trace = JsonlTrace(run / f"phase-{number}.jsonl")
        traces.append(trace.path)
        if thread_id is None:
            session = manager.create(client(trace), workspace,
                configuration=SessionConfiguration(max_consecutive_steps=24), trace=trace,
                agent_instructions="Maintain the Cedar test fixture. Serve the user's requests. Do not publish.")
        else:
            session = manager.resume(client(trace), thread_id, trace=trace)
        try:
            await session.post_user_message(prompt)
            await session.run_until_idle()
        finally:
            await session.close()
        return session

    try:
        first = await phase(1,
            "这是隔离的 compact 验收，仅操作当前 workspace 和自己的记忆，不访问网络、凭据或其他 Thread；"
            "不创建发生器，不修改 instructions。先完整读取 notes.md；使用 apply_patch 创建 tiny_math.py,"
            "实现 double(x)=x*2; exec_command 实测 double(3)==6 和 double(0)==0。用记忆工具保存"
            "职责、已验证状态、未支持 None、发布未授权、发布日期未知；写入后读回核对。然后直接按"
            "当前可见编号，把从最早历史开始、至少包含 notes.md "
            "读取 call/receipt 和已完成记忆读写的连续历史范围，用 compact_history 替换为忠实摘要；"
            "根据已见内容选择范围，不要为了获取编号重复查全文，确保调用与回执成对；摘要保留任务、约束、结果、未完成项和原始查验标记。"
            "compact 必须单独调用。成功回执后，调用 query_compaction 查看来源，最后用 send_user_message "
            "报告实际结果。不要把未验证项目说成成功，不要发布。")
        history = first.state.history
        assert history.generation >= 1, "Agent did not compact"
        first_summary = next(i for i in history.ids() if "<history_summary" in history.item(i).get("text", ""))
        saved_ids = history.ids()
        saved_origins = history.provenance(first_summary)
        report["first"] = {"thread_id": first.thread_id, "summary_id": first_summary,
                            "effective_items": len(saved_ids), "archive_items": history.revision,
                            "leaf_count": saved_origins["leaf_count"]}
        second = await phase(2,
            "继续同一隔离验收。先确认自己的记忆和有效历史摘要。旧摘要 ID 是 " + first_summary +
            ", 必须用 query_compaction 看它的来源结构，用 search_history 搜索原始查验标记，再用 query_history 按命中稳定ID查询原文，"
            "核验 notes.md 的原始查验标记；不要仅复述摘要当作完成查询。实测 tiny_math.py 的两个既有"
            "断言；不要加入 None 支持。然后直接根据当前可见编号，"
            "再次独立调用 compact_history，所选连续范围必须包含旧摘要 " + first_summary +
            ", 并包含一些本阶段已完成的查询或验证记录，以验证嵌套压缩。保留用户约束、未知发布日期、"
            "None 尚未支持的状态。压缩后用 query_compaction 验证 compact_items 中含旧摘要的稳定ID，最后通过"
            "send_user_message 报告真实结果和原始查验标记。仍不发布，不访问其他目录或网络。",
            first.thread_id)
        h = second.state.history
        assert h.generation > history.generation, "No nested compact after resume"
        nested = [i for i in h.ids("archive") if i not in history.ids("archive")
                  and "<history_summary" in h.item(i).get("text", "")]
        assert any(first_summary in h.provenance(i)["source_ids"] for i in nested), "Missing nested provenance"
        assert h.provenance(first_summary) == saved_origins, "Provenance changed on resume"
        namespace = {}
        exec(compile((workspace / "tiny_math.py").read_text(), "tiny_math.py", "exec"), namespace)
        assert namespace["double"](3) == 6 and namespace["double"](0) == 0
        assert any("CEDAR-ORIGINAL-7Q9" in text for text in second.state.sent_messages)
        # Third resume checks both generations without another model request.
        final = manager.resume(client(JsonlTrace(run / "resume-only.jsonl")), first.thread_id)
        try:
            assert final.state.history.generation == h.generation
            assert final.state.history.ids()[:len(h.ids())] == h.ids()
            assert final.state.history.provenance(first_summary) == saved_origins
        finally:
            await final.close()
        report["second"] = {"effective_items": len(h.ids()), "archive_items": h.revision,
                             "generation": h.generation, "nested_summaries": nested}
        report["passed"] = True
    except BaseException as error:
        report["failure"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        rows = [json.loads(line) for path in traces for line in path.read_text().splitlines()]
        errors = []
        for row in rows:
            if row["event"] != "tool_result":
                continue
            try:
                value = json.loads(row["result"]["output"])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and (value.get("error") or value.get("exit_code") not in (None, 0)):
                errors.append(row)
        report["model_requests"] = sum(r["event"] == "model_request" for r in rows)
        report["tool_errors"] = errors
        report["usage"] = [r["body"].get("usage") for r in rows if r["event"] == "api_response"]
        (run / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        for path in traces:
            path.with_suffix(".md").write_text(render(path), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k not in ("usage", "tool_errors")}
                         | {"tool_error_count": len(errors)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
