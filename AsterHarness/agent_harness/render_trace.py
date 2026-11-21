"""Render an existing JSONL trace without model calls or summarization."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path


def block(text: str, language: str = "text") -> str:
    longest = max((len(m.group()) for m in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}\n"


def readable_items(items: list[dict], labels: list[str] | None = None) -> str:
    sections = []
    for index, item in enumerate(items, 1):
        kind = item.get("type", "message")
        label = labels[index - 1] if labels is not None else f"Item {index}"
        sections.append(f"#### {label}：{kind} / {item.get('role', '无 role 字段')}\n")
        if "source" in item:
            sections.append(f"来源：`{item['source']}`\n")
        if kind == "function_call":
            sections.append(f"工具：`{item.get('name')}`；call_id：`{item.get('call_id')}`\n")
            arguments = item.get("arguments", "")
            try:
                arguments = json.dumps(json.loads(arguments), ensure_ascii=False, indent=2)
            except (ValueError, TypeError):
                pass
            sections.append(block(str(arguments), "json"))
        elif kind == "function_call_output":
            sections.append(f"对应 call_id：`{item.get('call_id')}`\n")
            output = item.get("output", "")
            sections.append(block(output if isinstance(output, str) else
                                  json.dumps(output, ensure_ascii=False, indent=2)))
        else:
            if "text" in item:
                sections.append(block(item["text"]))
            content = item.get("content", [])
            if isinstance(content, str):
                sections.append(block(content))
            else:
                for part in content:
                    if "text" in part:
                        sections.append(block(part["text"]))
            if kind == "reasoning":
                sections.append("推理 item 的完整字段见本事件 JSON；不尝试解密或推测其内容。\n")
    return "\n".join(sections)


def render(source: Path) -> str:
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    counts = Counter(row["event"] for row in rows)
    names = {
        "history_updated": "模型有效历史的完整快照",
        "history_recorded": "历史条目归档（稳定 ID）",
        "world_state_updated": "上下文状态基线更新（内部元数据）",
        "model_request": "Harness 构造 Prompt",
        "api_request": "发送到模型服务的实际请求体",
        "api_response": "完整响应体（流式模式由完成事件与已完成 items 组装）",
        "api_stream_event": "模型响应流事件（非调度队列事件）",
        "api_transport": "模型传输兼容模式",
        "model_response": "Adapter 返回给 Step 的 output items",
        "tool_started": "Harness 开始执行工具",
        "tool_rejected": "Harness 拒绝此调用（未执行）",
        "protocol_error": "协议错误",
        "tool_result": "工具返回结果",
        "history_compacted": "历史范围压缩已提交（原文保留）",
        "turn_completed": "Turn 结束",
        "event_enqueued": "输入事件入队（尚未加入历史）",
        "step_started": "Step 原子取出全部待处理事件",
        "step_completed": "Step 完成",
        "user_message_sent": "已向用户发送消息",
        "session_waiting": "Session 等待输入（仍存活）",
        "session_stopped": "Session 停止",
        "session_error": "Session 出错",
        "session_cancelled": "Session 被取消",
        "protocol_warning": "协议提醒",
        "generator_created": "事件发生器已注册",
        "generator_restored": "事件发生器已恢复运行",
        "generator_fired": "事件发生器已投递事件",
        "generator_completed": "事件发生器已完成",
        "generator_cancelled": "事件发生器已取消",
        "generator_failed": "事件发生器失败",
        "generator_paused": "事件发生器因限流暂停",
        "generator_restarted": "程序型发生器已显式重启",
    }
    parts = ["# Harness 完整运行记录\n",
             f"来源：`{source.name}`\n",
             f"记录包含 {len(rows)} 条事件、{counts['model_request']} 次模型请求、"
             f"{counts['tool_started']} 次工具执行。\n",
             "## 如何阅读\n",
             "每条事件先展开可读文本（如存在），再附完整 JSON。所有事件和字段均保留，"
             "没有用摘要代替原文；重复请求、响应、历史快照也不去重。JSON 中的转义仅作排版，"
             "原始字符串以源 JSONL 为准。认证请求头不在原日志中，因此这里也没有。\n",
             "这里的日志事件不等于调度用的 HarnessEvent；后者见入队记录和 Step 的 events。"
             "Step 编号在整个 Session 中递增，0 表示首次请求前。API 事件自身没有 step 字段，"
             "以下按其所在顺序归属于最近的 model_request。展示日志不会调用模型。\n",
             "reasoning 未返回时，不推测隐藏思维；model 名称、usage 等均照录服务端返回值。"
             "原日志未记录的工作目录、耗时或请求头，不会凭空补出。\n",
             "## 时间线\n"]
    step = 0
    for index, row in enumerate(rows, 1):
        step = row.get("step", step)
        parts.append(f"- 事件 {index:02d} · Step {step} · {names.get(row['event'], row['event'])}")
    parts.append("\n## 完整事件\n")
    step = 0
    for index, row in enumerate(rows, 1):
        step = row.get("step", step)
        event = row["event"]
        parts.append(f"\n### 事件 {index:02d} · Step {step} · {names.get(event, event)}\n")
        if event == "history_updated":
            reasons = {"initial_memory": "加载并记录初始记忆（仅一次）",
                       "world_state": "追加上下文状态片段",
                       "user_input": "追加用户消息", "model_output": "追加模型输出",
                       "tool_result": "追加工具结果", "input_events": "追加本次出队事件",
                       "non_waking_send_receipt": "追加发送回执（不产生唤醒事件）",
                       "non_waking_tool_receipt": "追加工具回执（不产生唤醒事件）"}
            parts.append(f"原因：{reasons.get(row.get('reason'), row.get('reason'))}。"
                         f"此时历史包含 {len(row['items'])} 个 item。\n")
            parts.append(readable_items(row["items"]))
        elif event in ("model_request", "api_request"):
            prompt = row.get("prompt", row.get("body", {}))
            parts.append("基础指令原文：\n")
            parts.append(block(prompt.get("base_instructions", prompt.get("instructions", ""))))
            if prompt.get("memory") is not None:
                parts.append("独立记忆基线（不编号，不属于历史）：\n")
                parts.append(readable_items([prompt["memory"]]))
            parts.append("本次完整 input，按顺序展开：\n")
            parts.append(readable_items(prompt.get("input", []), prompt.get("history_labels")))
            parts.append("完整工具 schema 和其他请求字段全部列在下面的 JSON 中。\n")
        elif event in ("model_response", "api_response"):
            output = row.get("output", row.get("body", {}).get("output", []))
            parts.append(readable_items(output))
            if event == "api_response":
                usage = row["body"].get("usage")
                if usage is not None:
                    parts.append("服务端报告的 token 用量：\n" +
                                 block(json.dumps(usage, ensure_ascii=False, indent=2), "json"))
        elif event == "history_compacted":
            parts.append(f"摘要 ID：`{row['summary_id']}`；有效视图版本：{row['generation']}；"
                         f"直接替换 {row['replaced_count']} 条，覆盖 {row['leaf_count']} 条原始记录。\n")
            parts.append("Agent 提供的完整摘要：\n" + block(row["summary"]))
            if row.get("memory_checkpoint") is not None:
                parts.append("同时记录的当前记忆索引检查点：\n")
                parts.append(readable_items([row["memory_checkpoint"]]))
        elif event == "user_message_sent":
            parts.append(block(row["text"]))
        elif event in ("step_started", "event_enqueued"):
            queued = row["events"] if event == "step_started" else [row["queued_event"]]
            parts.append(readable_items([entry["item"] for entry in queued]))
        elif event in ("tool_started", "tool_rejected", "tool_result"):
            parts.append(readable_items([row["result"] if event == "tool_result" else row["call"]]))
        parts.append("#### 本事件完整 JSON（所有字段）\n")
        parts.append(block(json.dumps(row, ensure_ascii=False, indent=2), "json"))
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.source.with_suffix(".md")
    if output.resolve() == args.source.resolve():
        parser.error("Output must not overwrite the source trace")
    # Exclusive creation protects existing reports from accidental replacement.
    with output.open("x", encoding="utf-8") as stream:
        stream.write(render(args.source))
    print(f"完整可读记录：{output}")


if __name__ == "__main__":
    main()
