"""Context-efficiency metrics (#159): composition, dead-end retries, compaction correlation, cache.

Counts are precomputed onto `turn_metrics` events as the native loop runs. Prometheus and the session
API sum those rows; they never scan `tool_result` history on a scrape.
"""

from __future__ import annotations

import json

from . import compaction

CORRELATION_K = 5
CORRELATION_TIERS = ("elide", "summary", "round_reset")
DELEGATE_TOOLS = frozenset({"delegate"})

_INTERRUPTED_MARK = "the daemon restarted while this tool call was running"


def canonical_args(args) -> str:
    """JSON-normalized, sorted-key form of tool arguments (dict or JSON string)."""
    parsed = args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except (json.JSONDecodeError, TypeError, ValueError):
            return args
    try:
        return json.dumps(parsed, sort_keys=True, default=str)
    except TypeError:
        return str(parsed)


def retry_key(name: str, args) -> tuple[str, str]:
    return (name or "", canonical_args(args if args is not None else {}))


def seeds_failure(result: dict, call: dict | None) -> bool:
    """Whether this ok=false result opens a dead-end (policy/deny/interrupt never seed)."""
    if result.get("ok"):
        return False
    output = result.get("output") or ""
    if _INTERRUPTED_MARK in output:
        return False
    if "blocked by policy" in output:
        return False
    if "the user denied this" in output:
        return False
    if output.startswith("Cancelled by the user") or output.startswith("Not run:"):
        return False
    if "this account cannot use that tool" in output:
        return False
    if (call or {}).get("decision") == "deny":
        return False
    return True


def _is_skipped_result(result: dict, call: dict | None) -> bool:
    """Blocks, denials and interrupted calls are not retry attempts."""
    return not seeds_failure(result, call) and not result.get("ok")


def _tool_names(context: list[dict]) -> dict:
    names = {}
    for message in context:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            names[call.get("id")] = fn.get("name") or ""
    return names


def _is_system_state(message: dict) -> bool:
    if message.get("role") == "system":
        return True
    content = message.get("content") or ""
    return content.startswith(compaction.STATE_TAG) or content.startswith(compaction.SUMMARY_TAG)


def compose(context: list[dict], schema_chars: int, chars_per_token: float,
            prompt_tokens: int | None) -> dict:
    """Estimate prompt composition on the context actually sent (after `_maybe_compact`).

    First matching bucket wins: system_state, file_contents, tool_outputs, reasoning_other.
    Tool-schema overhead lands in system_state. Delegate tool results are ignored until #157.
    """
    cpt = chars_per_token or 3.0
    names = _tool_names(context)
    chars = {"system_state": schema_chars, "file_contents": 0, "tool_outputs": 0, "reasoning_other": 0}
    for message in context:
        size = compaction.message_chars(message)
        name = names.get(message.get("tool_call_id"), "")
        if name in DELEGATE_TOOLS and message.get("role") == "tool":
            continue
        if _is_system_state(message):
            chars["system_state"] += size
        elif (message.get("role") == "tool" and name == "read_file"
              and not (message.get("content") or "").startswith(compaction.RECEIPT_PREFIX)):
            chars["file_contents"] += size
        elif message.get("role") == "tool":
            chars["tool_outputs"] += size
        else:
            chars["reasoning_other"] += size
    estimates = {key: int(value / cpt) for key, value in chars.items()}
    reported = int(prompt_tokens or 0)
    if reported > 0:
        return {"buckets": _scale(estimates, reported), "estimated": False}
    return {"buckets": estimates, "estimated": True}


def _scale(estimates: dict[str, int], target: int) -> dict[str, int]:
    keys = ("system_state", "tool_outputs", "file_contents", "reasoning_other")
    values = [estimates.get(key, 0) for key in keys]
    total = sum(values)
    if total <= 0:
        return {keys[0]: target, keys[1]: 0, keys[2]: 0, keys[3]: 0}
    scaled = [int(value * target / total) for value in values]
    scaled[0] += target - sum(scaled)
    return dict(zip(keys, scaled))


def recomputed_tokens(prompt_tokens: int | None, cache_tokens: int | None) -> int | None:
    if cache_tokens is None or prompt_tokens is None:
        return None
    return max(0, int(prompt_tokens) - int(cache_tokens))


def empty_correlated() -> dict[str, int]:
    return {tier: 0 for tier in CORRELATION_TIERS}


