"""Real processes, public Aster SDK, MCP, generator and HTTP; no paid model."""
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import aiohttp
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parents[1]


def free_ports():
    sockets = [socket.socket() for _ in range(4)]
    for sock in sockets:
        sock.bind(("127.0.0.1", 0))
    ports = [sock.getsockname()[1] for sock in sockets]
    for sock in sockets:
        sock.close()
    return ports


class SystemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="squad-test-")
        self.root = Path(self.temp.name)
        self.web, self.mcp, self.relay, self.relay_mcp = free_ports()
        self.log = (self.root / "server.log").open("w")
        self.proc = subprocess.Popen([sys.executable, "-m", "squad", "--root", str(self.root),
            "--port", str(self.web), "--mcp-port", str(self.mcp), "--relay-port", str(self.relay),
            "--relay-mcp-port", str(self.relay_mcp)], cwd=ROOT, stdout=self.log, stderr=self.log, start_new_session=True)
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
        for _ in range(100):
            try:
                self.token = (self.root / "control/owner.token").read_text()
                status, data = await self.request(self.web, "GET", "state", self.token)
                if status == 200:
                    break
            except (OSError, aiohttp.ClientError):
                pass
            await asyncio.sleep(.1)
        else:
            self.fail((self.root / "server.log").read_text())
        self.relay_token = (self.root / "control/relay/owner.token").read_text()

    async def asyncTearDown(self):
        await self.http.close()
        __import__('squad._windows',fromlist=['request_stop']).request_stop(self.proc) if os.name == 'nt' else self.proc.terminate()
        try:
            await asyncio.to_thread(self.proc.wait, 40)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            await asyncio.to_thread(self.proc.wait)
        self.log.close()
        self.temp.cleanup()

    async def request(self, port, method, path, token="", body=None):
        async with self.http.request(method, f"http://127.0.0.1:{port}/api/{path}",
            headers={"Authorization": "Bearer " + token}, json=body) as response:
            return response.status, await response.json()

    async def owner(self, path, body=None):
        status, result = await self.request(self.web, "POST" if body is not None else "GET", path, self.token, body)
        self.assertEqual(status, 200, result)
        return result

    async def tool(self, port, token, name, args=None, error=False):
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp", headers={"Authorization": "Bearer " + token}) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, args or {})
                self.assertEqual(bool(result.isError), error, result.content)
                return json.loads(result.content[0].text) if not error else result.content[0].text

    def credentials(self, ident):
        return json.loads((self.root / "control/agents" / ident / "credentials.json").read_text())

    async def wait_reply(self, peer, count):
        for _ in range(100):
            _, data = await self.request(self.relay, "GET", "messages?peer=" + peer, self.relay_token)
            if len(data["messages"]) >= count and all(r["read"] for r in data["messages"] if r["recipient"] == peer):
                return data["messages"]
            await asyncio.sleep(.15)
        logs = [p.read_text()[-2000:] for p in self.root.glob("control/agents/*/worker.log")]
        self.fail("No reply: " + repr(logs))

    async def test_two_relay_identities_through_agent_managed_connections(self):
        from aster import Harness, ScriptedClient, SessionConfiguration
        _, domain = await self.request(self.relay, "GET", "state", self.relay_token)
        endpoint = domain["domain"]["mcp_url"]
        self.assertEqual(endpoint, f"http://127.0.0.1:{self.relay_mcp}/mcp")
        work = self.root / "guest"
        work.mkdir()
        script, tokens = [], []
        def tool(name, call_id, arguments, **extra):
            return {"type": "function_call", "name": name, "call_id": call_id,
                    "arguments": json.dumps(arguments), **extra}
        for username in ("first", "second"):
            status, issued = await self.request(self.relay, "POST", "accounts", self.relay_token,
                                                {"username": username, "name": username})
            self.assertEqual(status, 200)
            tokens.append(issued["token"])
            credential = work / (username + ".json")
            credential.write_text(json.dumps({"mcp_url": endpoint, "token": issued["token"]}))
            alias = "relay_lab_" + username
            script.extend([
                [tool("connect_mcp", "connect_" + username, {
                    "url": endpoint, "name": alias, "credential_ref": str(credential)})],
                [{"type": "tool_search_call", "execution": "client", "call_id": "find_" + username,
                  "arguments": {"query": "mcp__" + alias + "__relay_send", "limit": 16}}],
                [tool("relay_send", "send_" + username, {"to": "user", "text": "from " + username},
                      namespace="mcp__" + alias)]])
        script.append([tool("send_user_message", "done", {"text": "done", "wake_model": False})])
        agent = Harness(self.root / "native").create(work, client=ScriptedClient(script),
            configuration=SessionConfiguration(filesystem_policy={"rules": []} if sys.platform.startswith("linux") else None))
        self.addAsyncCleanup(agent.close)
        await agent.configure_capabilities(allow_connect=True)
        await agent.start()
        async with agent.messages() as messages:
            await agent.send("Send from both accounts")
            self.assertEqual((await asyncio.wait_for(anext(messages), 15)).text, "done")
        self.assertEqual(len(agent.mcp_status["servers"]), 2)
        for username in ("first", "second"):
            _, result = await self.request(self.relay, "GET", "messages?peer=" + username, self.relay_token)
            self.assertEqual([row["text"] for row in result["messages"]], ["from " + username])
        for token in tokens:
            self.assertNotIn(token, (agent.thread_path / "rollout.jsonl").read_text())

    async def test_offline_error_configuration_save(self):
        import sqlite3
        member=await self.owner("agents",{"handle":"failedmember","instruction":"Test offline config."})
        await self.owner("agents/"+member["id"]+"/stop",{})
        with sqlite3.connect(self.root/"control/squad.db") as db:
            db.execute("UPDATE agents SET status='error',error=? WHERE id=?",("Could not reach model endpoint",member["id"]))
        target=self.root/"external-project"
        result=await self.owner("agents/"+member["id"]+"/configure",{"grants":[{"path":str(target),"access":"write"}]})
        self.assertEqual(result["thread"],member["thread"])
        self.assertEqual(result["status"],"error")
        self.assertEqual(result["error"],"Could not reach model endpoint")
        self.assertEqual(result["config"]["grants"],[{"path":str(target),"access":"write"}])
        self.assertTrue(target.is_dir())

    async def test_owner_sandbox_toggle_preserves_grants_and_child_defaults(self):
        external=self.root/'external';external.mkdir()
        grants=[{"path":str(external),"access":"write"}]
        member=await self.owner("agents",{"handle":"sandboxprobe","instruction":"Test toggle.","sandbox_enabled":False,"grants":grants})
        token=json.loads((self.root/"control/agents"/member["id"]/"credentials.json").read_text())["squad"]
        self.assertFalse(member["config"]["sandbox_enabled"])
        child=await self.tool(self.mcp,token,"squad_create",{"handle":"safechild","instruction":"Child defaults."})
        self.assertTrue(child["config"]["sandbox_enabled"])
        for enabled in (True,False,True):
            result=await self.owner("agents/"+member["id"]+"/configure",{"sandbox_enabled":enabled})
            self.assertEqual(result["thread"],member["thread"])
            self.assertEqual(result["config"]["sandbox_enabled"],enabled)
            self.assertEqual(result["config"]["grants"],grants)

    async def test_live_permissions_keep_worker_and_thread(self):
        member=await self.owner("agents",{"handle":"liveprobe","instruction":"Test live permissions."})
        ident=member['id'];target=self.root/'external';target.mkdir()
        def worker_pid():
            expected=str(self.root/'control/agents'/ident/'worker.json').encode()
            matches=[]
            for path in Path('/proc').glob('[0-9]*/cmdline'):
                try:args=path.read_bytes().split(b'\0')
                except (OSError,ProcessLookupError):continue
                if b'squad.worker' in args and expected in args:matches.append(int(path.parent.name))
            self.assertEqual(len(matches),1,matches)
            return matches[0]
        for _ in range(50):
            current=next(a for a in (await self.owner('state'))['agents'] if a['id']==ident)
            if current['telemetry'].get('phase')=='waiting' and 'cache_window' in current['telemetry']:break
            await asyncio.sleep(.1)
        self.assertEqual(current['telemetry']['phase'],'waiting',current['telemetry'])
        self.assertIn('cache_window',current['telemetry'])
        self.assertIsNone(current['telemetry']['latest']['input_tokens'])
        pid=worker_pid()
        changes=[{'grants':[{'path':str(target),'access':access}]} for access in ('write','read','deny')]
        changes += [{'sandbox_enabled':False},{'sandbox_enabled':True},{'grants':[]}]
        for change in changes:
            result=await self.owner('agents/'+ident+'/configure',change)
            self.assertEqual(result['configuration_update'],'live',result)
            self.assertEqual(result['permission_update']['status'],'applied')
            self.assertEqual(result['thread'],member['thread'])
            self.assertEqual(worker_pid(),pid)

    async def test_owner_systemd_grant_and_mcp_denial(self):
        member = await self.owner("agents", {"handle": "ops", "instruction": "Operate test services."})
        token = json.loads((self.root / "control/agents" / member["id"] / "credentials.json").read_text())["squad"]
        await self.tool(self.mcp, token, "squad_host_systemd", {"program": "systemctl", "arguments": ["status"]}, error=True)
        result = await self.owner("agents/" + member["id"] + "/configure", {"host_systemd": True})
        self.assertTrue(result["config"]["host_systemd"])
        self.assertEqual(result["thread"], member["thread"])
        # Even a granted fake agent must not mutate host services.
        await self.tool(self.mcp, token, "squad_host_systemd", {"program": "systemctl", "arguments": ["status"]}, error=True)
        await self.owner("agents/" + member["id"] + "/configure", {"host_systemd": False})

    async def test_chat_management_reconfigure_resume_and_permissions(self):
        status, _ = await self.request(self.web, "POST", "agents", "", {"handle": "forged", "instruction": "hi"})
        self.assertEqual(status, 401)
        alice = await self.owner("agents", {"handle": "alice", "instruction": "你负责研究，先阅读 skills 并接入 Relay。"})
        self.assertEqual(alice["status"], "online")
        aid, account, thread = alice["id"], alice["relay_account"], alice["thread"]
        credentials = self.credentials(aid)
        who = await self.tool(self.mcp, credentials["squad"], "squad_self")
        self.assertEqual(who["agent"]["id"], aid)
        self.assertNotIn(credentials["relay"], alice["instruction"])
        await self.tool(self.mcp, credentials["squad"], "squad_stop", {"agent": aid}, error=True)
        _, sent = await self.request(self.relay, "POST", "messages", self.relay_token,
                                     {"to": account, "text": "hello through Relay", "client_id": "first"})
        rows = await self.wait_reply(account, 2)
        self.assertIn("hello through Relay", rows[-1]["text"])
        self.assertEqual(rows[0]["read"], 1)
        # MCP inbox cannot forge a different caller by passing a sender field.
        relay_me = await self.tool(self.relay_mcp, credentials["relay"], "relay_me")
        self.assertEqual(relay_me["account"]["username"], "alice")
        self.assertNotIn("id", relay_me["account"])
        public_accounts = await self.tool(self.relay_mcp, credentials["relay"], "relay_accounts")
        self.assertTrue(all("id" not in a for a in public_accounts["accounts"]))
        chat_account = await self.tool(self.relay_mcp, credentials["relay"], "relay_create_account", {"username": "chat-only", "name": "Chat", "can_create": False})
        self.assertEqual(chat_account["account"]["username"], "chat-only")
        self.assertNotIn("id", chat_account["account"])
        await self.tool(self.relay_mcp, credentials["relay"], "relay_create_account", {"username": "CHAT-ONLY"}, error=True)
        await self.tool(self.relay_mcp, credentials["relay"], "relay_manage_account", {"account": "@chat-only", "action": "disable"})
        await self.tool(self.relay_mcp, credentials["relay"], "relay_send", {"to": "@user", "text": "username addressing", "client_id": "username-direct"})
        public_history = await self.tool(self.relay_mcp, credentials["relay"], "relay_history", {"peer": "user"})
        self.assertEqual(public_history["messages"][-1]["sender"], "alice")
        # Exercise the standalone group MCP surface without involving an agent.
        group_user = await self.tool(self.relay_mcp, self.relay_token, "relay_create_account", {"username": "group-only", "can_create": False})
        group = await self.tool(self.relay_mcp, self.relay_token, "relay_create_group", {"name": "MCP group", "members": ["group-only"]})
        self.assertEqual(group["owner"], "user")
        self.assertNotIn("id", group["members"][0])
        listed = await self.tool(self.relay_mcp, group_user["token"], "relay_groups")
        self.assertEqual(listed["groups"][0]["id"], group["id"])
        await self.tool(self.relay_mcp, self.relay_token, "relay_send_group", {"group": group["id"], "text": "Group queue", "client_id": "group-mcp"})
        received = await self.tool(self.relay_mcp, group_user["token"], "relay_receive")
        self.assertEqual(received["messages"][0]["group_id"], group["id"])
        await self.tool(self.relay_mcp, group_user["token"], "relay_ack", {"ids": [received["messages"][0]["id"]], "read": True})
        group_history = await self.tool(self.relay_mcp, group_user["token"], "relay_group_history", {"group": group["id"]})
        self.assertEqual(group_history["messages"][0]["sender"], "user")
        await self.tool(self.relay_mcp, self.relay_token, "relay_manage_group", {"group": group["id"], "action": "mute", "member": "group-only"})
        await self.tool(self.relay_mcp, group_user["token"], "relay_send_group", {"group": group["id"], "text": "blocked"}, error=True)
        await self.tool(self.relay_mcp, self.relay_token, "relay_manage_group", {"group": group["id"], "action": "invite", "members": ["alice"]})
        await self.tool(self.relay_mcp, self.relay_token, "relay_send_group", {"group": group["id"], "text": "agent group routing"})
        for _ in range(100):
            _, history = await self.request(self.relay, "GET", f"groups/{group['id']}/messages", self.relay_token)
            if any(m["text"] == "[演示模式 · alice] 收到：agent group routing" for m in history["messages"]):
                break
            await asyncio.sleep(.15)
        else:
            self.fail("Scripted agent did not reply in the originating group")
        await self.tool(self.relay_mcp, self.relay_token, "relay_manage_group", {"group": group["id"], "action": "dissolve"})
        kid = await self.tool(self.mcp, credentials["squad"], "squad_create", {"handle": "kid", "instruction": "负责核对来源。"})
        self.assertEqual(kid["parent"], aid)
        await self.tool(self.mcp, credentials["squad"], "squad_configure", {"agent": kid["id"], "host_systemd": True}, error=True)
        await self.owner("agents/" + aid + "/configure", {"host_systemd": True})
        delegated = await self.tool(self.mcp, credentials["squad"], "squad_configure", {"agent": kid["id"], "host_systemd": True})
        self.assertTrue(delegated["config"]["host_systemd"])
        await self.tool(self.mcp, credentials["squad"], "squad_configure", {"agent": kid["id"], "host_systemd": False})

        await self.tool(self.mcp, credentials["squad"], "squad_configure", {"agent": kid["id"], "instruction": "负责校验数据。"})
        kid_creds = self.credentials(kid["id"])
        await self.tool(self.mcp, kid_creds["squad"], "squad_stop", {"agent": aid}, error=True)
        with tempfile.TemporaryDirectory() as external:
            await self.tool(self.mcp, credentials["squad"], "squad_configure", {"agent": kid["id"], "grants": [{"path": external, "access": "write"}]}, error=True)
            await self.owner("agents/" + aid + "/configure", {"grants": [{"path": external, "access": "write"}]})
            await self.tool(self.mcp, credentials["squad"], "squad_configure", {"agent": kid["id"], "grants": [{"path": external, "access": "read"}]})
            status, _ = await self.request(self.web, "POST", "agents/" + aid + "/configure", self.token, {"grants": []})
            self.assertEqual(status, 409)
            await self.tool(self.mcp, credentials["squad"], "squad_configure", {"agent": kid["id"], "grants": []})
            await self.owner("agents/" + aid + "/configure", {"grants": []})
        await self.owner("agents/" + aid + "/configure", {"instruction": "更新后的长期职责。"})
        state = await self.owner("state")
        updated = next(a for a in state["agents"] if a["id"] == aid)
        self.assertEqual(updated["thread"], thread)
        self.assertIn("更新后的长期职责", updated["instruction"])
        actual = self.root / "control/aster/threads" / thread / "instructions.md"
        self.assertIn("更新后的长期职责", actual.read_text())
        await self.owner("agents/" + aid + "/stop", {})
        await self.request(self.relay, "POST", "messages", self.relay_token, {"to": account, "text": "offline message", "client_id": "offline"})
        await self.owner("agents/" + aid + "/start", {})
        rows = await self.wait_reply(account, 4)
        self.assertIn("offline message", rows[-1]["text"])
        self.assertEqual(sum("offline message" in r["text"] and r["sender"] == account for r in rows), 1)
        await self.owner("agents/" + kid["id"] + "/stop", {})
        msg = await self.tool(self.mcp, credentials["squad"], "squad_send", {"to": kid["id"], "text": "offline management task"})
        self.assertTrue(msg["stored"])
        await self.owner("agents/" + kid["id"] + "/start", {})
        await asyncio.sleep(.5)
        inbox = await self.tool(self.mcp, kid_creds["squad"], "squad_inbox")
        self.assertEqual(inbox["messages"][0]["accepted"], 1)
        await self.tool(self.relay_mcp, self.relay_token, "relay_manage_account", {"account": account, "action": "reset_token"})
        status, _ = await self.request(self.relay, "GET", "state", credentials["relay"])
        self.assertEqual(status, 401)
        await self.owner("agents/" + aid + "/reconnect", {})
        await self.request(self.relay, "POST", "messages", self.relay_token, {"to": account, "text": "after reconnect", "client_id": "reconnected"})
        rows = await self.wait_reply(account, 6)
        self.assertIn("after reconnect", rows[-1]["text"])
        # The server provides no destructive new-thread fallback.
        await self.owner("agents/" + aid + "/stop", {})
        config_path = self.root / "control/aster/threads" / thread / "thread.toml"
        original = config_path.read_text()
        config_path.write_text("broken toml = [")
        status, _ = await self.request(self.web, "POST", "agents/" + aid + "/start", self.token, {})
        self.assertEqual(status, 400)
        config_path.write_text(original)
        current = next(a for a in (await self.owner("state"))["agents"] if a["id"] == aid)
        self.assertEqual(current["thread"], thread)


if __name__ == "__main__":
    unittest.main()
