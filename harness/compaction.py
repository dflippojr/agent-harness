"""Context compaction.

Tier 1 (free): strip old reasoning and shorten old tool outputs.
Tier 2 (one model call): replace older turns with handoff notes, keeping the system prompt, the original task,
and recent turns verbatim.

Compaction only runs before a model call, never after a final answer, so answers are never summarized away
(the failure seen with OpenCode in Phase 0).
"""

from __future__ import annotations

import copy
import hashlib

SUMMARY_TAG = "[Context summary]"

SUMMARY_SYSTEM = """You write handoff notes for an AI agent whose earlier conversation is being removed to free context.
The agent keeps its original task and its most recent turns; your notes replace everything in between.
Be specific and factual. Keep exact file paths, commands, error messages, numbers, and names. Don't invent anything."""

SUMMARY_PROMPT = """Write handoff notes for the conversation excerpt below. Use these sections:

## User requests
Instructions from the user that came after the original task, verbatim where short. Write "none" if there are none.
## Done so far
Actions taken and their outcomes, including files created or edited and what changed.
## Findings
Facts learned: exact values, errors, test results, commands that work or fail. Keep every intermediate result the
task needs (counts, totals, per-item values) so the work doesn't have to be redone.
## Answer so far
Any conclusion or answer already reached, quoted exactly. Write "none yet" if there isn't one.
## Next steps
What remains to finish the task.

The agent's original task (it keeps this verbatim; don't repeat it, but use it to judge what matters):
{task}

Conversation excerpt:
{excerpt}"""


def message_chars(m: dict) -> int:
    size = len(m.get("content") or "") + len(m.get("reasoning_content") or "")
    for call in m.get("tool_calls") or []:
        size += len(call["function"].get("name", "")) + len(call["function"].get("arguments", ""))
    return size + 16


def estimate_tokens(messages: list[dict], chars_per_token: float) -> int:
    return int(sum(message_chars(m) for m in messages) / chars_per_token)


def _head_len(messages: list[dict]) -> int:
    """The system prompt and original task stay pinned. An earlier summary is not pinned: it is folded into the
    next summary along with everything else being condensed."""
    return 2 if len(messages) > 1 and messages[1]["role"] == "user" else 1


