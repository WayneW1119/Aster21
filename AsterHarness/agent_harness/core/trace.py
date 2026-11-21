import json
import sys
from pathlib import Path
from typing import Callable, TextIO


class ConsoleTrace:
    """Full, untruncated teaching output; never added to model context."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.step = 0

    def __call__(self, event: str, data: dict) -> None:
        self.step = data.get("step", self.step)
        labels = {
            "event_enqueued": "输入事件入队",
            "step_started": "Step 取出输入事件",
            "step_completed": "Step 完成",
            "user_message_sent": "向用户发消息",
            "session_waiting": "Session 等待输入（仍存活）",
            "session_stopped": "Session 已停止",
            "generator_created": "事件发生器已注册",
            "generator_fired": "事件发生器已投递事件",
            "generator_completed": "事件发生器已完成",
            "generator_cancelled": "事件发生器已取消",
            "generator_failed": "事件发生器失败",
            "history_updated": "当前模型有效历史",
            "history_recorded": "历史条目归档（稳定 ID）",
            "history_compacted": "历史范围压缩已提交（原文保留）",
            "world_state_updated": "上下文状态基线更新（内部元数据）",
            "model_request": "Harness 构造的 Prompt",
            "api_request": "实际 API 请求体（不含认证请求头）",
            "api_response": "完整 API 响应体（流式模式组装）",
            "api_stream_event": "模型响应流事件（不是输入队列事件）",
            "api_transport": "API 传输兼容模式",
            "model_response": "模型返回的 output items",
            "tool_started": "开始执行工具",
            "tool_rejected": "协议拒绝工具调用（未执行）",
            "protocol_error": "协议错误",
            "tool_result": "工具执行结果",
            "turn_completed": "Turn 完成",
        }
        print(f"\n{'=' * 20} Step {self.step} | {labels.get(event, event)} {'=' * 20}",
              file=self.stream)
        print(json.dumps(data, ensure_ascii=False, indent=2), file=self.stream, flush=True)


class CompositeTrace:
    def __init__(self, sinks: list[Callable[[str, dict], None]]) -> None:
        self.sinks = sinks

    def __call__(self, event: str, data: dict) -> None:
        for sink in self.sinks:
            sink(event, data)


class JsonlTrace:
    """Opt-in teaching trace; separate from model-visible history."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def __call__(self, event: str, data: dict) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, **data}, ensure_ascii=False) + "\n")
