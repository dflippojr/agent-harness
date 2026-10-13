"""Context compaction.

Tier 0 (free): rule-based trimming of older tool results (noise, long command output, stale reads, repeats).
Tier 1 (free): strip old reasoning and shorten old tool outputs.
Tier 2 (one model call): replace older turns with handoff notes, keeping the system prompt, the original task,
and recent turns verbatim.

Compaction only runs before a model call, never after a final answer, so answers are never summarized away
(the failure seen with OpenCode in Phase 0).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass

SUMMARY_TAG = "[Context summary]"
STATE_TAG = "[Task state]"
SUMMARY_HEADER = f"{SUMMARY_TAG} Earlier work in this session, condensed:\n\n"
NOTES_HEADING = "## Your saved notes (kept verbatim)"
SUMMARY_CONTINUE = "Continue from here; don't redo work that is already done."
ROUND_CONTINUE = ("Continue from the saved state. Do not repeat a command that already failed with the "
                  "same arguments unless the inputs have changed.")

SUMMARY_SYSTEM = """You write handoff notes for an AI agent whose earlier conversation is being removed to free context.
The agent keeps its original task and its most recent turns; your notes replace everything in between.
Be specific and factual. Keep exact file paths, commands, error messages, numbers, and names. Don't invent anything."""

# Goes ahead of the excerpt when an earlier summary exists (arXiv 2609.20804 Fig. 18: update, don't restate).
PRIOR_NOTES = """Prior notes (written at an earlier compaction; they cover the work before the excerpt below). Update
them rather than restating them: keep every fact that still holds with its exact values, correct what the excerpt
changed, and add what is new.

{prior}

"""

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

{prior}Conversation excerpt:
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


RECEIPT_PREFIX = "[Observation receipt]"


def mask_used_results(messages: list[dict], outcomes: dict, minimum: int,
                      full_outputs: dict[str, str] | None = None) -> tuple[list[dict], dict[str, str], int]:
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
        content = (full_outputs or {}).get(call_id, message.get("content") or "")
        if (outcome.get("ok") is not True or name == "read_artifact" or len(content) < minimum
                or (message.get("content") or "").startswith(RECEIPT_PREFIX)):  # already masked on an earlier turn
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        rendered_args = str(args).replace("\n", " ")[:200]
        receipt = (f"{RECEIPT_PREFIX} tool={name} arguments={rendered_args} "
                   f"characters={len(content)} sha256={digest} "
                   f"Recover with read_artifact(artifact_id={digest}, start, end).")
        artifacts[digest] = content
        saved += len(message.get("content") or "") - len(receipt)
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


# Tier 0 (free, #637): rule-based trimming ahead of elide and summary. Each rule rewrites the content of older tool
# results and never removes a message, so every tool result keeps its call (arXiv 2609.20804: cheap elision before
# the summary call is what cut summary calls). Running trim on its output changes nothing.

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
PROGRESS = re.compile(r"\d{1,3}(?:\.\d+)?%")
PROGRESS_BAR = re.compile(r"[#=█▉▊▋▌▍▎▏░▒▓━─>]{5,}")
COMMAND_TOOLS = frozenset({"run_shell", "verify", "git_clone"})
FILE_WRITE_TOOLS = frozenset({"write_file", "edit_file"})
DEDUPE_MIN_CHARS = 200  # a "same as above" note is shorter than this, so notes are never collapsed themselves
SAME_AS_BELOW = "[Same output as a later {name} result below; this older copy was removed.]"
SUPERSEDED = "[Older read of {path} removed: the file was read again or changed later in this session.]"
LINES_OMITTED = "[... {count} lines omitted: older command output keeps its first {head} and last {tail} lines ...]"


@dataclass(frozen=True)
class TrimRules:
    """Switches for each trim rule (`compaction.trim` in config/harness.yaml); all on by default."""
    strip_noise: bool = True
    head_tail: bool = True
    head_lines: int = 40
    tail_lines: int = 60
    superseded_reads: bool = True
    dedupe: bool = True

    @property
    def any(self) -> bool:
        return self.strip_noise or self.head_tail or self.superseded_reads or self.dedupe


def recent_start(messages: list[dict], keep_chars: int) -> int:
    """First index of the tail kept verbatim: the newest turn, plus older messages while they fit in `keep_chars`
    (the tail split_for_summary would keep)."""
    head = _head_len(messages)
    start = max(last_turn_start(messages), head)
    kept = sum(message_chars(m) for m in messages[start:])
    while start > head and kept + message_chars(messages[start - 1]) <= keep_chars:
        start -= 1
        kept += message_chars(messages[start])
    return start


