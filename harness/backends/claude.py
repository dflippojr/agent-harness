"""Claude Code (`claude -p --input-format stream-json --output-format stream-json`): its records as neutral events.

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

from dataclasses import replace

from .events import (Assistant, Delta, Event, PermissionRequest, RateLimits, Session, ToolCall, ToolResult, obj,
                     result_of, text_of)

REASONING_DELTAS = ("thinking_delta", "reasoning_delta")


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
