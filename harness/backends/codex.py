"""Codex (`codex app-server --stdio`, JSON-RPC v2): the adapter (#991).

`CodexSession` is the transport: one long-lived app-server process, its requests and the active turn.
`CodexTranslator` turns its notifications and requests into neutral events.

Notes on the protocol are in docs/phase8a-design.md ("Codex CLI", "Harness tools over MCP"). The messages a session
turns into events:

- `thread/started`: the thread id, kept for `thread/resume`;
- `item/agentMessage/delta`, `item/reasoning/summaryTextDelta`, `item/reasoning/textDelta`: streamed text;
- `item/started` and `item/completed`: a command, file change or MCP tool call and its outcome, and the agent's
  message;
- `item/commandExecution/requestApproval`, `item/fileChange/requestApproval` and `mcpServer/elicitation/request`:
  Codex asks before a tool call. An elicitation names no call: it is matched to the newest unasked MCP call in
  progress with the same server and arguments;
- `thread/tokenUsage/updated` (kept for the result), `account/rateLimits/updated`, `turn/completed`, `error`.

Responses to the harness's own requests carry no method and are the transport's business.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable

from .. import cli_domains
from .. import cli_backends as shared  # run_cmd and ready_domain are looked up there at call time
from ..cli_backends import (NO_PROXY, NODE_USE_ENV_PROXY, WORKSPACE, CliBackendError, session_key, split_note,
                            workspace_args)
from ..config import BackendConfig, SandboxConfig
from ..mcp_server import TOKEN_ENV, McpRelay, McpRelayError, codex_mcp_overrides
from ..policy import MCP_SERVER
from .events import (Assistant, Delta, Error, Event, PermissionRequest, RateLimits, Result, Session, ToolCall,
                     ToolResult, obj, text_of)

# Thread items an App-tools-only Codex session may produce. Anything else means a built-in tool ran: the run stops.
# Split mode (#427) turns off the same tools, except the plan tool, which touches no environment.
CODEX_TOOLS_ONLY_ITEMS = ("userMessage", "hookPrompt", "agentMessage", "plan", "reasoning", "contextCompaction",
                          "mcpToolCall")
# Answers Codex takes for an MCP tool call approval (an MCP elicitation), as opposed to command and file approvals.
ELICITATION = "mcpServer/elicitation/request"
COMMAND_APPROVAL = "item/commandExecution/requestApproval"
FILE_APPROVAL = "item/fileChange/requestApproval"
APPROVALS = (COMMAND_APPROVAL, FILE_APPROVAL, ELICITATION)
DELTAS = {"item/agentMessage/delta": "content", "item/reasoning/summaryTextDelta": "reasoning",
          "item/reasoning/textDelta": "reasoning"}
LIMIT_MARKERS = ("ratelimit", "rate_limit", "usage limit")


def rate_window(slot: str, value: dict) -> tuple[int, str, dict]:
    minutes = int(value.get("windowDurationMins") or 0)
    if minutes == 300:
        name = "five_hour"
    elif minutes == 10080:
        name = "seven_day"
    else:
        name = f"{minutes}_minute" if minutes else slot
    window: dict = {"utilization": float(value["usedPercent"]) / 100}
    if value.get("resetsAt") is not None:
        window["resetsAt"] = value["resetsAt"]
    return minutes, name, window


def rate_limits(snapshot: dict) -> dict:
    """App-server's percent windows in the shared 0..1 shape: the longest window decides `utilization` and
    `resetsAt`, and a reached limit (or spend control) makes the status `rejected`."""
    windows: dict[str, dict] = {}
    ranked: list[tuple[int, str, float, float]] = []
    for slot in ("primary", "secondary"):
        value = snapshot.get(slot)
        if not isinstance(value, dict) or value.get("usedPercent") is None:
            continue
        minutes, name, window = rate_window(slot, value)
        windows[name] = window
        ranked.append((minutes, name, window["utilization"], float(value.get("resetsAt") or 0)))
    longest = max(ranked, default=(0, "", 0.0, 0.0))
    reached = snapshot.get("rateLimitReachedType") or ("spend_control" if snapshot.get("spendControlReached") else "")
    result = {**snapshot, "status": "rejected" if reached else "allowed", "unifiedWindows": windows}
    if longest[1]:
        result.update({"rateLimitType": longest[1], "utilization": longest[2], "resetsAt": longest[3]})
    return result


def changes_of(item: dict) -> list:
    changes = item.get("changes")
    return changes if isinstance(changes, list) else []


def file_args(item: dict) -> dict:
    """A fileChange item as apply_patch arguments: the paths under /workspace and the diffs."""
    paths, patches = [], []
    for change in changes_of(item):
        if not isinstance(change, dict):
            continue
        raw = str(change.get("path") or "").replace("\\", "/")
        path = raw if raw.startswith("/") else "/workspace/" + raw.lstrip("/")
        if raw:
            paths.append(path)
        if change.get("diff"):
            patches.append(str(change["diff"]))
    return {"file_paths": paths, "patch": "\n".join(patches)}


def mcp_tool(item: dict) -> tuple[str, dict]:
    """An mcpToolCall item as the name the policy decides (mcp__<server>__<tool>, as Claude Code names it) and its
    arguments."""
    return f"mcp__{item.get('server') or ''}__{item.get('tool') or ''}", obj(item, "arguments")


class CodexTranslator:
    """One app-server process's messages as neutral events. It keeps the thread's items by id (an approval request
    names only the item), which MCP calls were already asked about, the agent's last message and the latest token
    usage. `mode` is "tools_only" or "split" for a session that starts with no environment: any built-in tool item
    then stops the run."""

    def __init__(self, mode: str = ""):
        self.mode = mode
        self.items: dict[str, dict] = {}
        self.asked: set[object] = set()
        self.last_answer = ""
        self.token_usage: dict = {}

    def translate(self, record: object) -> list[Event]:
        if not isinstance(record, dict):
            return []
        method = str(record.get("method") or "")
        params = obj(record, "params")
        if method == "thread/started":
            thread_id = str(obj(params, "thread").get("id") or "")
            return [Session(thread_id)] if thread_id else []
        if method in DELTAS:
            text = params.get("delta")
            return [Delta(DELTAS[method], text)] if isinstance(text, str) and text else []
        if method == "item/started":
            return self._started(obj(params, "item"))
        if method == "item/completed":
            return self._completed(obj(params, "item"))
        if method == "thread/tokenUsage/updated":
            if isinstance(params.get("tokenUsage"), dict):
                self.token_usage = params["tokenUsage"]
            return []
        if method == "account/rateLimits/updated":
            limits = rate_limits(obj(params, "rateLimits"))
            return [RateLimits(limits, limit_reached=limits["status"] == "rejected",
                               resets_at=float(limits.get("resetsAt") or 0))]
        if method in (COMMAND_APPROVAL, FILE_APPROVAL):
            return [self._approval(record, method, params)]
        if method == ELICITATION:
            return [self._elicitation(record, params)]
        if method == "turn/completed":
            return [self._result(obj(params, "turn"))]
        if method == "error":
            return [Error(f"Codex app-server error: {params.get('error') or params}")]
        return []

    def _keep(self, item: dict) -> str:
        item_id = str(item.get("id") or "")
        if item_id:
            self.items[item_id] = item
        return item_id

    def _guard(self, item: dict) -> list[Event]:
        """A session with no environment has every built-in tool off. Should one run anyway (a Codex change the
        pinned-version notes missed), the run stops rather than go on."""
        if not self.mode:
            return []
        kind = str(item.get("type") or "")
        if kind not in CODEX_TOOLS_ONLY_ITEMS or (kind == "mcpToolCall" and item.get("server") != MCP_SERVER):
            mode = "an App-tools-only" if self.mode == "tools_only" else "a split-mode"
            return [Error(f"Codex used a built-in tool ({kind}) in {mode} session; stopped")]
        return []

    def _started(self, item: dict) -> list[Event]:
        item_id, kind = self._keep(item), str(item.get("type") or "")
        stop = self._guard(item)
        if stop:
            return stop
        if kind == "commandExecution":
            name, args = "exec_command", {"command": str(item.get("command") or ""), "cwd": str(item.get("cwd") or "")}
        elif kind == "fileChange":
            name, args = "apply_patch", file_args(item)
        elif kind == "mcpToolCall":
            name, args = mcp_tool(item)
        else:
            return []
        return [Assistant(tool_calls=(ToolCall(item_id, name, args),))]

    def _completed(self, item: dict) -> list[Event]:
        item_id, kind = self._keep(item), str(item.get("type") or "")
        if kind == "agentMessage":
            self.last_answer = str(item.get("text") or "")
            return [Assistant(self.last_answer)]
        stop = self._guard(item)
        if stop:
            return stop
        ok, seconds = str(item.get("status") or "") == "completed", float(item.get("durationMs") or 0) / 1000
        if kind == "mcpToolCall":
            name, _ = mcp_tool(item)
            output = text_of(obj(item, "result").get("content")) if ok else str(obj(item, "error").get("message") or "")
            return [ToolResult(item_id, name, ok, output, seconds)]
        if kind == "commandExecution":
            return [ToolResult(item_id, "exec_command", ok, str(item.get("aggregatedOutput") or ""), seconds)]
        if kind == "fileChange":
            output = "\n".join(str(c.get("diff") or "") for c in changes_of(item) if isinstance(c, dict))
            return [ToolResult(item_id, "apply_patch", ok, output, seconds)]
        return []

    def _approval(self, record: dict, method: str, params: dict) -> PermissionRequest:
        item_id = str(params.get("itemId") or record.get("id") or "")
        item = self.items.get(item_id, {})
        if method == COMMAND_APPROVAL:
            name = "exec_command"
            command = params.get("command") if params.get("command") is not None else item.get("command")
            args: dict = {"command": str(command or ""), "cwd": str(params.get("cwd") or item.get("cwd") or "")}
            if params.get("networkApprovalContext"):
                args["network"] = True
        else:
            name, args = "apply_patch", file_args(item)
            if params.get("grantRoot") and not args["file_paths"]:
                args["file_paths"] = [str(params["grantRoot"])]
        return PermissionRequest(record.get("id"), name, args, item_id,
                                 str(params.get("reason") or params.get("command") or ""))

    def _elicitation(self, record: dict, params: dict) -> PermissionRequest:
        """Codex asks before every harness MCP call (default_tools_approval_mode "prompt") with an MCP elicitation
        that follows the call's item/started. It carries no call id, so it belongs to the newest started, unasked
        call to that server with those arguments: Codex asks right after it starts the item, and never twice for one
        call. Any other elicitation is a server's form, which nobody is there to fill in."""
        meta = obj(params, "_meta")
        item = None
        if meta.get("codex_approval_kind") == "mcp_tool_call":
            server, wanted = params.get("serverName"), obj(meta, "tool_params")
            item = next((i for i in reversed(list(self.items.values()))
                         if i.get("type") == "mcpToolCall" and i.get("server") == server
                         and i.get("status") == "inProgress" and i.get("id") not in self.asked
                         and obj(i, "arguments") == wanted), None)
        if item is None:
            return PermissionRequest(record.get("id"), "", {}, decidable=False)
        self.asked.add(item.get("id"))
        name, args = mcp_tool(item)
        return PermissionRequest(record.get("id"), name, args, str(item.get("id") or ""),
                                 str(params.get("message") or ""))

    def _result(self, turn: dict) -> Result:
        status = str(turn.get("status") or "")
        error = turn.get("error")
        error_text = json.dumps(error, ensure_ascii=False) if error else ""
        usage = obj(self.token_usage, "last")
        return Result(
            answer=self.last_answer or error_text, turns=1, is_error=status != "completed",
            subtype="success" if status == "completed" else status or "failed",
            limit_reached=status == "failed" and any(x in error_text.lower() for x in LIMIT_MARKERS),
            usage={"input_tokens": int(usage.get("inputTokens") or 0),
                   "output_tokens": int(usage.get("outputTokens") or 0),
                   "cached_input_tokens": int(usage.get("cachedInputTokens") or 0),
                   "reasoning_output_tokens": int(usage.get("reasoningOutputTokens") or 0)})


