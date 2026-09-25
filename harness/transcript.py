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


def _r_user_message(at: str, d: dict, last_content: str) -> list[str]:
    return [f"## {at} · User", "", d["content"], ""]


def _r_assistant(at: str, d: dict, last_content: str) -> list[str]:
    header = f"### {at} · Assistant"
    if d.get("gen_tps"):
        header += f" ({d['completion_tokens']} tokens, {d['gen_tps']} tok/s)"
    lines = [header, ""]
    if d.get("reasoning"):
        lines += ["<details><summary>Reasoning</summary>", "", d["reasoning"].strip(), "", "</details>", ""]
    if d.get("content", "").strip():
        lines += [d["content"].strip(), ""]
    for call in d.get("tool_calls") or []:
        lines += [f"- call `{call['function']['name']}` `{call['function']['arguments'][:500]}`"]
    if d.get("tool_calls"):
        lines.append("")
    return lines


def _r_tool_call(at: str, d: dict, last_content: str) -> list[str]:
    if d["decision"] == "allow":
        return []
    return [f"- policy: **{d['decision']}** `{d['name']}` ({d['reason']})", ""]


def _r_approval_requested(at: str, d: dict, last_content: str) -> list[str]:
    lines = [f"#### {at} · Approval requested `{d['id']}`: {d['tool']} ({d['reason']})", "",
             _block(json.dumps(d["args"], indent=1), 2000), ""]
    if d.get("detail"):
        lines += [_block(d["detail"], 4000), ""]
    return lines


def _r_approval_decided(at: str, d: dict, last_content: str) -> list[str]:
    note = f": {d['note']}" if d.get("note") else ""
    return [f"#### {at} · Approval `{d['id']}` {d['status']}{note}", ""]


def _r_approval_auto_approved(at: str, d: dict, last_content: str) -> list[str]:
    return [f"#### {at} · Auto-approved `{d.get('id', '')}`: deterministic gate and smart reviewer "
            f"both allowed this {d.get('tool') or 'call'} ({d.get('reason') or 'routine workspace work'})", ""]


def _r_smart_review(at: str, d: dict, last_content: str) -> list[str]:
    return [f"> {at} · smart review {d.get('outcome')} ({d.get('recommendation')} "
            f"{int(round((d.get('confidence') or 0) * 100))}%)", ""]


def _r_tool_result(at: str, d: dict, last_content: str) -> list[str]:
    status = "ok" if d["ok"] else "error"
    return [f"#### {at} · Result `{d['name']}` ({status}, {d['seconds']}s)", "", _block(d["output"]), ""]


def _r_snippet_started(at: str, d: dict, last_content: str) -> list[str]:
    return [f"#### {at} · Ran {d.get('label', d.get('language'))} snippet `{d['id']}`", "",
            _block(d.get("source", "")), ""]


def _r_snippet_result(at: str, d: dict, last_content: str) -> list[str]:
    tc = d.get("toolchain") or {}
    reasons = f" ({', '.join(d['reasons'])})" if d.get("reasons") else ""
    lines = [f"#### {at} · Snippet `{d['id']}` {d.get('status')}{reasons} · {tc.get('version') or '-'}", ""]
    if d.get("error"):
        lines += [d["error"], ""]
    if d.get("compile"):
        lines += [f"Compiler exit {d['compile']['exit_code']}:", "", _block(d["compile"]["output"]), ""]
    for stream in ("stdout", "stderr"):
        if (d.get("run") or {}).get(stream):
            lines += [f"{stream} (exit {d['run']['exit_code']}):", "", _block(d["run"][stream]), ""]
    return lines


def _r_compaction(at: str, d: dict, last_content: str) -> list[str]:
    lines = [f"#### {at} · Context compaction ({d['tier']}): ~{d['tokens_before']} → "
             f"~{d['tokens_after']} tokens", ""]
    if d.get("summary"):
        lines += ["<details><summary>Summary</summary>", "", d["summary"].strip(), "", "</details>", ""]
    return lines


def _r_error(at: str, d: dict, last_content: str, kind: str = "error") -> list[str]:
    return [f"> {at} · {kind}: {d.get('message') or d.get('error')}", ""]


def _r_llm_retry(at: str, d: dict, last_content: str) -> list[str]:
    return _r_error(at, d, last_content, "llm_retry")