class RetryTracker:
    """Replay native-loop events to count this turn's dead-end and compaction-correlated retries."""

    def __init__(self):
        self.calls: dict = {}
        self.failures: set[tuple[str, str]] = set()
        self.model_turn = 0
        self.windows: list[dict] = []
        self._pass_window: dict | None = None
        self.dead_end_retries = 0
        self.correlated = empty_correlated()

    def replay(self, events: list[dict]) -> None:
        for event in events:
            self.consume(event)

    def consume(self, event: dict) -> None:
        type_ = event["type"]
        data = event.get("data") or {}
        if type_ == "assistant":
            self.model_turn += 1
            self._pass_window = None
            for call in data.get("tool_calls") or []:
                fn = call.get("function") or {}
                self._register(call.get("id"), fn.get("name") or "", fn.get("arguments"), None)
        elif type_ == "tool_call":
            self._register(data.get("id"), data.get("name") or "", data.get("args"), data.get("decision"))
        elif type_ == "tool_result":
            self._on_result(data)
        elif type_ == "compaction":
            self._on_compaction(data)
        elif type_ == "turn_metrics":
            self._pass_window = None
            self.dead_end_retries = 0
            self.correlated = empty_correlated()

    def _register(self, call_id, name, args, decision) -> None:
        if not call_id:
            return
        slot = self.calls.setdefault(call_id, {})
        if name:
            slot["name"] = name
        if args is not None:
            slot["args"] = args
        if decision is not None:
            slot["decision"] = decision

    def _on_compaction(self, data: dict) -> None:
        tier = data.get("tier")
        if tier not in CORRELATION_TIERS:
            return
        if self._pass_window is not None:
            self._pass_window["tiers"].add(tier)
            return
        window = {"tiers": {tier}, "start_turn": self.model_turn, "failures": set(self.failures)}
        self.windows.append(window)
        self._pass_window = window

    def _on_result(self, data: dict) -> None:
        call = self.calls.get(data.get("id"))
        if call is None:
            return
        key = retry_key(call.get("name") or data.get("name") or "", call.get("args"))
        skipped = _is_skipped_result(data, call)
        if key in self.failures and not skipped:
            self.dead_end_retries += 1
            for window in self.windows:
                if key not in window["failures"]:
                    continue
                delta = self.model_turn - window["start_turn"]
                if 1 <= delta <= CORRELATION_K:
                    for tier in window["tiers"]:
                        self.correlated[tier] = self.correlated.get(tier, 0) + 1
        if data.get("ok"):
            self.failures.discard(key)
        elif seeds_failure(data, call):
            self.failures.add(key)


def turn_increments(events: list[dict]) -> tuple[int, dict[str, int]]:
    """Dead-end and correlated retry increments since the last `turn_metrics` event."""
    tracker = RetryTracker()
    tracker.replay(events)
    return tracker.dead_end_retries, dict(tracker.correlated)


def _max_output_chars(events: list[dict]) -> tuple[int | None, dict[str, int]]:
    largest = None
    by_tool: dict[str, int] = {}
    for event in events:
        if event["type"] != "tool_result":
            continue
        data = event.get("data") or {}
        chars = data.get("output_chars")
        if chars is None:
            continue
        chars = int(chars)
        largest = chars if largest is None else max(largest, chars)
        name = data.get("name") or "?"
        by_tool[name] = max(by_tool.get(name, 0), chars)
    return largest, by_tool


def session_payload(session_id: str, events: list[dict]) -> dict:
    turns = []
    retries = None
    correlated = None
    for event in events:
        if event["type"] != "turn_metrics":
            continue
        data = event.get("data") or {}
        composition = data.get("composition")
        turns.append({
            "turn": data.get("turn"),
            "prompt_tokens": data.get("prompt_tokens"),
            "completion_tokens": data.get("completion_tokens"),
            "composition": composition,
            "estimated": data.get("estimated"),
            "cache_tokens": data.get("cache_tokens"),
            "recomputed_tokens": data.get("recomputed_tokens"),
        })
        if "dead_end_retries" in data:
            retries = (retries or 0) + int(data.get("dead_end_retries") or 0)
            row = data.get("compaction_correlated_retries") or {}
            if correlated is None:
                correlated = empty_correlated()
            for tier in CORRELATION_TIERS:
                correlated[tier] += int(row.get(tier) or 0)
    if correlated is None:
        correlated = {tier: None for tier in CORRELATION_TIERS}
    largest, by_tool = _max_output_chars(events)
    return {
        "session_id": session_id,
        "turns": turns,
        "aggregate": {
            "dead_end_retries": retries,
            "compaction_correlated_retries": correlated,
            "largest_tool_output_chars": largest,
            "largest_tool_output_by_tool": by_tool,
        },
    }
