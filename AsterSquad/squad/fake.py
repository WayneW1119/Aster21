"""Deterministic offline demo using real Aster tools, MCP and program events."""
import json
import uuid

from aster import ModelResponse


def unwrap(value):
    for _ in range(6):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return value
        elif isinstance(value, dict) and "content" in value:
            content = value["content"]
            value = next((part.get("text", "") for part in content if part.get("type") == "text"), "")
        else:
            return value
    return value


class DemoClient:
    def __init__(self, handle, user, connection=".connections/relay.json", watch=".relay/watch.py", namespace="relay"):
        self.handle, self.user = handle, user
        self.connection, self.watch = connection, watch
        self.namespace = "mcp__" + namespace
        self.phase = "skills"
        self.last_call = None
        self.discovery = ["relay_receive", "relay_send", "relay_send_group", "relay_ack"]

    def call(self, name, args=None, namespace=None):
        ident = uuid.uuid4().hex
        self.last_call = ident
        item = {"type": "function_call", "call_id": ident, "name": name, "arguments": json.dumps(args or {}, ensure_ascii=False)}
        if namespace:
            item["namespace"] = namespace
        return item

    def respond(self, prompt):
        items = prompt.input
        result = next((unwrap(i.get("output")) for i in reversed(items) if i.get("type") == "function_call_output" and i.get("call_id") == self.last_call), None)
        if self.phase == "skills":
            self.phase = "relay_skill"
            output = [self.call("read", {"skill_id": "squad"}, "skills")]
        elif self.phase == "relay_skill":
            self.phase = "generators"
            output = [self.call("read", {"skill_id": "relay"}, "skills")]
        elif self.phase == "generators":
            self.phase = "check"
            output = [self.call("list_event_generators")]
        elif self.phase == "check":
            if "watch.py" in json.dumps(result) and '"active"' in json.dumps(result):
                self.phase = "discover"
                return self.respond(prompt)
            self.phase = "register"
            output = [self.call("test_program_generator", {"script_path": self.watch, "args": ["--connection", self.connection, "--self-test"]})]
        elif self.phase == "register":
            self.phase = "discover"
            output = [self.call("register_program_generator", {"script_path": self.watch, "args": ["--connection", self.connection]})]
        elif self.phase == "discover":
            if not self.discovery:
                self.phase = "idle"
                return self.respond(prompt)
            name = self.discovery.pop(0)
            output = [{"type": "tool_search_call", "call_id": uuid.uuid4().hex, "execution": "client", "arguments": {"query": self.namespace + "__" + name, "limit": 8}}]
        elif self.phase == "receive":
            rows = result.get("messages", []) if isinstance(result, dict) else []
            output = []
            for row in rows:
                if row["sender"] == self.user:
                    target = {"group": row["group_id"]} if row.get("kind") == "group" else {"to": row["sender"]}
                    output.append(self.call("relay_send_group" if row.get("kind") == "group" else "relay_send", {**target, "text": f"[演示模式 · {self.handle}] 收到：{row['text']}", "client_id": f"demo-reply-{row['id']}"}, self.namespace))
            if rows:
                output.append(self.call("relay_ack", {"ids": [r["id"] for r in rows], "read": True}, self.namespace))
            else:
                output.append(self.call("wait_for_event"))
            self.phase = "idle"
        else:
            last = items[-1] if items else {}
            text = last.get("text", "")
            if last.get("role") == "user" and "<terminal_message>" in text:
                try:
                    body = json.loads(text.split("<terminal_message>", 1)[1].split("</terminal_message>", 1)[0])
                    output = [self.call("send_user_message", {"text": f"[演示模式 · {self.handle}] 终端收到：{body['text']}"})]
                except (ValueError, KeyError):
                    output = [self.call("wait_for_event")]
            elif last.get("type") == "function_call_output":
                output = [self.call("wait_for_event")]
            else:
                self.phase = "receive"
                output = [self.call("relay_receive", {}, self.namespace)]
        return ModelResponse(output)
