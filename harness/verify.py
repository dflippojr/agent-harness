"""Summarizing verify tool: parse check logs into a bounded failure list.

Configured commands are owner-trusted and run with network=false through the existing
sandbox path. The model-facing result is rendered text only; structured JSON is for
tests and tool_result events. Pytest parsing is tested against `--tb=short -ra`.
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict

from .fileops import ToolError

PYTEST_CMD = re.compile(r"\bpytest\b")
PYTEST_SUMMARY = re.compile(r"^(FAILED|ERROR)\s+(\S+?)(?:\s+-\s+(.*))?$", re.M)
PYTEST_LOCATION = re.compile(r"^(\S+?):(\d+):\s+in\s+(\S+)", re.M)
GENERIC_LINE = re.compile(r"(?i)(error|fail)")
# Timings ("1.23s", "45ms"), PIDs, and hex addresses must not split otherwise-identical failures.
TIME_RE = re.compile(r"\b\d+\.\d+s\b|\b\d+\s*ms\b", re.I)
PID_RE = re.compile(r"(?i)\bpid\s*[=:]?\s*\d+\b")
HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")

ARTIFACT_FOOTER = ("... (output truncated; {total} characters total; "
                   "recover with read_artifact(artifact_id={artifact_id}, start=0, end=20000))")


class ToolOutput:
    """A tool result that may attach the untruncated raw content for artifact storage."""

    def __init__(self, text: str, *, artifact_content: str | None = None, extra: dict | None = None):
        self.text = text
        self.artifact_content = artifact_content
        self.extra = extra or {}


def infer_parser(check) -> str:
    parser = str(getattr(check, "parser", "") or "").strip().lower()
    if parser in ("pytest", "generic"):
        return parser
    return "pytest" if PYTEST_CMD.search(getattr(check, "command", "") or "") else "generic"


def normalize_message(message: str) -> str:
    text = TIME_RE.sub("<time>", message)
    text = PID_RE.sub("pid=<pid>", text)
    return HEX_RE.sub("<hex>", text)


def parse_pytest(log: str) -> list[dict]:
    locations = [(m.group(1), int(m.group(2)), m.group(3)) for m in PYTEST_LOCATION.finditer(log)]
    found: list[dict] = []
    for m in PYTEST_SUMMARY.finditer(log):
        kind, nodeid, message = m.group(1).lower(), m.group(2), (m.group(3) or "").strip()
        path = nodeid.split("::", 1)[0]
        test = nodeid.rsplit("::", 1)[-1] if "::" in nodeid else ""
        line = None
        for loc_path, loc_line, where in locations:
            if path in loc_path or loc_path in path:
                if not test or test in where or where in test:
                    line = loc_line
                    break
        found.append({"kind": kind, "file": path, "line": line, "message": message or kind, "nodeid": nodeid})
    return found


def parse_generic(log: str) -> list[dict]:
    lines = [ln for ln in log.splitlines() if GENERIC_LINE.search(ln)]
    if not lines:
        tail = log[-2000:] if log else ""
        return [{"kind": "log", "file": "", "line": None, "message": tail}] if tail else []
    return [{"kind": "error", "file": "", "line": None, "message": ln} for ln in lines]


def _dedup(check_name: str, items: list[dict]) -> list[dict]:
    groups: OrderedDict[tuple, dict] = OrderedDict()
    for item in items:
        key = (check_name, item.get("file") or "", item.get("line"),
               normalize_message(item.get("message") or ""))
        if key not in groups:
            groups[key] = {**item, "check": check_name, "count": 1,
                           "message": item.get("message") or ""}
        else:
            groups[key]["count"] += 1
    return list(groups.values())


def render_verify(payload: dict) -> str:
    checks = payload.get("checks") or []
    failures = payload.get("failures") or []
    n_fail = sum(1 for f in failures if f.get("kind") == "failed")
    n_error = sum(1 for f in failures if f.get("kind") in ("error", "log"))
    n_timeout = sum(1 for c in checks if c.get("timed_out"))
    if payload.get("ok") and not n_timeout:
        header = f"verify: all checks passed ({len(checks)} checks)"
    else:
        parts = []
        if n_fail:
            parts.append(f"{n_fail} failed")
        if n_error:
            parts.append(f"{n_error} error" + ("" if n_error == 1 else "s"))
        if n_timeout:
            parts.append(f"{n_timeout} timed out")
        header = f"verify: {', '.join(parts) or 'checks did not pass'} ({len(checks)} checks)"
    lines = [header]
    named = {f.get("check") for f in failures}
    for check in checks:
        if check.get("timed_out"):
            lines.append(f"[{check['name']}] TIMED OUT after {check.get('timeout')}s (exit 124)")
        elif check.get("code") not in (0, None) and check["name"] not in named:
            lines.append(f"[{check['name']}] exit {check['code']}")
    for item in failures:
        loc = item.get("file") or ""
        if item.get("line"):
            loc = f"{loc}:{item['line']}" if loc else f":{item['line']}"
        repeat = f" (×{item['count']})" if item.get("count", 1) > 1 else ""
        kind = (item.get("kind") or "error").upper()
        message = item.get("message") or ""
        prefix = f"[{item.get('check')}] {kind}"
        detail = " ".join(p for p in (loc, message) if p)
        lines.append(f"{prefix} {detail}{repeat}".rstrip() if detail else f"{prefix}{repeat}")
    return "\n".join(lines)


def bound_rendered(text: str, limit: int, raw_log: str) -> str:
    digest = hashlib.sha256(raw_log.encode("utf-8")).hexdigest()
    if len(text) <= limit:
        return text
    footer = ARTIFACT_FOOTER.format(total=len(raw_log), artifact_id=digest)
    return text[:limit] + "\n" + footer


async def run_verify(checks: list, exec_cmd, summary_chars: int) -> ToolOutput:
    """Run every configured check serially. `exec_cmd(command, timeout) -> (code, output)`."""
    if not checks:
        raise ToolError("no configured checks")
    logs: list[str] = []
    check_rows: list[dict] = []
    failures: list[dict] = []
    for check in checks:
        timeout = min(int(getattr(check, "timeout", 120) or 120), 1800)
        code, output = await exec_cmd(check.command, timeout)
        timed_out = code == 124
        logs.append(f"=== {check.name} (exit {code}) ===\n{output}")
        row = {"name": check.name, "code": code, "timed_out": timed_out, "timeout": timeout,
               "parser": infer_parser(check)}
        check_rows.append(row)
        parser = row["parser"]
        parsed = parse_pytest(output) if parser == "pytest" else parse_generic(output)
        if timed_out and not parsed:
            parsed = [{"kind": "error", "file": "", "line": None, "message": f"timed out after {timeout}s"}]
        failures.extend(_dedup(check.name, parsed))
    raw_log = "\n\n".join(logs)
    ok = all(row["code"] == 0 and not row["timed_out"] for row in check_rows)
    payload = {"ok": ok, "checks": check_rows, "failures": failures}
    rendered = bound_rendered(render_verify(payload), summary_chars, raw_log)
    return ToolOutput(rendered, artifact_content=raw_log, extra={"verify": payload})