# Every Codex 0.154.0 built-in tool an App-tools-only session (#329) turns off, besides the ones that need an
# environment (exec_command, write_stdin, apply_patch, view_image, request_permissions), which `environments: []`
# removes. Source notes in docs/phase8a-design.md "Harness tools over MCP"; recheck them when the Codex pin moves.
CODEX_TOOLS_ONLY_OVERRIDES = (
    'web_search="disabled"',                          # hosted web_search and the standalone web.run
    "tools.update_plan.enabled=false",
    "tools.experimental_request_user_input.enabled=false",
    *(f"features.{name}=false" for name in (
        "shell_tool", "unified_exec", "view_image", "code_mode", "multi_agent", "apps", "plugins",
        "tool_suggest", "image_generation", "goals", "sleep_tool", "memories", "browser_use", "computer_use",
        "skill_mcp_dependency_install")),
)
# Split mode (#427) turns off the same tools, except the plan tool, which touches no environment. Lost in split mode:
# view_image, apply_patch (edit_file replaces it), Codex's own multi-agent and web_search (web_search comes from the
# harness server). The thread items such a session may produce are CODEX_TOOLS_ONLY_ITEMS (harness/backends/codex.py).
CODEX_SPLIT_OVERRIDES = tuple(o for o in CODEX_TOOLS_ONLY_OVERRIDES if o != "tools.update_plan.enabled=false")


