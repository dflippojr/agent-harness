"""Session taint: once a session has read untrusted content, risky actions that a rule would allow are asked about.

The record is a capped JSON list of {kind, origin, first_seen} on the session row, deduplicated by origin. It is only
cleared by the owner's explicit action. Known v1 gaps: web_fetch to an arbitrary URL and network-less commands stay as
the policy decides; Chat sessions and app context are not tracked.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse

from .policy import ALLOW, ASK, Decision

MAX_SOURCES = 20
SHELL_TOOLS = ("run_shell", "Bash", "exec_command")
WEB_FETCH_TOOLS = ("web_fetch", "WebFetch")
WEB_SEARCH_TOOLS = ("web_search", "WebSearch")
ALWAYS_TAINT_ASK = ("restart_service", "rebuild_service", "memory_write")


def source_for(name: str, args: dict, *, mcp_client: bool = False) -> tuple[str, str] | None:
    """(kind, origin) when a successful call to this tool brings untrusted content into the session."""
    if mcp_client or (name.startswith("mcp__") and not name.startswith("mcp__harness__")):
        return "mcp", name
    if name in WEB_FETCH_TOOLS:
        host = urlparse(str(args.get("url") or "")).hostname
        return "web_fetch", host or "a web page"
    if name in WEB_SEARCH_TOOLS:
        return "web_search", "search results"
    return None


def add(taint: list, kind: str, origin: str, now: float | None = None) -> list | None:
    """The taint list with this source added, or None when the origin is already recorded."""
    if any(t.get("origin") == origin for t in taint):
        return None
    out = [*taint, {"kind": kind, "origin": origin, "first_seen": now if now is not None else time.time()}]
    return out[-MAX_SOURCES:] if len(out) > MAX_SOURCES else out


def reason(taint: list) -> str:
    extra = f" (+{len(taint) - 1} more)" if len(taint) > 1 else ""
    return f"session has read untrusted content from {taint[0]['origin']}{extra}"


def is_risky(name: str, args: dict, app_tool_names: set[str]) -> bool:
    if name in SHELL_TOOLS:
        return bool(args.get("network")) or bool(re.search(r"\bgit\s+push\b", str(args.get("command", ""))))
    if name == "git_clone":  # a remote clone runs with network access, like a network command
        return not str(args.get("url") or "").startswith("local:")
    return name in ALWAYS_TAINT_ASK or name in app_tool_names


def escalate(decision: Decision, name: str, args: dict, taint: list, app_tool_names: set[str]) -> Decision:
    """The decision after the taint layer: ALLOW of a risky call becomes ASK; DENY is unchanged."""
    if not taint:
        return decision
    if decision.action == ALLOW and is_risky(name, args, app_tool_names):
        return Decision(ASK, reason(taint))
    if decision.action == ASK:
        return Decision(ASK, decision.reason, smart_eligible=False)
    return decision
