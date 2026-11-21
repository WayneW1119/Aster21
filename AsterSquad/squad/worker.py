"""One process owns one Aster Thread. Public SDK only; no private Session access."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from aster import Aster, ModelConfig, SessionConfiguration

from .fake import DemoClient
from .telemetry import trace_update


def output(event, **data):
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def provider_failure(event, data):
    """Translate known upstream codes; never publish arbitrary upstream bodies."""
    if event != "api_stream_event":
        return None
    data = data.get("data", data)
    if not isinstance(data, dict) or data.get("type") not in {"error", "response.failed", "response.incomplete"}:
        return None
    error = data.get("error") or (data.get("response") or {}).get("error") or {}
    return {
        "subscription_sharing_usage_limit_exceeded": "ChatGPT 订阅共享额度已用尽；额度恢复后可启动原成员继续，也可由用户配置 API 服务。",
        "insufficient_quota": "模型服务额度不足；请检查服务账户余额或配额后重新启动成员。",
        "rate_limit_exceeded": "模型服务暂时限流；稍后可启动原成员继续。",
    }.get(error.get("code")) if isinstance(error, dict) else None


def policy_receipt(event, data):
    """Only native runtime receipts, never text supplied by a host/program event."""
    queued = data.get("queued_event", {})
    if event != "event_enqueued" or queued.get("kind") not in {"filesystem_policy_applied", "filesystem_policy_failed"}:
        return None
    try:
        body = json.loads(queued["item"]["text"].removeprefix("<harness_event>").removesuffix("</harness_event>"))
    except (ValueError, KeyError, TypeError):
        return None
    return body if isinstance(body, dict) and body.get("source") == "runtime" and isinstance(body.get("data"), dict) else None


async def run(args):
    policy_requests = {}
    early_policy_receipts = {}
    mcp_requests = {}
    early_mcp_receipts = {}

    def report_mcp(native_id, event, data):
        receipt = {"ok": event == "mcp_configuration_applied", "details": {
            key: data[key] for key in ("servers", "unavailable", "error") if key in data}}
        if request_id := mcp_requests.pop(native_id, None):
            output("mcp_result", id=request_id, **receipt)
        else:
            early_mcp_receipts[native_id] = receipt
            if len(early_mcp_receipts) > 32:
                early_mcp_receipts.pop(next(iter(early_mcp_receipts)))

    def observe(event, data):
        if event in {"mcp_configuration_applied", "mcp_configuration_rejected"} and data.get("request_id"):
            report_mcp(data["request_id"], event, data)
            for native_id in data.get("superseded_request_ids", []):
                report_mcp(native_id, "mcp_configuration_superseded",
                           {"error": "MCP request superseded by " + data["request_id"]})
        if update := trace_update(event, data):
            output("runtime", data=update, model=config["model"])
        if message := provider_failure(event, data):
            args.provider_error = message
        if receipt := policy_receipt(event, data):
            details = receipt.get("data", {})
            native_id = details.get("request_id")
            if request_id := policy_requests.pop(native_id, None):
                output("policy_result", id=request_id, ok=receipt["kind"] == "filesystem_policy_applied", details=details)
            elif native_id:
                early_policy_receipts[native_id] = receipt
                if len(early_policy_receipts) > 32:
                    early_policy_receipts.pop(next(iter(early_policy_receipts)))

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if os.name == "nt":
        from .windows_admin import is_admin
        expected = config.get("windows_admin") is True
        if is_admin() != expected or (expected and config.get("sandbox_enabled", True)):
            raise RuntimeError("Windows worker token does not match the configured administrator mode")
    if config.get('run_as_root') and (os.name == 'nt' or os.geteuid() != 0):
        raise RuntimeError('root 身份未生效，拒绝按普通用户悄悄运行')
    home = Path(config["home"])
    aster = Aster(home)
    fake = config["mode"] == "fake"
    client = DemoClient(config["handle"], config["user_username"], config["relay_connection"], config["relay_watch"], config.get("relay_namespace", "relay")) if fake else None
    if config.get("thread"):
        # An error here is terminal and visible. Never silently replace a Thread.
        agent = aster.resume(config["thread"], client=client, trace=True, on_trace=observe)
    else:
        agent = aster.create(config["workspace"], client=client,
            model=None if fake else ModelConfig(**(config.get("model_config") or {
                "name": config["model"], "url": config["model_url"],
                "api_key_env": "SQUAD_MODEL_KEY", "allow_http": config["allow_http"]})),
            instructions=Path(config["instructions"]).read_text(encoding="utf-8"), name="agent-" + config["id"], trace=True, on_trace=observe,
            configuration=SessionConfiguration(max_consecutive_steps=None,
                filesystem_policy={"rules": config["rules"], **({key: True for key, source in (('gpu', 'gpu_access'), ('local_ipc', 'local_ipc')) if config.get(source)})} if not fake and config.get("sandbox_enabled", True) else None,
                workspace_skills=False, skill_sources=tuple(config["skills"]),
                mcp_servers=config.get("mcp_servers", {
                    "squad": {"url": config["squad_mcp"], "bearer_token_env_var": "SQUAD_TOKEN", "required": True},
                    "relay": {"url": config["relay_mcp"], "bearer_token_env_var": "RELAY_TOKEN", "required": True},
                })))
    # Persist identity before startup; ready means Session startup actually succeeded.
    if config.get('run_as_root'):
        from .root_worker import restore_ownership
        restore_ownership(home, int(os.environ['SQUAD_ROOT_OWNER_UID']), int(os.environ['SQUAD_ROOT_OWNER_GID']))
    output("thread", thread=agent.thread_id)
    if "suites" in config:
        await agent.configure_suites(config["suites"], owner="squad", grants=config["suite_grants"], allow_connect=config.get("allow_connect", False))
    async with agent.messages() as messages, agent:
        output("ready", thread=agent.thread_id, session=agent.session_id, mcp_status=agent.mcp_status,
               capabilities={"live_filesystem_policy": callable(getattr(agent, "set_filesystem_policy", None)),
                             "live_mcp": callable(getattr(agent, "set_mcp_servers", None))})
        if not config.get("thread") or "suites" not in config:
            await agent.emit("squad_started", {"instruction": "检查 squad 和 relay skills，确认监听已建立，继续未完成职责。"})

        async def pump():
            async for message in messages:
                output("terminal", text=message.text)

        async def reader():
            transport = None
            if os.name == 'nt':
                from squad._windows import StdinLines
                reader = StdinLines()
            else:
                reader = asyncio.StreamReader(limit=1024 * 1024)
                protocol = asyncio.StreamReaderProtocol(reader)
                transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
            try:
                while line := await reader.readline():
                    command = json.loads(line)
                    if command["op"] == "stop":
                        return
                    if command["op"] == "suites":
                        try:
                            await agent.configure_suites(command["installations"], owner="squad", grants=command["grants"],
                                                        allow_connect=command["allow_connect"])
                        except (ValueError, RuntimeError) as error:
                            output("mcp_result", id=command["id"], ok=False, details={"error": str(error)})
                        else:
                            output("mcp_result", id=command["id"], ok=True, details=agent.mcp_status)
                        continue
                    if command["op"] == "mcp_servers":
                        try:
                            native_id = await agent.set_mcp_servers(command["servers"])
                            if receipt := early_mcp_receipts.pop(native_id, None):
                                output("mcp_result", id=command["id"], **receipt)
                            else:
                                mcp_requests[native_id] = command["id"]
                        except Exception as error:
                            output("mcp_result", id=command["id"], ok=False if isinstance(error, (ValueError, TypeError)) else None, details={"error": str(error)})
                        continue
                    if command["op"] == "filesystem_policy":
                        try:
                            native_id = await agent.set_filesystem_policy(command["policy"])
                            policy_requests[native_id] = command["id"]
                            output("policy_queued", id=command["id"], native_id=native_id)
                            if receipt := early_policy_receipts.pop(native_id, None):
                                policy_requests.pop(native_id, None)
                                output("policy_result", id=command["id"], ok=receipt["kind"] == "filesystem_policy_applied", details=receipt["data"])
                        except Exception as error:
                            output("policy_result", id=command["id"], ok=False if isinstance(error, (ValueError, TypeError)) else None, details={"error": str(error)})
                        continue
                    if command["op"] == "terminal":
                        await agent.send(command["text"])
                    else:
                        await agent.emit("squad_management_message", command["message"])
                    output("accepted", id=command["id"])
            finally:
                if transport is not None:
                    transport.close()

        tasks = {asyncio.create_task(pump()), asyncio.create_task(reader())}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    output("closed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as error:
        output("error", error=getattr(args, "provider_error", None) or f"{type(error).__name__}: {error}")
        raise


if __name__ == "__main__":
    main()
