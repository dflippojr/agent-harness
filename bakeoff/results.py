"""Shared result semantics for reference runs, reports and checker-only rescoring."""

from __future__ import annotations

import math
import statistics
import json


MEASURES = ("turns", "tool_calls", "tool_errors", "model_errors", "invalid_tool_calls", "retries",
            "compactions", "masking_events", "compaction_failures", "prompt_tokens", "completion_tokens",
            "context_tokens")


def error_metrics(errors: list[dict], infrastructure_error: str | None = None) -> dict:
    """Transport failures are infrastructure; HTTP 5xx/tool parsing are model failures."""
    messages = [json.dumps(error) for error in errors]
    transport = [m for m in messages if any(marker in m.lower() for marker in
                 ("econnrefused", "enotfound", "eai_again", "connection refused", "connecterror",
                  "could not resolve host", "connection error", "connect timeout"))]
    return {"model_errors": len(messages) - len(transport), "error_messages": messages,
            "infrastructure_error": infrastructure_error or "\n".join(transport) or None}


def classify(record: dict) -> str | None:
    """Checker failures are model failures unless process/event evidence says otherwise."""
    if record.get("passed"):
        return None
    if str(record.get("note", "")).startswith("checker error:"):
        return "infrastructure"
    if record.get("infrastructure_error") and not record.get("finished"):
        return "infrastructure"
    if record.get("adapter_error"):
        return "adapter"
    if record.get("finished"):
        return "model"
    if record.get("tool_errors") or record.get("compaction_failures"):
        return "harness/tooling"
    if record.get("model_errors") or record.get("invalid_tool_calls"):
        return "model"
    if record.get("exit_code") not in (None, 0):
        return "adapter"
    return "model"


def outcome(record: dict) -> str:
    if record.get("stop_reason") in ("wall_limit", "timeout") or record.get("status") in ("timeout", "wall_limit"):
        return "timeout"
    if record.get("finished"):
        return "completion"
    if record.get("model_errors") or record.get("invalid_tool_calls"):
        return "model_error"
    if record.get("tool_errors"):
        return "tool_error"
    return "incomplete"


def total(rows: list[dict], key: str):
    """Missing telemetry is unknown, not zero; preserve older saved runs."""
    values = [r.get(key) for r in rows]
    return sum(values) if values and all(v is not None for v in values) else None


def report_lines(summaries: list[dict]) -> list[str]:
    lines = ["", "## Stage A measures", "", "Missing telemetry is shown as `unknown`; p90 uses nearest rank.", "",
             "| Cell | Repeat | Pass | Completion | Timeout | Model error | Tool error | Incomplete | Median wall s | p90 wall s | Peak RAM MiB | Peak VRAM MiB |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for summary in summaries:
        rows = summary["tasks"]
        for repeat in sorted({r.get("repeat", 0) for r in rows}):
            runs = [r for r in rows if r.get("repeat", 0) == repeat]
            walls = sorted(r["wall_seconds"] for r in runs)
            counts = [sum(outcome(r) == label for r in runs) for label in
                      ("completion", "timeout", "model_error", "tool_error", "incomplete")]
            ram = summary.get("peak_ram_mib")
            vram = summary.get("peak_vram_mib")
            lines.append(f"| {summary['model']} | {repeat} | {sum(r['passed'] for r in runs)}/{len(runs)} | "
                         + " | ".join(map(str, counts))
                         + f" | {statistics.median(walls):.1f} | {walls[math.ceil(.9 * len(walls)) - 1]:.1f} "
                         + f"| {ram if ram is not None else 'unknown'} | {vram if vram is not None else 'unknown'} |")
        lines += ["", f"### {summary['model']}", "", "| Measure | Total |", "| --- | --- |"]
        for key in MEASURES:
            value = total(rows, key)
            lines.append(f"| {key} | {value if value is not None else 'unknown'} |")
        lines += ["", "| Task | Repeat | Pass | Outcome | Failure class | Stop reason |", "| --- | --- | --- | --- | --- | --- |"]
        for r in rows:
            stop = str(r.get("stop_reason", "")).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r['task']} | {r.get('repeat', 0)} | {r['passed']} | {outcome(r)} "
                         f"| {classify(r) or '-'} | {stop} |")
    return lines
