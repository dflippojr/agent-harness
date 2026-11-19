"""Claude Code (`claude -p --input-format stream-json --output-format stream-json`): the adapter (#991).

`ClaudeSession` is the transport: one long-lived process in its container, stdin and stdout. `ClaudeTranslator`
turns its records into neutral events.

The verified protocol is in docs/phase8a-design.md ("Claude Code"). The records a session turns into events:

- `system`/`init`: the session id, kept for `--resume`;
- `stream_event`: a partial message; a `delta` with `text` streams it (`thinking_delta` and `reasoning_delta` as
  reasoning);
- `assistant`: a message's text and its `tool_use` blocks;
- `user`: the `tool_result` blocks, which name only the call id;
- `control_request` with subtype `can_use_tool`: the CLI asks the host (`--permission-prompt-tool stdio`);
- `rate_limit_event`: `rate_limit_info`, already in the shared shape (`status`, `utilization`, `resetsAt`, ...);
- `result`: the turn's end, with usage and cost.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from .. import claude_token, cli_domains
from ..cli_backends import (EMPTY_MCP_CONFIG, HARDENING, WORKSPACE, CliBackendError, JsonlProcess, container_limits,
                            provider_run, split_note, workspace_args)
from ..mcp_server import TOKEN_ENV, mcp_config
from ..workspace_guidance import GUIDANCE_LIMIT, cap, read_workspace_file
from .events import (Assistant, Delta, Event, PermissionRequest, RateLimits, Session, ToolCall, ToolResult, obj,
                     result_of, text_of)

REASONING_DELTAS = ("thinking_delta", "reasoning_delta")

# Claude Code built-ins that don't touch the environment, kept in split mode (#427). Everything else (Bash, Read, Edit,
# Write, Glob, Grep, WebFetch, ...) is off: the harness MCP server does that work on the session sandbox.
CLAUDE_SPLIT_TOOLS = "Task,TodoWrite"
# Claude Code builtin mode under the bash-only tool profile (#638): Bash and nothing else.
CLAUDE_BASH_ONLY_TOOLS = "Bash"

# Project instructions Claude Code would load from the workspace itself. `--setting-sources user` drops them along with
# the project settings (#531), so the session's system prompt carries them instead.
PROJECT_MEMORY_FILES = ("CLAUDE.md", ".claude/CLAUDE.md")
PROJECT_MEMORY_LIMIT = GUIDANCE_LIMIT  # characters: the prompt travels on the docker command line


def project_memory(workspace: Path) -> str:
    """The workspace's CLAUDE.md files as a system prompt section, or "". A file reached through a link is skipped:
    it is read on the host, where a link could point anywhere."""
    sections = [f"Contents of {WORKSPACE}/{name} (project instructions):\n\n{text}"
                for name in PROJECT_MEMORY_FILES if (text := read_workspace_file(workspace, name))]
    body = cap("\n\n".join(sections), PROJECT_MEMORY_LIMIT, f"read the files in {WORKSPACE} for the rest")
    return "\n\n" + body if body else ""


class ClaudeTranslator:
    """One Claude Code process's stream-json records as neutral events. A tool result names only its call id, so the
    tool names that assistant messages and permission requests announced are kept by id."""

    def __init__(self) -> None:
        self.tool_names: dict[str, str] = {}

    def translate(self, record: object) -> list[Event]:
        if not isinstance(record, dict):
            return []
        type_ = record.get("type")
        if type_ == "system" and record.get("subtype") == "init":
            session_id = str(record.get("session_id") or "")
            return [Session(session_id)] if session_id else []
        if type_ == "stream_event":
            return self._delta(record)
        if type_ == "assistant":
            return [self._assistant(record)]
        if type_ == "user":
            return self._tool_results(record)
        if type_ == "control_request":
            return self._permission(record)
        if type_ == "rate_limit_event":
            info = obj(record, "rate_limit_info")
            return [RateLimits(info, limit_reached=info.get("status") == "rejected",
                               resets_at=float(info.get("resetsAt") or 0), billing=True)]
        if type_ == "result":
            result = result_of(record)
            return [replace(result, limit_reached=result.is_error and "rate limit" in result.answer.lower())]
        return []

    @staticmethod
    def _delta(record: dict) -> list[Event]:
        delta = obj(obj(record, "event"), "delta")
        text = delta.get("text")
        if not isinstance(text, str) or not text:
            return []
        return [Delta("reasoning" if delta.get("type") in REASONING_DELTAS else "content", text)]

    def _assistant(self, record: dict) -> Assistant:
        content = obj(record, "message").get("content") or []
        calls = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            call_id, name = str(block.get("id") or ""), str(block.get("name") or "")
            self.tool_names[call_id] = name
            calls.append(ToolCall(call_id, name, obj(block, "input")))
        return Assistant(text_of(content), tuple(calls))

    def _tool_results(self, record: dict) -> list[Event]:
        content = obj(record, "message").get("content") or []
        results: list[Event] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call_id = str(block.get("tool_use_id") or "")
            results.append(ToolResult(call_id, self.tool_names.get(call_id, ""), not bool(block.get("is_error")),
                                      text_of(block.get("content"))))
        return results

    def _permission(self, record: dict) -> list[Event]:
        request = obj(record, "request")
        if request.get("subtype") != "can_use_tool":
            return []
        request_id = str(record.get("request_id") or "")
        tool = str(request.get("tool_name") or "")
        call_id = str(request.get("tool_use_id") or "")
        self.tool_names[call_id or request_id] = tool
        return [PermissionRequest(request_id, tool, obj(request, "input"), call_id,
                                  str(request.get("description") or ""))]


class ClaudeSession(JsonlProcess):
    """One long-lived ``claude -p`` stream-json process."""

    name, label = "claude", "Claude CLI"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.translator = ClaudeTranslator()

    def command(self) -> list[str]:
        if self._command_override is not None:
            return list(self._command_override)
        network = f"container:{self.mcp.container}" if self.mcp else self.backend.network
        args = [
            *provider_run(self.session_id, self.container, network, self.backend.proxy),
            *cli_domains.docker_args("claude", self.backend, self.app_id, token=self._uses_token(), end_user=self.end_user),
            *workspace_args(self.workspace, self.split),
            *container_limits(self.sandbox),
            *HARDENING,
            self.backend.image,
            "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--include-partial-messages", "--permission-prompt-tool", "stdio",
            "--permission-mode", "default" if self.tools_only else self.backend.permission_mode, "--model", self.model,
            # User settings only (#531): the read-only state files and the managed settings. Project and local
            # settings in the workspace's .claude/ could set env, helpers or permissions the session never asked for.
            "--setting-sources", "user",
            "--system-prompt" if self.tools_only else "--append-system-prompt",
            self.system_prompt + (split_note(self.tool_profile) if self.split else project_memory(self.workspace)),
        ]
        if self.tools_only:
            # "" turns off every built-in tool (Bash, Read, Edit, WebFetch, Task...); MCP tools stay. Skills and slash
            # commands go too. can_use_tool still sees every call and denies anything but the App's tools.
            args += ["--tools", "", "--disable-slash-commands"]
        elif self.split:
            args += ["--tools", CLAUDE_SPLIT_TOOLS]  # under bash-only the harness MCP server narrows instead
        elif self.tool_profile == "bash-only":
            args += ["--tools", CLAUDE_BASH_ONLY_TOOLS]
        if self.backend_session_id:
            args += ["--resume", self.backend_session_id]
        # Only the harness server, or none: --strict-mcp-config ignores any .mcp.json the workspace brings along and
        # the user-scope servers in the state's .claude.json, which a session could have written (#371).
        args += ["--mcp-config", mcp_config() if self.mcp else EMPTY_MCP_CONFIG, "--strict-mcp-config"]
        env_names = ((["ANTHROPIC_API_KEY"] if self.api_key else []) + ([claude_token.ENV] if self._uses_token() else [])
                     + ([TOKEN_ENV] if self.mcp else []))
        for name in env_names:  # by name only: the value comes from the docker client's environment
            at = args.index(self.backend.image)
            args[at:at] = ["-e", name]
        return args

    def _uses_token(self) -> bool:
        return not self.end_user and claude_token.uses_token(self.backend, self.app_id, self.api_key)

    async def start(self) -> None:
        await self._prepare()
        child_env = os.environ.copy()
        if self.api_key:
            child_env["ANTHROPIC_API_KEY"] = self.api_key
        child_env.pop(claude_token.ENV, None)  # the daemon's own environment never leaks a token into a session
        if self._uses_token():
            token = claude_token.read_token(self.backend.oauth_token_file)
            if not token:
                raise CliBackendError("the Claude subscription token file is missing or empty; run "
                                      "ops/backends/login.ps1 claude -Token")
            child_env[claude_token.ENV] = token
        if self.mcp is not None:
            child_env[TOKEN_ENV] = self.mcp_token
        self._open(child_env)

    async def send(self, item: dict) -> None:
        await self._write(item)

    async def receive(self, timeout: float | None = None) -> dict | None:
        return await self._read(timeout)

    async def initialize(self, prompt: str) -> None:
        await self.send({"type": "control_request", "request_id": "init-1",
                         "request": {"subtype": "initialize"}})
        reply = await self.receive(timeout=30)
        response = (reply or {}).get("response") or {}
        if (not reply or reply.get("type") != "control_response" or response.get("subtype") != "success"
                or response.get("request_id") != "init-1"):
            raise CliBackendError(f"Claude CLI returned an invalid initialize response: {reply!r}"[:500])
        await self.send(self.user_message(prompt))

    def user_message(self, content: str) -> dict:
        return {"type": "user", "message": {"role": "user", "content": content},
                "parent_tool_use_id": None, "session_id": ""}

    async def respond_permission(self, request_id: str, behavior: str, args: dict,
                                 message: str = "") -> None:
        decision: dict[str, object] = {"behavior": behavior}
        if behavior == "allow":
            decision["updatedInput"] = args
        elif message:
            decision["message"] = message
        await self.send({"type": "control_response", "response": {"subtype": "success",
                         "request_id": request_id, "response": decision}})