def last_turn_start(messages: list[dict]) -> int:
    """Index of the newest assistant message. Tool results after it haven't been seen by the model yet."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] == "assistant":
            return i
    return len(messages)


def mask_used_results(messages: list[dict], outcomes: dict, minimum: int) -> tuple[list[dict], dict[str, str], int]:
    """Replace old successful tool results with receipts and return (context, artifacts, chars_saved)."""
    out = copy.deepcopy(messages)
    call_info = {}
    for message in out:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                call_info[call.get("id")] = (fn.get("name", ""), fn.get("arguments", "{}"))
    artifacts: dict[str, str] = {}
    saved = 0
    cutoff = last_turn_start(out)
    for i, message in enumerate(out):
        if i >= cutoff or message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id")
        outcome = outcomes.get(call_id, {})
        name, args = call_info.get(call_id, (outcome.get("name", ""), "{}"))
        content = message.get("content") or ""
        if (outcome.get("ok") is not True or name == "read_artifact" or len(content) < minimum):
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        artifacts[digest] = content
        rendered_args = str(args).replace("\n", " ")[:200]
        receipt = (f"[Observation receipt] tool={name} arguments={rendered_args} "
                   f"characters={len(content)} sha256={digest} "
                   f"Recover with read_artifact(artifact_id={digest}, start, end).")
        saved += len(content) - len(receipt)
        message["content"] = receipt
    return out, artifacts, saved


def elide(messages: list[dict], keep_last: int = 6, max_chars: int = 1500) -> tuple[list[dict], int]:
    """Tier 1. Returns (new messages, characters saved)."""
    out = copy.deepcopy(messages)
    saved = 0
    cutoff = min(len(out) - keep_last, last_turn_start(out))
    for i, m in enumerate(out):
        if i >= cutoff:
            break
        if m["role"] == "assistant" and m.get("reasoning_content"):
            saved += len(m.pop("reasoning_content"))
        if m["role"] == "tool" and len(m.get("content") or "") > max_chars:
            text = m["content"]
            short = (f"{text[:600]}\n... [older tool output shortened from {len(text)} characters; "
                     f"re-run the tool if you need it] ...\n{text[-600:]}")
            saved += len(text) - len(short)
            m["content"] = short
    return out, saved


def split_for_summary(messages: list[dict], keep_chars: int) -> tuple[int, int] | None:
    """Pick [start, end) of messages to summarize. The kept tail never starts with a tool result, and always
    includes the newest turn: summarizing results the model hasn't read yet would make the summarizer do the
    work (badly) instead of the agent."""
    start = _head_len(messages)
    end = last_turn_start(messages)
    kept = sum(message_chars(m) for m in messages[end:])
    while end > start:
        kept += message_chars(messages[end - 1])
        if kept > keep_chars:
            break
        end -= 1
    # Never keep an orphaned tool result: its assistant call must stay with it.
    while end < len(messages) and messages[end]["role"] == "tool":
        end += 1
    if end - start < 2:
        return None
    return start, end


def _render_assistant(m: dict) -> str:
    text = ""
    if m.get("reasoning_content"):
        # Local reasoning models keep running state (counts, plans) in their reasoning, not their replies.
        reasoning = m["reasoning_content"].strip()
        if len(reasoning) > 3000:
            reasoning = reasoning[:1000] + " ... " + reasoning[-2000:]
        text += f"(thinking: {reasoning})\n"
    text += m.get("content") or ""
    for call in m.get("tool_calls") or []:
        text += f"\n-> {call['function']['name']}({call['function']['arguments'][:1500]})"
    return f"ASSISTANT: {text.strip()}"


def _render_tool(m: dict, tool_limit: int) -> str:
    content = m.get("content") or ""
    if len(content) > tool_limit:
        half = tool_limit // 2
        content = content[:half] + f"\n...[{len(content) - 2 * half} chars omitted]...\n" + content[-half:]
    return f"TOOL RESULT: {content}"


def render_excerpt(messages: list[dict], max_chars: int) -> str:
    parts = []
    # Spread the budget: a few large tool results shouldn't crowd out everything else.
    tool_limit = max(1500, max_chars // max(1, 2 * sum(1 for m in messages if m["role"] == "tool")))
    for m in messages:
        role = m["role"]
        if role == "assistant":
            parts.append(_render_assistant(m))
        elif role == "tool":
            parts.append(_render_tool(m, tool_limit))
        else:
            parts.append(f"{role.upper()}: {m.get('content') or ''}")
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        # Keep the newest material; the oldest is the least likely to matter.
        text = "[earliest part omitted]\n" + text[-max_chars:]
    return text


def summary_request(messages: list[dict], start: int, end: int, max_chars: int) -> list[dict]:
    task = messages[1]["content"] if _head_len(messages) == 2 else "(not available)"
    excerpt = render_excerpt(messages[start:end], max_chars)
    return [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": SUMMARY_PROMPT.format(task=task, excerpt=excerpt)},
    ]


def apply_summary(messages: list[dict], _start: int, end: int, summary: str, notes: str = "") -> list[dict]:
    head = [m for m in messages[:_head_len(messages)] if not (m.get("content") or "").startswith(SUMMARY_TAG)]
    text = f"{SUMMARY_TAG} Earlier work in this session, condensed:\n\n{summary.strip()}"
    if notes.strip():
        text += f"\n\n## Your saved notes (kept verbatim)\n{notes.strip()}"
    note = {"role": "user", "content": text + "\n\nContinue from here; don't redo work that is already done."}
    return head + [note] + messages[end:]
