"""Readable Markdown transcript built from the persisted event log."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .db import Database


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _block(text: str, limit: int = 4000) -> str:
    text = text.rstrip()
    if len(text) > limit:
        text = text[: limit // 2] + f"\n... [{len(text) - limit} characters omitted] ...\n" + text[-limit // 2:]
    return "```\n" + text.replace("```", "``​`") + "\n```"


def render(db: Database, sid: str) -> str:
    s = db.get_session(sid)
    if s is None:
        raise KeyError(sid)
    created = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["created_at"]))
    totals = s["totals"]
    lines = [
        f"# {s['title']}",
        "",
        f"- Session `{s['id']}` · project `{s['project']}` · target `{s['target']}` · model `{s['model']}`",
        f"- Created {created} · status **{s['status']}** ({s['stop_reason'] or '-'})",
        f"- Totals: {totals.get('turns', 0)} model turns, {totals.get('prompt_tokens', 0)} prompt tokens, "
        f"{totals.get('completion_tokens', 0)} completion tokens",
        "",
    ]
    last_content = ""
    for e in db.events(sid):
        d, t, at = e["data"], e["type"], _clock(e["ts"])
        if t == "assistant":
            last_content = d.get("content", "").strip()
        if t == "user_message":
            lines += [f"## {at} · User", "", d["content"], ""]
        elif t == "assistant":
            header = f"### {at} · Assistant"
            if d.get("gen_tps"):
                header += f" ({d['completion_tokens']} tokens, {d['gen_tps']} tok/s)"
            lines += [header, ""]
            if d.get("reasoning"):
                lines += ["<details><summary>Reasoning</summary>", "", d["reasoning"].strip(), "", "</details>", ""]
            if d.get("content", "").strip():
                lines += [d["content"].strip(), ""]
            for call in d.get("tool_calls") or []:
                lines += [f"- call `{call['function']['name']}` `{call['function']['arguments'][:500]}`"]
            if d.get("tool_calls"):
                lines.append("")
        elif t == "tool_call" and d["decision"] != "allow":
            lines += [f"- policy: **{d['decision']}** `{d['name']}` ({d['reason']})", ""]
        elif t == "approval_requested":
            lines += [f"#### {at} · Approval requested `{d['id']}`: {d['tool']} ({d['reason']})", "",
                      _block(json.dumps(d["args"], indent=1), 2000), ""]
            if d.get("detail"):
                lines += [_block(d["detail"], 4000), ""]
        elif t == "approval_decided":
            note = f": {d['note']}" if d.get("note") else ""
            lines += [f"#### {at} · Approval `{d['id']}` {d['status']}{note}", ""]
        elif t == "tool_result":
            status = "ok" if d["ok"] else "error"
            lines += [f"#### {at} · Result `{d['name']}` ({status}, {d['seconds']}s)", "", _block(d["output"]), ""]
        elif t == "compaction":
            lines += [f"#### {at} · Context compaction ({d['tier']}): ~{d['tokens_before']} → "
                      f"~{d['tokens_after']} tokens", ""]
            if d.get("summary"):
                lines += ["<details><summary>Summary</summary>", "", d["summary"].strip(), "", "</details>", ""]
        elif t in ("error", "llm_retry"):
            lines += [f"> {at} · {t}: {d.get('message') or d.get('error')}", ""]
        elif t == "resumed":
            lines += [f"> {at} · daemon restarted; session resumed (was {d['status']})", ""]
        elif t == "status" and d["status"] in ("done", "cancelled", "failed"):
            lines += [f"## {at} · {d['status'].capitalize()} ({d.get('stop_reason', '')})", ""]
            if d.get("answer") and d["answer"].strip() != last_content:  # a final message is already shown
                lines += [d["answer"].strip(), ""]
    return "\n".join(lines)


def write_transcript(db: Database, directory: Path, sid: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sid}.md"
    path.write_text(render(db, sid), encoding="utf-8")
    return path
