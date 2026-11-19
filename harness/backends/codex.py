"""Codex (`codex app-server --stdio`, JSON-RPC v2): its notifications and requests as neutral events.

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

import json

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
