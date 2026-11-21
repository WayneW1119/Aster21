"""Real-model text budget, uploaded image, local image and clean-resume check."""

import argparse
import asyncio
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_harness.config import load_env_file
from agent_harness.core.client import ResponsesClient
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.tools.registry import Tool
from scripts.mcp_live_check import RecordingTrace, digest, service_state
from scripts.mcp_trace_report import build


def make_card(path, code, dots):
    image = Image.new("RGB", (800, 420), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 100)
    draw.text((70, 45), code, fill="black", font=font)
    for i in range(dots):
        x = 80 + i * 160
        draw.ellipse((x, 240, x + 75, 315), fill="red")
    image.save(path)


async def check():
    run = ROOT / "test_runs" / (datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S") + "-budget-images-live")
    workspace = run / "workspace"
    workspace.mkdir(parents=True)
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    codes = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    make_card(workspace / "phone.png", codes[0], 3)
    make_card(workspace / "card.png", codes[1], 4)
    report_text = "A" * 20000 + "CHECK_CODE=" + codes[2] + "\n" + "Z" * 30000
    load_env_file(ROOT / ".env")
    manager = ThreadManager(run / "threads")
    sessions, traces, report_calls = [], [], []
    before = service_state()
    phone = ROOT / ".harness/resident/mobile/instance.json"
    phone_hash = digest(phone)
    result = {"run": str(run), "checks": {}, "stages": [], "expected_codes": codes}
    start = time.monotonic()

    def open_session(thread=None):
        trace = RecordingTrace(run / f"session-{len(traces) + 1}.jsonl")
        traces.append(trace)
        client = ResponsesClient(os.environ["HARNESS_RESPONSES_URL"], os.environ["HARNESS_MODEL"],
            os.environ["OPENAI_API_KEY"], allow_http=True, trace=trace)
        session = (manager.create(client, workspace, trace=trace,
            configuration=SessionConfiguration(max_consecutive_steps=30, tool_output_token_limit=1024))
            if thread is None else manager.resume(client, thread, trace=trace))

        def read_report():
            report_calls.append(session.state.step_number)
            return report_text

        session.router.registry.register(Tool({"type": "function", "name": "read_report",
            "description": "Read the long local test report. No arguments.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False}, "strict": False}, read_report))
        sessions.append(session)
        return session

    async def ask(session, stage, text, images=None):
        await session.post_user_message(text, images=images)
        await asyncio.wait_for(session.run_until_idle(), 120)
        result["stages"].append({"stage": stage, "step": session.state.step_number})
        print("STAGE", stage, session.state.step_number, flush=True)

    def delivered(session, label, code, dots=None):
        combined = "\n".join(session.state.sent_messages)
        pattern = re.escape(label) + r"\s+code\s*=\s*" + re.escape(code)
        if dots is not None:
            pattern += r"\s+red\s*=\s*" + str(dots)
        assert re.search(pattern, combined), (label, combined)

    session = open_session()
    result["thread_id"] = session.thread_id
    try:
        await ask(session, "uploaded-image", "独立图片验收。查看附带的图片，读出四位码并数红色圆点。"
            "只通过 send_user_message 发送一行 PHOTO code=实际四位码 red=实际数量，然后等待。不要猜测，不访问网络或其他工作区。",
            images=[{"name": "phone.png", "data_url": "data:image/png;base64," +
                     base64.b64encode((workspace / "phone.png").read_bytes()).decode()}])
        delivered(session, "PHOTO", codes[0], 3)
        result["checks"]["uploaded_image_seen_as_pixels"] = True

        await ask(session, "long-output-pages", "通过 read_report 读取长报告。报告里字符偏移 20000 附近有 CHECK_CODE。"
            "若回执被统一预算截断，使用 read_tool_output 的 output_id 分页精确读取那里，不要通过命令或猜测代替。"
            "拿到后用 send_user_message 发送 BUDGET code=实际代码，然后等待。")
        delivered(session, "BUDGET", codes[2])
        assert report_calls
        limited = next(row for row in traces[0].rows if row["event"] == "tool_output_limited" and row["kind"] == "text")
        output_id = limited["output_id"]
        assert any(row["event"] == "tool_result_raw" and row["result"].get("output") == report_text for row in traces[0].rows)
        result["checks"]["full_raw_output_saved_and_model_used_exact_page"] = True

        await ask(session, "local-view-image", "请调用 view_image 查看工作区 card.png，读出四位码并数红色圆点。"
            "通过 send_user_message 发送 LOCAL code=实际四位码 red=实际数量，然后等待。")
        delivered(session, "LOCAL", codes[1], 4)
        local_calls = {row["call"]["call_id"] for row in traces[0].rows if row["event"] == "tool_started"
            and row["call"].get("name") == "view_image"
            and json.loads(row["call"]["arguments"]).get("path") == "card.png"}
        image_result = next(row["result"] for row in traces[0].rows if row["event"] == "tool_result"
            and row["result"]["call_id"] in local_calls)
        descriptor = json.loads(image_result["output"][0]["text"])
        original_pixels = image_result["output"][1]["image_url"]
        result["checks"]["local_view_image_native_result_seen"] = True

        history = session.state.history
        session._compact_boundary = set(history.ids())
        session.compact_history(history.ids()[0], history.ids()[-1], history.generation,
            "Image and output capabilities checked. Re-open saved image at " + descriptor["path"]
            + "; long report artifact " + output_id + " has CHECK_CODE near character 20000. Do not guess values.")
        await session.suspend()
        (workspace / "phone.png").unlink()
        (workspace / "card.png").unlink()
        session = open_session(session.thread_id)
        await ask(session, "compact-resume", "恢复后重新核验：必须调用 view_image 打开 " + descriptor["path"]
            + " 并重新看图；必须调用 read_tool_output 读取 " + output_id
            + " 字符 20000 附近。用 send_user_message 分别发送 RESUME_IMAGE code=四位码 red=数量 和 RESUME_BUDGET code=报告代码。"
            "不要根据旧汇报或记忆猜答案，最后等待。")
        delivered(session, "RESUME_IMAGE", codes[1], 4)
        delivered(session, "RESUME_BUDGET", codes[2])
        outputs = [row["result"]["output"] for row in traces[1].rows if row["event"] == "tool_result"]
        assert any(isinstance(o, list) and any(p.get("image_url") == original_pixels for p in o) for o in outputs)
        calls = [row["call"] for row in traces[1].rows if row["event"] == "tool_started"]
        assert any(c.get("name") == "read_tool_output" for c in calls)
        result["checks"]["saved_image_and_output_work_after_compact_resume"] = True
    except BaseException as error:
        result["error"] = type(error).__name__ + ": " + str(error)
    finally:
        for session in reversed(sessions):
            await session.suspend()
        result["checks"]["phone_unchanged"] = before == service_state() and phone_hash == digest(phone)
        rows = [row for trace in traces for row in trace.rows]
        usage = [row["body"].get("usage", {}) for row in rows if row["event"] == "api_response"]
        result.update(seconds=round(time.monotonic() - start, 3), steps=len(usage),
            input_tokens=sum(u.get("input_tokens", 0) for u in usage),
            output_tokens=sum(u.get("output_tokens", 0) for u in usage))
        result["passed"] = "error" not in result and all(result["checks"].values())
        (run / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        build(run)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return result["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required for billable model calls")
    sys.exit(0 if asyncio.run(check()) else 1)
