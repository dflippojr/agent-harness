"""The neutral events a hosted CLI's translator emits (#991). They extend the print-mode vocabulary of #168 (session,
text, tool, step, error, exit) with the outcomes the built-in backends add: rate limits and permission requests.

A translator turns one vendor record into a list of these. It is pure: it keeps only the parse state of its own
session (tool names by call id, Codex's items, Cursor's aggregate answer) and touches no database, bus or Runner.
`Runner._apply` does the rest: it records the events, emits the session events, asks the approval policy, and stops
the run on a limit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol, Union


@dataclass(frozen=True)
class Session:
    """The CLI's own session id (Claude's session, Codex's thread, Cursor's chat), kept to resume after a restart."""
    id: str


@dataclass(frozen=True)
class Delta:
    """Streamed text, shown live and never stored. `kind` is "content" or "reasoning"."""
    kind: str
    text: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass(frozen=True)
class Assistant:
    """An assistant message: its text and the tool calls it makes."""
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolResult:
    """A tool call's outcome as the CLI reports it, `seconds` when it says how long the call took."""
    id: str
    name: str
    ok: bool
    output: str
    seconds: float = 0.0


@dataclass(frozen=True)
class RateLimits:
    """The provider's usage windows in the shared shape (`status`, `utilization` 0..1, `resetsAt`, `rateLimitType`,
    `unifiedWindows`). `limit_reached`: the provider refuses more until `resets_at` (0 when it doesn't say).
    `billing`: the record also says how usage is billed (Claude's overage flags), so the billing warning is checked
    on it again."""
    info: dict
    limit_reached: bool = False
    resets_at: float = 0.0
    billing: bool = False


@dataclass(frozen=True)
class PermissionRequest:
    """The CLI asks before a tool call. The answer goes back to `request_id` (`respond_permission`); `call_id` is the
    tool call it is about ("" when the request carries none). `decidable` False: nothing the approval policy can
    decide (an MCP server's form that belongs to no tool call), so it is declined at once."""
    request_id: object
    tool: str
    args: dict
    call_id: str = ""
    description: str = ""
    decidable: bool = True


@dataclass(frozen=True)
class Result:
    """The end of the CLI's turn. `is_error`: the turn failed. `limit_reached`: it failed on a usage limit, so the run
    waits for the reset or falls back to an API key instead of failing. `show_answer`: the answer has not been shown
    as an assistant message yet (the turn-based CLIs only have it once the turn ends)."""
    answer: str = ""
    usage: dict = field(default_factory=dict)
    turns: int = 0
    cost: float = 0.0
    is_error: bool = False
    subtype: str = ""
    limit_reached: bool = False
    show_answer: bool = False


@dataclass(frozen=True)
class Error:
    """The CLI reported a failure that ends the run."""
    message: str


Event = Union[Session, Delta, Assistant, ToolResult, RateLimits, PermissionRequest, Result, Error]


class Translator(Protocol):
    def translate(self, record: object) -> list[Event]:
        """The neutral events for one record the transport received."""
        ...


FAILED_SUBTYPES = ("error", "failed")


def obj(value: object, key: str) -> dict:
    """`value[key]` when both are JSON objects, else {}: vendor records are untrusted shapes."""
    found = value.get(key) if isinstance(value, dict) else None
    return found if isinstance(found, dict) else {}


def text_of(content: object) -> str:
    """The text in a message's content: a string, or the text of its blocks (nested tool-result content included)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else json.dumps(content, ensure_ascii=False)
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            value = block.get("text")
            inner = block.get("content")
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(inner, str):
                parts.append(inner)
            elif isinstance(inner, list):
                parts.append(text_of(inner))
    return "\n".join(x for x in parts if x)


def result_of(record: dict) -> Result:
    """A result record in Claude Code's shape (`result`, `usage`, `num_turns`, `total_cost_usd`, `is_error`,
    `subtype`) as a Result. A `subtype` of error or failed fails the turn even without `is_error`."""
    subtype = str(record.get("subtype") or "")
    return Result(answer=str(record.get("result") or ""), usage=obj(record, "usage"),
                  turns=int(record.get("num_turns") or 0), cost=float(record.get("total_cost_usd") or 0),
                  is_error=bool(record.get("is_error")) or subtype in FAILED_SUBTYPES, subtype=subtype)