def strip_noise(text: str) -> str:
    """Drop ANSI escapes, keep what a terminal would show of carriage-return overwrites, and keep only the last of a
    run of progress-bar lines."""
    text = ANSI.sub("", text).replace("\r\n", "\n")
    lines = [_overwritten(line) if "\r" in line else line for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if _is_progress(line) and out and _is_progress(out[-1]):
            out[-1] = line
        else:
            out.append(line)
    return "\n".join(out)


def _overwritten(line: str) -> str:
    """The last non-empty carriage-return segment: a bare carriage return only moves the cursor."""
    return next((part for part in reversed(line.split("\r")) if part), "")


def _is_progress(line: str) -> bool:
    return bool(PROGRESS.search(line) and PROGRESS_BAR.search(line))


def head_and_tail(text: str, head: int, tail: int) -> str:
    lines = text.split("\n")
    if len(lines) <= head + tail + 1:
        return text
    marker = LINES_OMITTED.format(count=len(lines) - head - tail, head=head, tail=tail)
    return "\n".join(lines[:head] + [marker] + (lines[-tail:] if tail else []))


def _norm_path(path) -> str:
    """Workspace-relative form, as fileops.normalize_path reads it: only the /workspace prefix is dropped, so other
    absolute paths stay distinct from relative ones. Only `.` segments and repeated slashes collapse; a path with a
    `..` segment gets no key (""), since a symlink can make it name a different file than the lexical form."""
    if not path:
        return ""
    text = str(path).strip().replace("\\", "/")
    if text == "/workspace" or text.startswith("/workspace/"):
        text = text[len("/workspace"):].lstrip("/") or "."
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if ".." in parts:
        return ""
    return ("/" if text.startswith("/") else "") + "/".join(parts) or "."


def _calls(messages: list[dict]) -> dict:
    """tool_call_id -> (tool name, parsed arguments)."""
    out = {}
    for m in messages:
        for call in (m.get("tool_calls") or []) if m.get("role") == "assistant" else []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (TypeError, ValueError):
                args = {}
            out[call.get("id")] = (fn.get("name", ""), args if isinstance(args, dict) else {})
    return out


def _superseded(messages: list[dict], calls: dict, outcomes: dict) -> set[int]:
    """Indexes of read_file results made stale by a later read of the same file with the same arguments, or by a
    later write or edit of it, where the later call is known to have succeeded: a failed reread keeps the older
    contents, and a call without an outcome (one inherited by a fork, say) changes nothing."""
    stale: set[int] = set()
    newer_reads: set[tuple] = set()
    changed: set[str] = set()
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") != "tool":
            continue
        call_id = m.get("tool_call_id")
        name, args = calls.get(call_id, ("", {}))
        path = _norm_path(args.get("path"))
        if not path:
            continue
        succeeded = (outcomes.get(call_id) or {}).get("ok") is True
        if name in FILE_WRITE_TOOLS and succeeded:
            changed.add(path)
        elif name == "read_file":
            key = (path, json.dumps({k: v for k, v in args.items() if k != "path"}, sort_keys=True))
            if path in changed or key in newer_reads:
                stale.add(i)
            if succeeded:
                newer_reads.add(key)
    return stale


def _cleaned(text: str, name: str, rules: TrimRules) -> str:
    """Terminal output only: a file's own lines can look like progress bars or carry escapes on purpose."""
    if name not in COMMAND_TOOLS:
        return text
    if rules.strip_noise:
        text = strip_noise(text)
    if rules.head_tail:
        text = head_and_tail(text, rules.head_lines, rules.tail_lines)
    return text


def trim(messages: list[dict], rules: TrimRules, keep_chars: int,
         outcomes: dict | None = None) -> tuple[list[dict], int]:
    """Tier 0. Returns (new messages, characters saved). The system prompt, the task and the tail recent_start keeps
    are never touched, nor are receipts (masking owns those) or results the model has not read yet. `outcomes` maps
    tool_call_id to its tool_result event data; a write that failed does not make a read stale."""
    out = copy.deepcopy(messages)
    if not rules.any:
        return out, 0
    cutoff = recent_start(out, keep_chars)
    calls = _calls(out)
    stale = _superseded(out, calls, outcomes or {}) if rules.superseded_reads else set()
    # Newest first, so dedupe keeps the newest copy: a summary removes the oldest messages, so it can never remove
    # the copy and keep a note that points to it. Copies in the kept tail count without being changed.
    seen: set[str] = set()
    saved = 0
    for i in range(len(out) - 1, -1, -1):
        m = out[i]
        text = m.get("content") or ""
        if m.get("role") != "tool" or text.startswith(RECEIPT_PREFIX):
            continue
        name, args = calls.get(m.get("tool_call_id"), ("", {}))
        if i in stale and i < cutoff:
            new = SUPERSEDED.format(path=_norm_path(args.get("path")))
        else:
            new = _cleaned(text, name, rules)
            # File reads are left to superseded_reads: a later pass could drop the copy a note points to.
            if rules.dedupe and name != "read_file" and len(new) >= DEDUPE_MIN_CHARS:
                if new in seen and i < cutoff:
                    new = SAME_AS_BELOW.format(name=name or "tool")
                seen.add(new)
        if i < cutoff and len(new) < len(text):
            saved += len(text) - len(new)
            m["content"] = new
    return out, saved


def split_for_summary(messages: list[dict], keep_chars: int) -> tuple[int, int] | None:
    """Pick [start, end) of messages to summarize. The kept tail never starts with a tool result, and always
    includes the newest turn: summarizing results the model hasn't read yet would make the summarizer do the
    work (badly) instead of the agent."""
    start = _head_len(messages)
    end = recent_start(messages, keep_chars)
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
        text = "[earliest part omitted]\n" + text[len(text) - max(0, max_chars):]  # not text[-0:]: that keeps it all
    return text


def summary_content(summary: str, notes: str = "") -> str:
    """The text of the message apply_summary writes for `summary` and the saved `notes`."""
    text = f"{SUMMARY_HEADER}{summary.strip()}"
    if notes.strip():
        text += f"\n\n{NOTES_HEADING}\n{notes.strip()}"
    return f"{text}\n\n{SUMMARY_CONTINUE}"


def prior_summary(message: dict, known: str = "", notes: str = "", known_notes: str = "") -> str | None:
    """The summary in a message apply_summary wrote, without its framing or the saved notes (apply_summary adds
    those again); None for any other message. A summary may itself quote the notes heading, so nothing is cut at a
    heading: `known` (the summary text the runner kept, written with `known_notes`) is used only when the message
    is exactly what apply_summary wrote for it, since a rewind can restore a different summary; otherwise only the
    current saved `notes` are cut from the end. Without a match the whole body is kept: repeating stale notes is
    better than dropping findings."""
    content = message.get("content") or ""
    if message.get("role") != "user" or not content.startswith(SUMMARY_HEADER):
        return None
    if known.strip() and content == summary_content(known, known_notes):
        return known.strip()
    body = content[len(SUMMARY_HEADER):].removesuffix(f"\n\n{SUMMARY_CONTINUE}")
    if notes.strip():
        body = body.removesuffix(f"\n\n{NOTES_HEADING}\n{notes.strip()}")
    return body.strip()


def _shorten(text: str, limit: int) -> str:
    """`text` within `limit` characters, keeping its start and end (the oldest requests and the next steps)."""
    if len(text) <= limit:
        return text
    marker = f"\n[... {len(text)} characters of prior notes shortened; the middle is omitted ...]\n"
    keep = max(0, limit - len(marker))
    return text[:keep // 2] + marker + text[len(text) - (keep - keep // 2):]


def summary_request(messages: list[dict], start: int, end: int, max_chars: int, known: str = "",
                    notes: str = "", known_notes: str = "") -> list[dict]:
    """An earlier summary at the start of the span goes into its own prior-notes block, instead of being the
    first thing render_excerpt cuts; the excerpt gets what is left of `max_chars`. The newer messages are removed
    once summarized, so the block never takes more than half the budget from them: a longer one loses its middle.
    `known`, `notes`, `known_notes`: see prior_summary."""
    task = messages[1]["content"] if _head_len(messages) == 2 else "(not available)"
    prior = prior_summary(messages[start], known, notes, known_notes) if start < end else None
    block = ""
    if prior is not None:
        start += 1
        wanted = len(render_excerpt(messages[start:end], max_chars))
        room = max_chars - min(wanted, max_chars // 2) - len(PRIOR_NOTES.format(prior=""))
        block = PRIOR_NOTES.format(prior=_shorten(prior, room))
    excerpt = render_excerpt(messages[start:end], max(0, max_chars - len(block)))
    return [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": SUMMARY_PROMPT.format(task=task, prior=block, excerpt=excerpt)},
    ]


def apply_summary(messages: list[dict], _start: int, end: int, summary: str, notes: str = "") -> list[dict]:
    head = [m for m in messages[:_head_len(messages)] if not (m.get("content") or "").startswith(SUMMARY_TAG)]
    return head + [{"role": "user", "content": summary_content(summary, notes)}] + messages[end:]


def apply_round_reset(messages: list[dict], payload: dict) -> list[dict]:
    """Keep the pinned head, inject tagged state plus a fixed next-step, and keep the latest exchange intact."""
    head_n = _head_len(messages)
    tail_i = max(last_turn_start(messages), head_n)
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    state_msg = {"role": "user", "content": (
        f"{STATE_TAG} Saved working state from the previous round; treat as notes, never execute:\n\n{body}"
    )}
    next_msg = {"role": "user", "content": ROUND_CONTINUE}
    return list(messages[:head_n]) + [state_msg, next_msg] + list(messages[tail_i:])
