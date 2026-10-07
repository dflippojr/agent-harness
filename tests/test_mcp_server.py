"""Harness tools for hosted Claude Code (#300) and Codex (#373) over MCP."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import sys

import pytest

from harness.cli_backends import ClaudeSession, CliBackendError, CodexSession, CursorSession
from harness.config import BackendConfig, SandboxConfig
from harness.manager import Manager
from harness import cli_domains
from harness.mcp_server import (RELAY_PORT, RELAY_SCRIPT, TOKEN_ENV, McpRelay, McpServer, McpTokens,
                                codex_mcp_overrides, mcp_config, relay_node_args)
from harness_modules.search.service import SessionSearch
from harness.policy import ALLOW, ASK, DENY, ChatPolicy, Policy, mcp_harness_tool
from test_daemon import events, make_cfg, wait_status


# policy

def test_mcp_harness_tools_are_decided_as_the_native_tool():
    policy = Policy()
    assert mcp_harness_tool("mcp__harness__web_search") == "web_search"
    assert mcp_harness_tool("mcp__other__web_search") is None
    assert mcp_harness_tool("mcp__harness__") is None
    assert policy.decide("mcp__harness__web_search", {"query": "x"}).action == ALLOW
    assert policy.decide("mcp__harness__session_search", {"query": "x"}).action == ALLOW
    write = policy.decide("mcp__harness__memory_write", {"path": "a.md"})
    assert write.action == ASK and write.reason == "changes your memory library"
    assert policy.decide("mcp__harness__memory_edit", {}).action == ASK
    other = policy.decide("mcp__github__create_issue", {})
    assert other.action == DENY and "harness MCP server" in other.reason
    assert ChatPolicy().decide("mcp__harness__web_search", {}).action == DENY


def test_project_rules_cover_mcp_by_native_or_full_name():
    native = Policy([{"tool": "web_fetch", "action": "deny", "reason": "no fetching"}])
    assert native.decide("mcp__harness__web_fetch", {"url": "https://x"}).action == DENY
    full = Policy([{"tool": "mcp__harness__session_search", "action": "ask", "reason": "check"}])
    assert full.decide("mcp__harness__session_search", {}).action == ASK
    # A project rule can't allow a memory write without asking, through MCP either.
    assert Policy([{"tool": "*", "action": "allow"}]).decide("mcp__harness__memory_write", {}).action == ASK


# tokens and the HTTP handler

def _request(token: str | None, body, method: str = "POST", path: str = "/mcp", **headers) -> dict:
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return {"id": 1, "method": method, "path": path, "headers": headers,
            "body": body if isinstance(body, str) else json.dumps(body)}


def _server(calls: list | None = None):
    tokens = McpTokens()
    schemas = {"s1": [{"type": "function", "function": {"name": "web_search", "description": "Search",
                                                        "parameters": {"type": "object"}}}]}

    async def call(sid, name, args, tool_use_id):
        if calls is not None:
            calls.append((sid, name, args, tool_use_id))
        return f"{sid}:{name}:{args.get('query')}", True
    return tokens, McpServer(tokens, lambda sid: schemas.get(sid, []), call)


def test_tokens_are_per_session_and_revocable():
    tokens = McpTokens()
    one, two = tokens.mint("s1"), tokens.mint("s2")
    assert one != two and len(one) >= 32
    assert tokens.session_for(one) == "s1" and tokens.session_for(two) == "s2"
    assert tokens.session_for("") is None and tokens.session_for("guess") is None
    again = tokens.mint("s1")
    assert tokens.session_for(one) is None and tokens.session_for(again) == "s1"
    tokens.revoke("s1")
    assert tokens.session_for(again) is None and tokens.session_for(two) == "s2"


def test_handler_rejects_missing_foreign_and_revoked_tokens():
    async def body():
        calls = []
        tokens, server = _server(calls)
        mine, theirs = tokens.mint("s1"), tokens.mint("s2")
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert (await server.handle("s1", _request(None, rpc)))["status"] == 401
        assert (await server.handle("s1", _request("nope", rpc)))["status"] == 401
        assert (await server.handle("s1", _request(theirs, rpc)))["status"] == 403
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "web_search", "arguments": {"query": "q"}}}
        assert (await server.handle("s1", _request(theirs, call)))["status"] == 403
        assert calls == []
        assert (await server.handle("s1", _request(mine, rpc)))["status"] == 200
        tokens.revoke("s1")
        assert (await server.handle("s1", _request(mine, rpc)))["status"] == 401
        assert (await server.handle("s1", _request(mine, rpc, Origin="http://evil")))["status"] == 403
    asyncio.run(body())


def test_handler_speaks_streamable_http_json_rpc():
    async def body():
        calls = []
        tokens, server = _server(calls)
        token = tokens.mint("s1")
        init = await server.handle("s1", _request(token, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                          "params": {"protocolVersion": "2025-03-26"}}))
        result = json.loads(init["body"])["result"]
        assert result["protocolVersion"] == "2025-03-26" and result["serverInfo"]["name"] == "harness"
        assert init["headers"]["Content-Type"] == "application/json"
        note = await server.handle("s1", _request(token, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        assert note["status"] == 202 and note["body"] == ""
        listed = await server.handle("s1", _request(token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        assert json.loads(listed["body"])["result"]["tools"] == [
            {"name": "web_search", "description": "Search", "inputSchema": {"type": "object"}}]
        called = await server.handle("s1", _request(token, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "web_search", "arguments": {"query": "q"}, "_meta": {"claudecode/toolUseId": "tu-1"}}}))
        assert json.loads(called["body"])["result"] == {"content": [{"type": "text", "text": "s1:web_search:q"}],
                                                        "isError": False}
        assert calls == [("s1", "web_search", {"query": "q"}, "tu-1")]
        unknown = await server.handle("s1", _request(token, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                                             "params": {"name": "run_shell", "arguments": {}}}))
        assert json.loads(unknown["body"])["result"]["isError"] is True and len(calls) == 1
        batch = await server.handle("s1", _request(token, [{"jsonrpc": "2.0", "id": 5, "method": "ping"},
                                                           {"jsonrpc": "2.0", "method": "notifications/x"}]))
        assert json.loads(batch["body"]) == [{"jsonrpc": "2.0", "id": 5, "result": {}}]
        missing = await server.handle("s1", _request(token, {"jsonrpc": "2.0", "id": 6, "method": "resources/list"}))
        assert json.loads(missing["body"])["error"]["code"] == -32601
        assert (await server.handle("s1", _request(token, "{not json")))["status"] == 400
        assert (await server.handle("s1", _request(token, "", method="GET")))["status"] == 405
        assert (await server.handle("s1", _request(token, {}, path="/other")))["status"] == 404
    asyncio.run(body())


# docker commands

def _backend(**kw):
    return BackendConfig(enabled=True, proxy="http://proxy:8888", volume="auth", network="cli-net", **kw)


def test_claude_command_joins_the_relay_namespace_and_only_names_the_token(tmp_path):
    relay = McpRelay(session_id="abc", backend=_backend(), server=None)
    cli = ClaudeSession(session_id="abc", workspace=tmp_path, backend=_backend(), sandbox=SandboxConfig(),
                        system_prompt="system", mcp=relay, mcp_token="secret-token")
    command = cli.command()
    assert command[command.index("--network") + 1] == "container:harness-abc-mcp"
    image = command.index(_backend().image)
    assert command[image - 2:image] == ["-e", TOKEN_ENV]
    config = json.loads(command[command.index("--mcp-config") + 1])
    assert config == {"mcpServers": {"harness": {"type": "http", "url": "http://127.0.0.1:8790/mcp",
                                                 "headers": {"Authorization": "Bearer ${HARNESS_MCP_TOKEN}"}}}}
    assert "--strict-mcp-config" in command
    assert not any("secret-token" in arg for arg in command)
    assert ["--permission-prompt-tool", "stdio"] == command[command.index("--permission-prompt-tool"):][:2]
    plain = ClaudeSession(session_id="abc", workspace=tmp_path, backend=_backend(), sandbox=SandboxConfig(),
                          system_prompt="system").command()
    assert plain[plain.index("--network") + 1] == "cli-net"
    assert TOKEN_ENV not in plain
    # No relay: no MCP server at all, and --strict-mcp-config still ignores user-scope servers in the state (#371).
    assert json.loads(plain[plain.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert "--strict-mcp-config" in plain


def test_relay_runs_unprivileged_on_the_session_network_with_loopback_only():
    relay = McpRelay(session_id="abc", backend=_backend(), server=None)
    command = relay.command()
    assert command[command.index("--name") + 1] == "harness-abc-mcp"
    assert command[command.index("--network") + 1] == "cli-net"
    assert "agent-harness.session=abc" in command
    assert ["--cap-drop", "ALL"] == command[command.index("--cap-drop"):][:2]
    assert "no-new-privileges" in command
    assert not any(arg in ("-p", "--publish", "-P") for arg in command)
    assert '"127.0.0.1"' in RELAY_SCRIPT.read_text(encoding="utf-8")
    assert "${HARNESS_MCP_TOKEN}" in mcp_config()


def test_codex_command_joins_the_relay_namespace_and_only_names_the_token(tmp_path):
    relay = McpRelay(session_id="abc", backend=_backend(), server=None)
    cli = CodexSession(session_id="abc", workspace=tmp_path, backend=_backend(), sandbox=SandboxConfig(),
                       system_prompt="system", mcp=relay, mcp_token="secret-token", api_key="sk-1")
    command = cli.command()
    assert command[command.index("--network") + 1] == "container:harness-abc-mcp"
    image = command.index(_backend().image)
    assert command[image - 4:image] == ["-e", "OPENAI_API_KEY", "-e", TOKEN_ENV]
    assert command[image + 1:image + 4] == ["codex", "app-server", "--stdio"]
    assert command[image + 4:] == codex_mcp_overrides()
    assert command[-1] == ('mcp_servers.harness={url="http://127.0.0.1:8790/mcp", '
                           'bearer_token_env_var="HARNESS_MCP_TOKEN", default_tools_approval_mode="prompt", '
                           'startup_timeout_sec=30}')
    assert not any("secret-token" in arg or "sk-1" in arg for arg in command)
    # The server is registered with -c for this process only: CODEX_HOME's volume and read-only files are the
    # same mounts as without MCP, and the harness's config.toml registers nothing.
    plain = CodexSession(session_id="abc", workspace=tmp_path, backend=_backend(), sandbox=SandboxConfig(),
                         system_prompt="system").command()
    assert plain[plain.index("--network") + 1] == "cli-net"
    assert TOKEN_ENV not in plain and not any("mcp_servers" in arg for arg in plain)
    domain = cli_domains.docker_args("codex", _backend())
    for args in (command, plain):
        at = args.index(domain[1]) - 1
        assert args[at:at + len(domain)] == domain
    assert "mcp_servers" not in (cli_domains.CLI_HOME / "codex" / "config.toml").read_text(encoding="utf-8")


def test_cursor_gets_no_mcp_endpoint(tmp_path):
    command = CursorSession(session_id="abc", workspace=tmp_path, backend=_backend(), sandbox=SandboxConfig(),
                            system_prompt="system").command()
    assert TOKEN_ENV not in command and not any("mcp_servers" in arg or "harness-abc-mcp" in arg for arg in command)
    assert command[command.index("--network") + 1] == "cli-net"


# end to end: fake Claude CLI -> real node relay -> daemon

NODE = shutil.which("node")

FAKE_MCP_CLAUDE = r'''import json
import os
import pathlib
import sys
import urllib.request

port, state, plan = int(sys.argv[1]), pathlib.Path(sys.argv[2]), json.loads(sys.argv[3])
token = os.environ.get("HARNESS_MCP_TOKEN", "")

def log(*parts):
    with state.open("a", encoding="utf-8") as f:
        f.write(json.dumps(parts) + "\n")

def read():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    return json.loads(line)

def send(item):
    print(json.dumps(item), flush=True)

def rpc(method, params=None, rid=1, bearer=None):
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token if bearer is None else bearer}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None

read()
send({"type": "control_response", "response": {"subtype": "success", "request_id": "init-1", "response": {}}})
read()
send({"type": "system", "subtype": "init", "session_id": "claude-mcp-1", "model": "claude-opus-5"})
log("initialize", rpc("initialize", {"protocolVersion": "2025-06-18"})[1]["result"]["serverInfo"]["name"])
log("tools", sorted(t["name"] for t in rpc("tools/list")[1]["result"]["tools"]))
log("bad_token", rpc("tools/list", bearer="wrong")[0])
for n, step in enumerate(plan):
    tid = f"toolu-{n}"
    name = "mcp__harness__" + step["tool"]
    send({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name,
                                                        "input": step["input"]}]}})
    allowed = True
    if not step.get("skip_ask"):
        send({"type": "control_request", "request_id": f"req-{n}", "request": {
            "subtype": "can_use_tool", "tool_name": name, "input": step["input"], "tool_use_id": tid}})
        reply = read()["response"]["response"]
        allowed = reply["behavior"] == "allow"
    if allowed:
        status, body = rpc("tools/call", {"name": step["tool"], "arguments": step["input"],
                                          "_meta": {"claudecode/toolUseId": tid}}, rid=10 + n)
        result = body["result"]
        text, error = result["content"][0]["text"], result["isError"]
    else:
        text, error = reply.get("message", "denied"), True
    log("call", step["tool"], error, text)
    send({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                                   "content": text, "is_error": error}]}})
send({"type": "result", "subtype": "success", "result": "done", "num_turns": 1,
      "usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0})
'''


class FakeWeb:
    tool_names = {"web_search", "web_fetch"}

    def __init__(self):
        self.calls = []

    def schemas(self):
        return [{"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object"}}}
                for n in sorted(self.tool_names)]

    async def call(self, name, args):
        self.calls.append((name, args))
        return f"results for {args.get('query')}"


def test_relay_runs_with_the_node_arguments_production_uses():
    command = McpRelay(session_id="abc", backend=_backend(), server=None).command()
    assert command[command.index(_backend().image) + 1:] == relay_node_args(RELAY_PORT)


def _start_relay(*args):
    import subprocess
    return subprocess.run([NODE, *args], input="", capture_output=True, text=True, timeout=30)


@pytest.mark.skipif(NODE is None, reason="node is needed to run the relay")
@pytest.mark.parametrize("launch", ["eval", "file"])
def test_relay_reads_its_port_from_the_last_argument(launch):
    import subprocess
    port = _free_port()
    args = relay_node_args(port) if launch == "eval" else [str(RELAY_SCRIPT), str(port)]
    process = subprocess.Popen([NODE, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert json.loads(process.stdout.readline()) == {"ready": port}
    finally:
        process.kill()
        process.wait(timeout=10)


@pytest.mark.skipif(NODE is None, reason="node is needed to run the relay")
@pytest.mark.parametrize("bad", ["abc", "0", "65536", "80.5"])
def test_relay_refuses_a_bad_port_loudly(bad):
    result = _start_relay(str(RELAY_SCRIPT), bad)
    assert result.returncode != 0
    assert f"invalid port {bad!r}".replace("'", '"') in result.stderr
    assert result.stdout == ""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _mcp_manager(tmp_path, plan, rules=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake = tmp_path / "fake_mcp_claude.py"
    fake.write_text(FAKE_MCP_CLAUDE, encoding="utf-8")
    state = tmp_path / "mcp-state.jsonl"
    port = _free_port()
    cfg = make_cfg(tmp_path, rules=rules)
    cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5")
    manager = Manager(cfg)
    web = FakeWeb()
    manager.runner.web = web
    search = manager.modules.get("search")
    search.service = search.service or SessionSearch(manager.db)
    made = []

    class RelayedClaude(ClaudeSession):  # the docker path starts the relay; the test command must do it itself
        async def start(self):
            await self.mcp.start()
            await super().start()

        async def stop(self):
            await super().stop()
            await self.mcp.stop()

    def factory(**kwargs):
        made.append(kwargs)
        return RelayedClaude(**kwargs, command=[sys.executable, "-u", str(fake), str(port), str(state),
                                                json.dumps(plan)])

    manager.runner.cli_factory = factory
    manager.runner.mcp_relay_factory = lambda **kw: McpRelay(
        **kw, command=[NODE, *relay_node_args(port)])
    return manager, made, state, web


def _log(state):
    return [json.loads(line) for line in state.read_text(encoding="utf-8").splitlines()]


@pytest.mark.skipif(NODE is None, reason="node is needed to run the relay")
def test_hosted_claude_calls_harness_tools_through_policy_and_relay(tmp_path):
    async def body():
        plan = [{"tool": "web_search", "input": {"query": "mcp"}},
                {"tool": "session_search", "input": {"query": "earlier"}},
                {"tool": "web_search", "input": {"query": "sneaky"}, "skip_ask": True}]
        m, made, state, web = _mcp_manager(tmp_path, plan)
        await m.start()
        sid = m.create("search", backend="claude")["id"]
        s = await wait_status(m, sid, "done")
        await asyncio.gather(*m.tasks.values())
        log = _log(state)
        assert ["initialize", "harness"] in log
        tools = next(entry[1] for entry in log if entry[0] == "tools")
        assert {"web_search", "web_fetch", "session_search", "session_read"} <= set(tools)
        assert not any(t.startswith(("memory_", "open_claude", "propose_skill")) for t in tools)
        assert ["bad_token", 401] in log
        calls = [entry[1:] for entry in log if entry[0] == "call"]
        assert calls[0] == ["web_search", False, "results for mcp"]
        assert calls[1][:2] == ["session_search", False]
        # A call that skipped can_use_tool never runs: the endpoint wants a grant from the policy path.
        assert calls[2][:2] == ["web_search", True] and "not allowed through the harness" in calls[2][2]
        assert web.calls == [("web_search", {"query": "mcp"})]
        decided = events(m, sid, "tool_call")
        assert [(e["name"], e["decision"]) for e in decided] == [
            ("mcp__harness__web_search", "allow"), ("mcp__harness__session_search", "allow")]
        results = events(m, sid, "tool_result")
        assert [r["name"] for r in results][:2] == ["mcp__harness__web_search", "mcp__harness__session_search"]
        assert results[0]["output"] == "results for mcp"
        assert [t["kind"] for t in s["taint"]] == ["web_search"]  # MCP results taint like native web tools
        assert made[0]["mcp_token"] and m.runner.mcp_tokens.session_for(made[0]["mcp_token"]) is None
        assert sid not in m.runner._mcp_grants
        for record in m.db.events(sid):
            assert made[0]["mcp_token"] not in json.dumps(record)
        await m.stop()
    asyncio.run(body())


@pytest.mark.skipif(NODE is None, reason="node is needed to run the relay")
def test_an_ask_rule_prompts_for_an_mcp_call_and_denial_blocks_it(tmp_path):
    async def one(root, approve):
        plan = [{"tool": "web_search", "input": {"query": "guarded"}}]
        rules = [{"tool": "web_search", "action": "ask", "reason": "check searches"}]
        m, _, state, web = _mcp_manager(root, plan, rules=rules)
        await m.start()
        sid = m.create("search", backend="claude", project="guarded")["id"]
        await wait_status(m, sid, "waiting_approval")
        pending = m.db.pending_approvals(sid)
        assert [(p["tool"], p["reason"]) for p in pending] == [("mcp__harness__web_search", "check searches")]
        m.decide(sid, pending[0]["id"], approve=approve)
        await wait_status(m, sid, "done")
        await asyncio.gather(*m.tasks.values())
        call = next(entry[1:] for entry in _log(state) if entry[0] == "call")
        await m.stop()
        return call, web.calls

    call, ran = asyncio.run(one(tmp_path / "approved", True))
    assert call == ["web_search", False, "results for guarded"] and ran == [("web_search", {"query": "guarded"})]
    call, ran = asyncio.run(one(tmp_path / "denied", False))
    assert call[1] is True and ran == []


def test_disabled_backends_and_cursor_get_no_relay_or_token(tmp_path):
    async def body():
        cfg = make_cfg(tmp_path)
        cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5", mcp=False)
        cfg.backends["codex"] = BackendConfig(enabled=True, model="gpt-test", network="harness-cli-codex", mcp=False)
        cfg.backends["cursor"] = BackendConfig(enabled=True, model="auto", network="harness-cli-cursor")
        m = Manager(cfg)
        m.runner.web = FakeWeb()
        made = []

        def factory(**kwargs):
            made.append(kwargs)
            raise CliBackendError("stop here")
        m.runner.cli_factory = m.runner.codex_factory = m.runner.cursor_factory = factory
        await m.start()
        sids = [m.create("x", backend=name)["id"] for name in ("claude", "codex", "cursor")]
        for sid in sids:
            await wait_status(m, sid, "failed")
        await asyncio.gather(*m.tasks.values())
        assert len(made) == 3 and not any("mcp" in kw or "mcp_token" in kw for kw in made)
        claude, codex, cursor = sids
        # The tools exist for Claude Code and Codex; the backend switch keeps them off the CLI. Cursor has none.
        assert m.runner.mcp_tool_schemas(claude) and m.runner.mcp_tool_schemas(codex)
        assert m.runner.mcp_tool_schemas(cursor) == []
        await m.stop()
    asyncio.run(body())


# end to end: fake Codex app-server -> real node relay -> daemon (#373)

FAKE_MCP_CODEX = r'''import json
import os
import pathlib
import sys
import urllib.request

port, state, plan = int(sys.argv[1]), pathlib.Path(sys.argv[2]), json.loads(sys.argv[3])
token = os.environ.get("HARNESS_MCP_TOKEN", "")

def log(*parts):
    with state.open("a", encoding="utf-8") as f:
        f.write(json.dumps(parts) + "\n")

def read():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    return json.loads(line)

def send(item):
    print(json.dumps(item), flush=True)

def rpc(method, params=None, rid=1, bearer=None):
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token if bearer is None else bearer}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None

def call(tool, args, call_id, rid):
    body = rpc("tools/call", {"name": tool, "arguments": args, "_meta": {"callId": call_id}}, rid=rid)[1]
    result = body["result"]
    return result["content"][0]["text"], result["isError"]

for method, reply in (("initialize", {}), ("thread/start", {"thread": {"id": "thr-1"}}),
                      ("turn/start", {"turn": {"id": "turn-1"}})):
    request = read()
    assert request["method"] == method, request
    log(method, request["params"])
    send({"id": request["id"], "result": reply})
    if method == "initialize":
        assert read().get("method") == "initialized"
log("tools", sorted(t["name"] for t in rpc("tools/list")[1]["result"]["tools"]))
log("bad_token", rpc("tools/list", bearer="wrong")[0])
for n, step in enumerate(plan):
    cid = f"call-{n}"
    if step.get("builtin"):
        send({"method": "item/started", "params": {"item": {"type": step["builtin"], "id": cid, "command": "id"}}})
        continue
    server, args = step.get("server", "harness"), step["input"]
    send({"method": "item/started", "params": {"item": {"type": "mcpToolCall", "id": cid, "server": server,
                                                        "tool": step["tool"], "arguments": args,
                                                        "status": "inProgress"}}})
    accepted = True
    if not step.get("skip_ask"):
        send({"id": 100 + n, "method": "mcpServer/elicitation/request", "params": {
            "threadId": "thr-1", "turnId": "turn-1", "serverName": server, "mode": "form",
            "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": args},
            "message": f"Allow the {server} MCP server to run tool \"{step['tool']}\"?",
            "requestedSchema": {"type": "object", "properties": {}}}})
        reply = read()
        assert reply["id"] == 100 + n, reply
        log("answer", step["tool"], reply["result"])
        accepted = reply["result"]["action"] == "accept"
    if accepted:
        text, error = call(step["tool"], args, cid, 10 + n)
        if step.get("replay"):  # the same call id again, with no fresh approval
            log("replay", *call(step["tool"], args, cid, 50 + n)[::-1])
    else:
        text, error = "user rejected MCP tool call", True
    log("call", step["tool"], error, text)
    item = {"type": "mcpToolCall", "id": cid, "server": server, "tool": step["tool"], "arguments": args,
            "status": "failed" if error else "completed", "durationMs": 5,
            "result": None if error else {"content": [{"type": "text", "text": text}]},
            "error": {"message": text} if error else None}
    send({"method": "item/completed", "params": {"item": item}})
send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "msg-1", "text": "done"}}})
send({"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}})
while True:
    read()
'''


def _codex_manager(tmp_path, plan, rules=None, **backend):
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake = tmp_path / "fake_mcp_codex.py"
    fake.write_text(FAKE_MCP_CODEX, encoding="utf-8")
    state = tmp_path / "codex-state.jsonl"
    port = _free_port()
    cfg = make_cfg(tmp_path, rules=rules)
    cfg.backends["codex"] = BackendConfig(enabled=True, model="gpt-test", network="harness-cli-codex", **backend)
    manager = Manager(cfg)
    web = FakeWeb()
    manager.runner.web = web
    search = manager.modules.get("search")
    search.service = search.service or SessionSearch(manager.db)
    made = []

    class RelayedCodex(CodexSession):  # the docker path starts the relay; the test command must do it itself
        async def start(self):
            await self.mcp.start()
            await super().start()

        async def stop(self):
            await super().stop()
            await self.mcp.stop()

    def factory(**kwargs):
        made.append(kwargs)
        return RelayedCodex(**kwargs, command=[sys.executable, "-u", str(fake), str(port), str(state),
                                               json.dumps(plan)])

    manager.runner.codex_factory = factory
    manager.runner.mcp_relay_factory = lambda **kw: McpRelay(**kw, command=[NODE, *relay_node_args(port)])
    return manager, made, state, web


@pytest.mark.skipif(NODE is None, reason="node is needed to run the relay")
def test_hosted_codex_calls_harness_tools_through_policy_and_relay(tmp_path):
    async def body():
        plan = [{"tool": "web_search", "input": {"query": "mcp"}, "replay": True},
                {"tool": "session_search", "input": {"query": "earlier"}},
                {"tool": "web_search", "input": {"query": "sneaky"}, "skip_ask": True},
                {"tool": "create_issue", "input": {"title": "x"}, "server": "github"}]
        m, made, state, web = _codex_manager(tmp_path, plan)
        await m.start()
        sid = m.create("search", backend="codex")["id"]
        s = await wait_status(m, sid, "done")
        await asyncio.gather(*m.tasks.values())
        log = _log(state)
        assert made[0]["mcp"].container == f"harness-{sid}-mcp" and made[0]["mcp_token"]
        tools = next(entry[1] for entry in log if entry[0] == "tools")
        assert {"web_search", "web_fetch", "session_search", "session_read"} <= set(tools)
        assert ["bad_token", 401] in log
        answers = [entry[1:] for entry in log if entry[0] == "answer"]
        assert answers == [["web_search", {"action": "accept", "content": None, "_meta": None}],
                           ["session_search", {"action": "accept", "content": None, "_meta": None}],
                           ["create_issue", {"action": "decline", "content": None, "_meta": None}]]
        calls = [entry[1:] for entry in log if entry[0] == "call"]
        assert calls[0] == ["web_search", False, "results for mcp"]
        assert calls[1][:2] == ["session_search", False]
        # Allowed once is run once: the same call id again finds no grant.
        replay = next(entry[1:] for entry in log if entry[0] == "replay")
        assert replay[0] is True and "not allowed through the harness" in replay[1]
        # A call Codex didn't ask about never runs: the endpoint wants a grant from the policy path.
        assert calls[2][:2] == ["web_search", True] and "not allowed through the harness" in calls[2][2]
        assert calls[3][:2] == ["create_issue", True]
        assert web.calls == [("web_search", {"query": "mcp"})]
        decided = [(e["name"], e["decision"]) for e in events(m, sid, "tool_call")]
        assert decided == [("mcp__harness__web_search", "allow"), ("mcp__harness__session_search", "allow"),
                           ("mcp__github__create_issue", "deny")]
        results = events(m, sid, "tool_result")
        assert [(r["name"], r["ok"]) for r in results] == [
            ("mcp__harness__web_search", True), ("mcp__harness__session_search", True),
            ("mcp__harness__web_search", False), ("mcp__github__create_issue", False)]
        assert results[0]["output"] == "results for mcp"
        assert [t["kind"] for t in s["taint"]] == ["web_search"]  # MCP results taint like native web tools
        assert m.runner.mcp_tokens.session_for(made[0]["mcp_token"]) is None
        assert sid not in m.runner._mcp_grants
        for record in m.db.events(sid):
            assert made[0]["mcp_token"] not in json.dumps(record)
        await m.stop()
    asyncio.run(body())


@pytest.mark.skipif(NODE is None, reason="node is needed to run the relay")
@pytest.mark.parametrize("rule,approve,ran", [("ask", True, True), ("ask", False, False), ("deny", None, False)])
def test_codex_mcp_calls_follow_ask_and_deny_rules(tmp_path, rule, approve, ran):
    async def body():
        plan = [{"tool": "web_search", "input": {"query": "guarded"}}]
        rules = [{"tool": "web_search", "action": rule, "reason": "check searches"}]
        m, _, state, web = _codex_manager(tmp_path, plan, rules=rules)
        await m.start()
        sid = m.create("search", backend="codex", project="guarded")["id"]
        if rule == "ask":
            await wait_status(m, sid, "waiting_approval")
            pending = m.db.pending_approvals(sid)
            assert [(p["tool"], p["reason"]) for p in pending] == [("mcp__harness__web_search", "check searches")]
            m.decide(sid, pending[0]["id"], approve=approve)
        await wait_status(m, sid, "done")
        await asyncio.gather(*m.tasks.values())
        log = _log(state)
        answer = next(entry[2]["action"] for entry in log if entry[0] == "answer")
        call = next(entry[1:] for entry in log if entry[0] == "call")
        await m.stop()
        assert answer == ("accept" if ran else "decline")
        assert call[1] is (not ran)
        assert web.calls == ([("web_search", {"query": "guarded"})] if ran else [])
    asyncio.run(body())


class FakeMemory:
    tool_names = {"memory_read", "memory_write"}
    wants_session = True

    def __init__(self):
        self.calls = []

    def schemas(self):
        return [{"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object"}}}
                for n in sorted(self.tool_names)]

    async def preview(self, name, args):
        return "Add a note\n\n--- a/notes.md\n+++ b/notes.md", ""

    async def call(self, name, args, session=None, call_id=""):
        self.calls.append((name, session["id"], call_id))
        return "written"


class FakeCli:
    def __init__(self):
        self.answers = []

    async def respond_permission(self, request_id, behavior, args, message=""):
        self.answers.append((request_id, behavior, message))


def test_memory_writes_over_mcp_ask_with_the_diff_and_run_once_under_the_approved_call(tmp_path):
    async def body():
        cfg = make_cfg(tmp_path)
        cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5")
        m = Manager(cfg)
        m.runner.cli_factory = lambda **kw: (_ for _ in ()).throw(CliBackendError("stop here"))
        await m.start()
        sid = m.create("x", backend="claude")["id"]
        await wait_status(m, sid, "failed")
        await asyncio.gather(*m.tasks.values())
        memory = m.modules.get("memory_library").service = FakeMemory()
        s, cli, args = m.db.get_session(sid), FakeCli(), {"path": "notes.md", "content": "hi", "summary": "note"}
        name = "mcp__harness__memory_write"
        approval = await m.runner._ask_cli_policy(s, cli, "req-1", {"tool_name": name, "input": args}, name, args,
                                                  "toolu-1")
        assert approval["status"] == "pending" and approval["tool"] == name
        assert approval["detail"] == "Add a note\n\n--- a/notes.md\n+++ b/notes.md"
        assert cli.answers == [] and await m.runner.mcp_call(sid, "memory_write", args, "toolu-1") == (
            "Error: this call was not allowed through the harness approval policy. Call the tool normally so the "
            "harness can decide it.", False)
        # Approved, and regenerated by Claude after --resume under a new tool_use id.
        await m.runner._allow_cli(sid, cli, "req-2", name, args, "toolu-2", approval["tool_call_id"])
        assert cli.answers == [("req-2", "allow", "")]
        assert await m.runner.mcp_call(sid, "memory_write", args, "toolu-2") == ("written", True)
        assert memory.calls == [("memory_write", sid, "toolu-1")]
        assert (await m.runner.mcp_call(sid, "memory_write", args, "toolu-2"))[1] is False  # one use only
        other = await m.runner._ask_cli_policy(s, cli, "req-3", {}, "mcp__harness__run_shell",
                                               {"command": "id"}, "toolu-3")
        assert other is None and cli.answers[-1][:2] == ("req-3", "deny") and not m.runner._mcp_grants[sid]
        await m.runner._stop_cli(sid)
        assert sid not in m.runner._mcp_grants
        await m.stop()
    asyncio.run(body())