def _r_model_waking(at: str, d: dict, last_content: str) -> list[str]:
    return [f"> {at} · model was asleep; waking it (about {d['expected_seconds']} s)", ""]


def _r_model_ready(at: str, d: dict, last_content: str) -> list[str]:
    return [f"> {at} · model ready after {d['seconds']} s", ""]


def _r_compaction_started(at: str, d: dict, last_content: str) -> list[str]:
    return [f"> {at} · summarizing {d['messages']} older messages to free context", ""]


def _r_target_waiting(at: str, d: dict, last_content: str) -> list[str]:
    return [f"> {at} · waiting for the {d['target']} (offline or asleep)", ""]


def _r_target_online(at: str, d: dict, last_content: str) -> list[str]:
    return [f"> {at} · {d['target']} back after {d['seconds']} s", ""]


def _r_app_context(at: str, d: dict, last_content: str) -> list[str]:
    return [f"## {at} · Context from app", "", d["content"], ""]


def _r_app_tool_call(at: str, d: dict, last_content: str) -> list[str]:
    return [f"> {at} · waiting for the app to run `{d['name']}`", ""]


def _r_app_tool_result(at: str, d: dict, last_content: str) -> list[str]:
    return [f"> {at} · app returned {'a result' if d['ok'] else 'an error'} ({d['chars']} characters)", ""]


def _r_gpu_paused(at: str, d: dict, last_content: str) -> list[str]:
    return [f"> {at} · paused: {d['reason']} needs the GPU, so the model was unloaded", ""]


def _r_gpu_resumed(at: str, d: dict, last_content: str) -> list[str]:
    return [f"> {at} · GPU free again after {d['seconds']} s; model reloading", ""]


def _r_workspace_ready(at: str, d: dict, last_content: str) -> list[str]:
    return [f"> {at} · cloned `{d['repo']}` on branch `{d['branch']}` from `{d['base_branch']}` "
            f"({d['base_commit'][:10]})", ""]


def _r_branch_saved(at: str, d: dict, last_content: str) -> list[str]:
    extra = " (committed leftover changes)" if d.get("auto_commit") else ""
    return [f"> {at} · branch `{d['branch']}` saved at {d['head']}, {len(d['commits'])} commit(s) "
            f"ahead{extra}", ""]


def _r_review(at: str, d: dict, last_content: str) -> list[str]:
    return [f"## {at} · Review: {d['action']} ({d['detail']})", ""]


def _r_resumed(at: str, d: dict, last_content: str) -> list[str]:
    return [f"> {at} · daemon restarted; session resumed (was {d['status']})", ""]


def _r_status(at: str, d: dict, last_content: str) -> list[str]:
    if d["status"] not in ("done", "cancelled", "failed"):
        return []
    lines = [f"## {at} · {d['status'].capitalize()} ({d.get('stop_reason', '')})", ""]
    if d.get("answer") and d["answer"].strip() != last_content:  # a final message is already shown
        lines += [d["answer"].strip(), ""]
    return lines


_RENDERERS = {
    "user_message": _r_user_message, "assistant": _r_assistant, "tool_call": _r_tool_call,
    "approval_requested": _r_approval_requested, "approval_decided": _r_approval_decided,
    "approval_auto_approved": _r_approval_auto_approved, "smart_review": _r_smart_review,
    "tool_result": _r_tool_result, "snippet_started": _r_snippet_started, "snippet_result": _r_snippet_result,
    "compaction": _r_compaction, "error": _r_error, "llm_retry": _r_llm_retry,
    "model_waking": _r_model_waking, "model_ready": _r_model_ready, "compaction_started": _r_compaction_started,
    "target_waiting": _r_target_waiting, "target_online": _r_target_online, "app_context": _r_app_context,
    "app_tool_call": _r_app_tool_call, "app_tool_result": _r_app_tool_result, "gpu_paused": _r_gpu_paused,
    "gpu_resumed": _r_gpu_resumed, "workspace_ready": _r_workspace_ready, "branch_saved": _r_branch_saved,
    "review": _r_review, "resumed": _r_resumed, "status": _r_status,
}


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
        renderer = _RENDERERS.get(t)
        if renderer is not None:
            lines += renderer(at, d, last_content)
    return "\n".join(lines)


def write_transcript(db: Database, directory: Path, sid: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sid}.md"
    path.write_text(render(db, sid), encoding="utf-8")
    return path
