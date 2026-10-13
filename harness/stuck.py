"""Cheap stuck caps for the native loop (#643): repeated identical failing calls and consecutive invalid calls.

No judge model: the counters live on the run (`run["stuck"]`), so they reset with each run and survive a daemon
restart. Identity is `efficiency.retry_key` (tool name plus canonical arguments); what counts as a failure is
`efficiency.seeds_failure`, so policy blocks, user denials, interrupted and skipped calls never count.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import efficiency

STUCK_REPEAT = "stuck_repeat"
STUCK_INVALID = "stuck_invalid"


@dataclass(frozen=True)
class Caps:
    """Thresholds from `budgets:`; 0 turns a cap off."""
    nudge_after: int = 3     # identical failing calls before the tool result says to stop repeating it
    stop_after: int = 8      # identical failing calls that end the run (stuck_repeat)
    invalid_after: int = 5   # consecutive invalid tool calls that end the run (stuck_invalid)


def nudge(count: int) -> str:
    return (f"\n\nNote: this exact call (same tool, same arguments) has now failed {count} times in this run. "
            "Repeating it will not work; change the arguments or try a different approach.")


def _key(name: str, arguments) -> str:
    tool, args = efficiency.retry_key(name, arguments)
    return hashlib.sha256(f"{tool}\0{args}".encode("utf-8")).hexdigest()[:16]


def observe(run: dict, caps: Caps, name: str, arguments, output: str, ok: bool,
            invalid: bool = False) -> tuple[str, dict | None]:
    """Count one tool result into `run["stuck"]`. Returns (text to append to the result, stop or None); a stop is
    `{"reason", "tool", "count"}` and is also kept on the run as `run["stuck"]["stop"]`."""
    state = run.setdefault("stuck", {"failures": {}, "invalid_streak": 0})
    if state.get("stop"):
        return "", None
    stop = None
    state["invalid_streak"] = state.get("invalid_streak", 0) + 1 if invalid else 0
    if caps.invalid_after > 0 and state["invalid_streak"] >= caps.invalid_after:
        stop = {"reason": STUCK_INVALID, "tool": name, "count": state["invalid_streak"]}
    text = ""
    if not ok and efficiency.seeds_failure({"ok": False, "output": output}, None):
        key = _key(name, arguments)
        count = state["failures"][key] = state["failures"].get(key, 0) + 1
        if caps.nudge_after > 0 and count >= caps.nudge_after:
            text = nudge(count)
        if stop is None and caps.stop_after > 0 and count >= caps.stop_after:
            stop = {"reason": STUCK_REPEAT, "tool": name, "count": count}
    if stop is not None:
        state["stop"] = stop
    return text, stop


def describe(stop: dict) -> str:
    """One line for the transcript, the CLI and the web UI's event row."""
    if stop.get("reason") == STUCK_INVALID:
        return f"Run stopped: {stop.get('count')} invalid tool calls in a row"
    return f"Run stopped: {stop.get('tool')} failed {stop.get('count')} times with the same arguments"


def stop_reason(run: dict) -> str:
    return ((run.get("stuck") or {}).get("stop") or {}).get("reason", "")