class CodexSession:
    """One long-lived ``codex app-server`` JSON-RPC process."""

    def __init__(self, *, session_id: str, workspace: Path, backend: BackendConfig,
                 sandbox: SandboxConfig, system_prompt: str, model: str = "", backend_session_id: str = "",
                 api_key: str = "", popen: Callable = subprocess.Popen, command: list[str] | None = None,
                 mcp: McpRelay | None = None, mcp_token: str = "", tools_only: bool = False, app_id: str = "", end_user: str = "", split: bool = False,
                 tool_profile: str = "default"):
        self.session_id = session_id
        self.tool_profile = tool_profile  # "bash-only" (#638): split mode only, where the MCP server serves run_shell
        self.app_id = app_id
        self.end_user = end_user
        self.workspace = workspace.resolve()
        # With MCP Codex shares the relay's network namespace, as Claude Code does (#373). An App-tools-only session
        # starts with no environment and every other built-in tool off, so only the harness server's tools remain.
        self.mcp = mcp
        self.mcp_token = mcp_token
        self.tools_only = tools_only
        self.split = split and not tools_only  # split mode (#427), as for Claude Code
        self.backend = backend
        self.sandbox = sandbox
        self.system_prompt = system_prompt
        self.model = model or backend.model
        self.backend_session_id = backend_session_id
        self.api_key = session_key(end_user, api_key)
        self.container = f"harness-{session_id}-codex"
        self._popen = popen
        self._command_override = command
        self.process: subprocess.Popen | None = None
        self._events: asyncio.Queue[str | None] = asyncio.Queue()
        self._stderr: list[str] = []
        self._write_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._next_request_id = 1
        self._pending: list[dict] = []
        self._approval_methods: dict[str, str] = {}
        self.active_turn_id = ""
        self._turn_requests: set[int] = set()
        self.translator = CodexTranslator("tools_only" if self.tools_only else "split" if self.split else "")

    @property
    def no_environment(self) -> bool:
        """Whether Codex starts with no environment, and so no shell, apply_patch or view_image."""
        return self.tools_only or self.split

    def command(self) -> list[str]:
        if self._command_override is not None:
            return list(self._command_override)
        args = [
            "docker", "run", "--rm", "-i", "--name", self.container,
            "--label", f"agent-harness.session={self.session_id}",
            "--network", f"container:{self.mcp.container}" if self.mcp else self.backend.network,
            "-e", f"HTTPS_PROXY={self.backend.proxy}",
            "-e", f"HTTP_PROXY={self.backend.proxy}",
            "-e", NO_PROXY,
            "-e", NODE_USE_ENV_PROXY,
            *cli_domains.docker_args("codex", self.backend, self.app_id, end_user=self.end_user),
            *workspace_args(self.workspace, self.split),
            "--memory", self.sandbox.memory,
            "--cpus", str(self.sandbox.cpus),
            "--pids-limit", str(self.sandbox.pids),
            # Docker's default seccomp profile blocks the unprivileged user
            # namespace that Linux Codex uses for bubblewrap. The outer
            # container still has no-new-privileges, dropped capabilities,
            # a workspace-only bind mount, and a provider-only network.
            "--security-opt", "seccomp=unconfined",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "NET_RAW", "--cap-drop", "MKNOD", "--cap-drop", "AUDIT_WRITE",
            self.backend.image, "codex", "app-server", "--stdio",
        ]
        if self.mcp:
            args += codex_mcp_overrides()
        if self.tools_only or self.split:
            for override in (CODEX_TOOLS_ONLY_OVERRIDES if self.tools_only else CODEX_SPLIT_OVERRIDES):
                args += ["-c", override]
        env_names = (["OPENAI_API_KEY"] if self.api_key else []) + ([TOKEN_ENV] if self.mcp else [])
        for name in env_names:  # by name only: the value comes from the docker client's environment
            at = args.index(self.backend.image)
            args[at:at] = ["-e", name]
        return args

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._command_override is None:
            await shared.run_cmd(["docker", "rm", "-f", self.container], timeout=30)
            await shared.ready_domain("codex", self.backend, self.app_id, self.api_key, self.end_user)
            if self.mcp is not None:
                try:
                    await self.mcp.start()
                except McpRelayError as e:
                    raise CliBackendError(str(e)) from e
        child_env = os.environ.copy()
        if self.api_key:
            child_env["OPENAI_API_KEY"] = self.api_key
        if self.mcp is not None:
            child_env[TOKEN_ENV] = self.mcp_token
        process = self._popen(
            self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=child_env,
        )
        self.process = process

        def stdout_reader() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    loop.call_soon_threadsafe(self._events.put_nowait, line)
            finally:
                loop.call_soon_threadsafe(self._events.put_nowait, None)

        def stderr_reader() -> None:
            assert process.stderr is not None
            self._stderr.extend(process.stderr)

        self._threads = [threading.Thread(target=stdout_reader, daemon=True),
                         threading.Thread(target=stderr_reader, daemon=True)]
        for thread in self._threads:
            thread.start()

    def _id(self) -> int:
        value = self._next_request_id
        self._next_request_id += 1
        return value

    async def _write(self, item: dict) -> None:
        def write() -> None:
            if self.process is None or self.process.stdin is None or self.process.poll() is not None:
                raise CliBackendError("Codex app-server is not running")
            with self._write_lock:
                self.process.stdin.write(json.dumps(item, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
        try:
            await asyncio.to_thread(write)
        except OSError as e:
            raise CliBackendError(f"could not write to Codex app-server: {e}") from e

    async def _receive_raw(self, timeout: float | None = None) -> dict | None:
        try:
            line = await asyncio.wait_for(self._events.get(), timeout) if timeout else await self._events.get()
        except asyncio.TimeoutError:
            return None
        if line is None:
            code = self.process.poll() if self.process is not None else None
            error = "".join(self._stderr).strip()
            raise CliBackendError(f"Codex app-server exited with code {code}: {error}".rstrip())
        try:
            value = json.loads(line)
        except json.JSONDecodeError as e:
            raise CliBackendError(f"Codex app-server wrote invalid JSONL: {line[:300].rstrip()}") from e
        if not isinstance(value, dict):
            raise CliBackendError("Codex app-server JSONL event was not an object")
        return value

    async def _request(self, method: str, params: dict) -> dict:
        request_id = self._id()
        await self._write({"id": request_id, "method": method, "params": params})
        while True:
            event = await self._receive_raw(timeout=30)
            if event is None:
                raise CliBackendError(f"Codex app-server timed out answering {method}")
            if event.get("id") == request_id:
                if event.get("error"):
                    raise CliBackendError(f"Codex app-server {method} failed: {event['error']}")
                result = event.get("result")
                if not isinstance(result, dict):
                    raise CliBackendError(f"Codex app-server returned an invalid {method} response: {event!r}"[:500])
                return result
            self._pending.append(event)

    async def initialize(self, prompt: str) -> None:
        init: dict[str, object] = {"clientInfo": {"name": "agent-harness", "version": "0.1.0"}}
        if self.no_environment:  # `environments` is an experimental field in 0.154.0; nothing else here opts in
            init["capabilities"] = {"experimentalApi": True}
        await self._request("initialize", init)
        await self._write({"method": "initialized"})
        common = {
            "cwd": WORKSPACE, "model": self.model, "approvalPolicy": self._approval_policy(),
            "approvalsReviewer": "user", "sandbox": "read-only" if self.no_environment else "workspace-write",
        }
        if self.backend_session_id:
            result = await self._request("thread/resume", {**common, "threadId": self.backend_session_id})
        elif self.split:
            result = await self._request("thread/start", {
                **common, "developerInstructions": self.system_prompt + split_note(self.tool_profile),
                "environments": []})
        elif self.tools_only:
            # The App's prompt replaces Codex's coding-agent instructions, as --system-prompt does for Claude Code.
            result = await self._request("thread/start", {**common, "baseInstructions": self.system_prompt,
                                                          "environments": []})
        else:
            result = await self._request("thread/start", {
                **common, "developerInstructions": self.system_prompt,
            })
        thread_id = str(obj(result, "thread").get("id") or self.backend_session_id)
        if not thread_id:
            raise CliBackendError("Codex app-server did not return a thread id")
        self.backend_session_id = thread_id
        result = await self._request("turn/start", self._turn_start_params(prompt))
        self.active_turn_id = str(obj(result, "turn").get("id") or "")

    def _approval_policy(self) -> str:
        # Codex itself refuses every MCP call under "never", so an App-tools-only session always asks (the harness
        # answers from AppToolsPolicy; nobody is prompted).
        if self.no_environment:
            return "on-request"
        return self.backend.permission_mode if self.backend.permission_mode in ("on-request", "never") else "on-request"

    def _turn_start_params(self, content: str) -> dict:
        params = {"threadId": self.backend_session_id, "input": [{"type": "text", "text": content}],
                  "cwd": WORKSPACE, "model": self.model, "effort": self.backend.effort,
                  "approvalPolicy": self._approval_policy(), "approvalsReviewer": "user"}
        if self.no_environment:
            # No environment for this turn and the ones after it: no shell, apply_patch, view_image or
            # request_permissions. Every turn/start says so again, so a resumed thread can't get one back.
            params["environments"] = []
        return params

    def user_message(self, content: str) -> dict:
        request_id = self._id()
        input_ = [{"type": "text", "text": content}]
        if self.active_turn_id:
            return {"id": request_id, "method": "turn/steer", "params": {
                "threadId": self.backend_session_id, "expectedTurnId": self.active_turn_id, "input": input_}}
        self._turn_requests.add(request_id)
        return {"id": request_id, "method": "turn/start", "params": self._turn_start_params(content)}

    async def send(self, item: dict) -> None:
        await self._write(item)

    async def receive(self, timeout: float | None = None) -> dict | None:
        event = self._pending.pop(0) if self._pending else await self._receive_raw(timeout)
        if event and event.get("error") and event.get("id") is not None:
            raise CliBackendError(f"Codex app-server request failed: {event['error']}")
        if not event:
            return event
        method = event.get("method")
        if event.get("id") in self._turn_requests:
            self._turn_requests.discard(event["id"])
            self.active_turn_id = str(obj(obj(event, "result"), "turn").get("id") or self.active_turn_id)
        if method == "turn/started":  # the turn a message sent now steers
            self.active_turn_id = str(obj(obj(event, "params"), "turn").get("id") or self.active_turn_id)
        elif method == "turn/completed":  # a message sent now starts a new turn
            self.active_turn_id = ""
        elif method in APPROVALS:
            self._approval_methods[str(event.get("id"))] = str(method)
        return event

    async def respond_permission(self, request_id, behavior: str, _args: dict, _message: str = "") -> None:
        # App-server's stable approval response is deliberately narrower than
        # Claude's: notes remain in the harness transcript, while Codex gets the
        # accept/decline decision.
        accept = behavior == "allow"
        if self._approval_methods.pop(str(request_id), "") == ELICITATION:
            # An MCP tool call approval. A plain accept approves this one call; no "persist" in _meta, so Codex
            # never remembers it for the session and asks again next time.
            result = {"action": "accept" if accept else "decline", "content": None, "_meta": None}
        else:
            result = {"decision": "accept" if accept else "decline"}
        await self._write({"id": request_id, "result": result})

    async def stop(self) -> None:
        proc = self.process
        self.process = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 2)
            except subprocess.TimeoutExpired:
                proc.kill()
                await asyncio.to_thread(proc.wait)
        for thread in self._threads:
            await asyncio.to_thread(thread.join, 0.5)
        if self._command_override is None:
            await shared.run_cmd(["docker", "rm", "-f", self.container], timeout=30)
            if self.mcp is not None:
                await self.mcp.stop()
