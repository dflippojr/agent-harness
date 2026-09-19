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
    ]
    frozen = s.get("skills") or []
    if frozen:
        names = ", ".join(f"`{item.get('slug')}` v{item.get('version')} ({(item.get('content_hash') or '')[:12]})"
                          for item in frozen)
        lines += [f"- Frozen skills: {names}"]
    lines += ["",]
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
        elif t == "model_waking":
            lines += [f"> {at} · model was asleep; waking it (about {d['expected_seconds']} s)", ""]
        elif t == "model_ready":
            lines += [f"> {at} · model ready after {d['seconds']} s", ""]
        elif t == "compaction_started":
            lines += [f"> {at} · summarizing {d['messages']} older messages to free context", ""]
        elif t == "target_waiting":
            lines += [f"> {at} · waiting for the {d['target']} (offline or asleep)", ""]
        elif t == "target_online":
            lines += [f"> {at} · {d['target']} back after {d['seconds']} s", ""]
        elif t == "app_context":
            lines += [f"## {at} · Context from app", "", d["content"], ""]
        elif t == "app_tool_call":
            lines += [f"> {at} · waiting for the app to run `{d['name']}`", ""]
        elif t == "app_tool_result":
            lines += [f"> {at} · app returned {'a result' if d['ok'] else 'an error'} ({d['chars']} characters)", ""]
        elif t == "gpu_paused":
            lines += [f"> {at} · paused: {d['reason']} needs the GPU, so the model was unloaded", ""]
        elif t == "gpu_resumed":
            lines += [f"> {at} · GPU free again after {d['seconds']} s; model reloading", ""]
        elif t == "workspace_ready":
            lines += [f"> {at} · cloned `{d['repo']}` on branch `{d['branch']}` from `{d['base_branch']}` "
                      f"({d['base_commit'][:10]})", ""]
        elif t == "branch_saved":
            extra = " (committed leftover changes)" if d.get("auto_commit") else ""
            lines += [f"> {at} · branch `{d['branch']}` saved at {d['head']}, {len(d['commits'])} commit(s) "
                      f"ahead{extra}", ""]
        elif t == "review":
            lines += [f"## {at} · Review: {d['action']} ({d['detail']})", ""]
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
