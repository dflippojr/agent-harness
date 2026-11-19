"""Cursor Agent (`agent -p --output-format stream-json --stream-partial-output`): its records as neutral events.

Cursor's print mode has no host approval protocol (the session runs with `--force`, confined by its container) and
reports no rate limits; a usage limit only shows as a failed result. The records a session turns into events:

- `system`/`init`: the chat id, kept for `--resume`;
- `thinking`: reasoning deltas;
- `assistant`: with `timestamp_ms`, a streamed delta; without it, the turn's aggregate text, which is the answer;
- `tool_call`: `started` and `completed`, the tool named by the one key under `tool_call`;
- `result`: the turn's end. One process runs one turn, and messages that arrive meanwhile become the next turn on
  the same chat, so usage, turns and cost add up over the session's turns.
"""

from __future__ import annotations

from .events import (FAILED_SUBTYPES, Assistant, Delta, Event, Result, Session, ToolCall, ToolResult, obj,
                     text_of)

LIMIT_MARKERS = ("rate limit", "quota", "usage limit")


def cursor_tool(record: dict) -> tuple[str, dict, dict]:
    """A tool_call record's tool: its name (the key under `tool_call`), arguments and result."""
    for name, value in obj(record, "tool_call").items():
        if isinstance(value, dict):
            return name, obj(value, "args"), obj(value, "result")
    return "cursorTool", {}, {}


class CursorTranslator:
    """One Cursor session's records over its turns, as neutral events. It keeps the answer (the latest aggregate
    assistant text, or the result's) and the usage, turns and cost the turns so far add up to."""

    def __init__(self) -> None:
        self.answer = ""
        self.usage: dict[str, int] = {}
        self.turns = 0
        self.cost = 0.0

    def translate(self, record: object) -> list[Event]:
        if not isinstance(record, dict):
            return []
        type_ = str(record.get("type") or "")
        if type_ == "system" and record.get("subtype") == "init":
            chat_id = str(record.get("session_id") or "")
            return [Session(chat_id)] if chat_id else []
        if type_ in ("assistant", "thinking"):
            return self._text(record, type_)
        if type_ == "tool_call":
            return self._tool(record)
        if type_ == "result":
            return [self._result(record)]
        return []

    def _text(self, record: dict, type_: str) -> list[Event]:
        text = text_of(obj(record, "message").get("content"))
        if not text and isinstance(record.get("text"), str):
            text = record["text"]
        if not text:
            return []
        # With --stream-partial-output Cursor emits timestamped deltas, then one untimestamped aggregate assistant
        # record. Stream the deltas and keep the aggregate without showing it twice.
        streamed = type_ == "thinking" or record.get("timestamp_ms") is not None
        if type_ == "assistant" and record.get("timestamp_ms") is None:
            self.answer = text
        return [Delta("reasoning" if type_ == "thinking" else "content", text)] if streamed else []

    @staticmethod
    def _tool(record: dict) -> list[Event]:
        call_id = str(record.get("call_id") or "")
        name, args, result = cursor_tool(record)
        if record.get("subtype") == "started":
            return [Assistant(tool_calls=(ToolCall(call_id, name, args),))]
        if record.get("subtype") == "completed":
            ok = "success" in result and "error" not in result
            return [ToolResult(call_id, name, ok, text_of(result.get("success") if ok else result.get("error", result)))]
        return []

    def _result(self, record: dict) -> Result:
        raw = obj(record, "usage")
        cache_read = int(raw.get("cacheReadTokens") or raw.get("cache_read_input_tokens") or 0)
        cache_write = int(raw.get("cacheWriteTokens") or raw.get("cache_creation_input_tokens") or 0)
        total_input = int(raw.get("inputTokens") or raw.get("input_tokens") or 0)
        usage = {"input_tokens": max(0, total_input - cache_read - cache_write),
                 "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write,
                 "output_tokens": int(raw.get("outputTokens") or raw.get("output_tokens") or 0)}
        for key, value in usage.items():
            self.usage[key] = self.usage.get(key, 0) + value
        self.turns += int(record.get("num_turns") or 1)
        self.cost += float(record.get("total_cost_usd") or 0)
        self.answer = str(record.get("result") or self.answer)
        subtype = str(record.get("subtype") or "")
        is_error = bool(record.get("is_error")) or subtype in FAILED_SUBTYPES
        return Result(answer=self.answer, usage=dict(self.usage), turns=self.turns, cost=self.cost,
                      is_error=is_error, subtype=subtype, show_answer=True,
                      limit_reached=is_error and any(m in self.answer.lower() for m in LIMIT_MARKERS))
