"""Where consecutive model requests stop sharing a prefix, and what each break costs (#774, docs/prefix-cache-study.md).

    python -m bakeoff.prefix_diff render PROMPTS.jsonl OUT.jsonl --server http://127.0.0.1:8082
    python -m bakeoff.prefix_diff report RENDERED.jsonl [--json OUT]

PROMPTS.jsonl is `python -m bakeoff.canary --capture-prompts` output: the exact messages and tools of every model
call, in the order the server got them. `render` asks a running llama-server for each request's prompt as its chat
template renders it (/apply-template) and its tokens (/tokenize); nothing is generated. `report` is offline: for each
consecutive pair it finds the first differing message and character offset of the payload, the first differing
token of the rendered prompts, classifies the break and prices the tokens the server read again.

llama-server with --parallel 1 keeps one slot, so request N+1 can reuse at most the tokens it shares with request N
plus N's reply. A break at token p costs re-reading from p, or, on a hybrid model whose recurrent state can't be cut
back (Qwen3.6's Gated DeltaNet layers), from the last context checkpoint at or before p.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
from pathlib import Path

from harness import compaction, planning, repomap, workspace_guidance

DATE_TIME = re.compile(r"\d{4}-\d\d-\d\d|\d\d:\d\d")
# compaction's trim and elide notes (harness/compaction.py: SAME_AS_BELOW, SUPERSEDED, LINES_OMITTED, elided output)
TRIM_MARK = re.compile(r"\[Same output as a later|\[Older read of|lines omitted|chars omitted|earliest part omitted|"
                       r"characters of prior notes shortened|older tool output shortened")
MESSAGE_OVERHEAD = 8  # tokens the template adds around one message (role markers, <tool_response> tags)


def _text(message: dict) -> str:
    content = message.get("content")
    return content if isinstance(content, str) else ""


def common_prefix(a, b) -> int:
    """Length of the common prefix of two strings or lists."""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def first_message_diff(prev: list[dict], cur: list[dict]) -> tuple[int, int] | None:
    """(message index, character offset in its JSON) where `cur` first departs from `prev`, or None when `prev` is
    a prefix of `cur` (only appended to)."""
    for i, (a, b) in enumerate(zip(prev, cur, strict=False)):
        sa, sb = json.dumps(a), json.dumps(b)
        if sa != sb:
            return i, common_prefix(sa, sb)
    if len(cur) < len(prev):
        return len(cur), 0
    return None


def classify(prev: dict, cur: dict, where: tuple[int, int] | None) -> str:
    """One of the issue's classes for the break between two captured requests (None: no break)."""
    if prev.get("session") != cur.get("session"):
        return "other session"
    if any((r["messages"] or [{}])[0].get("content") == compaction.SUMMARY_SYSTEM for r in (prev, cur)):
        return "compaction summary request"  # a separate call with its own prompt, before or after it
    if prev.get("tools") != cur.get("tools"):
        names = [[t.get("function", {}).get("name") for t in (r.get("tools") or [])] for r in (prev, cur)]
        return "tool-schema order" if sorted(names[0]) == sorted(names[1]) else "tool schemas changed"
    if where is None:
        return ""
    index, offset = where
    old, new = prev["messages"], cur["messages"]
    texts = [_text(m) for m in new]
    if any(t.startswith(compaction.SUMMARY_TAG) for t in texts[index:]):
        return "compaction: summary"
    # a round reset rebuilds the context as system, task, [Task state], a nudge and the newest turn; a second reset
    # with the same saved state departs only after the nudge
    if any(t.startswith(compaction.STATE_TAG) for t in texts[index:]) or (
            any(t.startswith(compaction.STATE_TAG) for t in texts) and len(new) <= len(old)):
        return "compaction: round reset"
    if index >= len(new):
        return "compaction: other rewrite"  # history shortened from the end
    a = json.dumps(old[index]) if index < len(old) else ""
    b = json.dumps(new[index])
    if index == 0:
        window = a[max(0, offset - 200):offset + 200] + b[max(0, offset - 200):offset + 200]
        if "{base_branch}" in a:
            return "system prompt: {base_branch} filled"
        if repomap.SECTION_OPEN in window or repomap.SECTION_OPEN in b[offset:]:
            return "system prompt: repo map rewritten"
        if workspace_guidance.SECTION_OPEN in window or workspace_guidance.SECTION_OPEN in b[offset:]:
            return "system prompt: workspace guidance rewritten"
        near = slice(max(0, offset - 20), offset + 40)  # the offset can fall inside the date itself
        if DATE_TIME.search(a[near]) or DATE_TIME.search(b[near]):
            return "date or time"
        return "system prompt: other"
    if index < len(old) and old[index].get("reasoning_content") and not new[index].get("reasoning_content"):
        return "compaction: elide dropped reasoning"  # the elide tier strips older assistant turns' reasoning
    if texts[index].startswith(compaction.RECEIPT_PREFIX):
        return "masking: observation receipt"  # a used tool result replaced by its receipt (mask_min_chars)
    if planning.MARKER in a[offset:] and planning.MARKER not in b[offset:]:
        return "injected state: plan re-injection"  # #641: the previous request's appended plan is gone
    if TRIM_MARK.search(b[offset:offset + 300]):
        return "compaction: trim or elide"
    if len(new) < len(old) or len(b) < len(a):
        return "compaction: other rewrite"
    if new[index].get("role") == "assistant":
        return "assistant turn re-serialized"
    if new[index].get("role") in ("user", "system") and index == len(old) - 1:
        return "injected state"  # the previous request's last message (a per-turn note) was not kept
    return "other"


def new_messages(prev: dict, cur: dict) -> list[dict]:
    """The messages `cur` adds after `prev`'s, without the assistant turn that holds `prev`'s reply: the server has
    already generated that one, the rest it has to read whatever the cache holds. A request after a summary call
    shares nothing with it, so nothing in it counts as added."""
    if (prev["messages"] or [{}])[0].get("content") == compaction.SUMMARY_SYSTEM:
        return []
    tail = cur["messages"][len(prev["messages"]):]
    return tail[1:] if tail and tail[0].get("role") == "assistant" else tail


def appended_tokens(prev: dict, cur: dict) -> int:
    """Tokens of new_messages: counted by `render`, or estimated at 3 characters a token for an unrendered capture."""
    if "new_tokens" in cur:
        return cur["new_tokens"]
    return round(len(json.dumps(new_messages(prev, cur))) / 3) if new_messages(prev, cur) else 0


def pairs(rows: list[dict]) -> list[dict]:
    """One row per request after the first: prompt and cache sizes, where it departs from the previous request
    and the break's class. Token fields are filled when the rows carry rendered `tokens`."""
    out = []
    for prev, cur in itertools.pairwise(rows):
        where = first_message_diff(prev["messages"], cur["messages"])
        tools_changed = prev.get("tools") != cur.get("tools")
        row = {"seq": cur["seq"], "session": cur.get("session"), "prompt_tokens": cur.get("prompt_tokens"),
               "cache_tokens": cur.get("cache_tokens"), "prompt_ms": cur.get("prompt_ms"),
               "message": where[0] if where else None, "offset": where[1] if where else None,
               "class": classify(prev, cur, where) if (where or tools_changed
                                                       or prev.get("session") != cur.get("session")) else ""}
        cache = cur.get("cache_tokens") or 0
        # What the server read beyond the messages this request added: re-reading it is the break's cost.
        row["lost_tokens"] = max(0, (cur.get("prompt_tokens") or 0) - cache - appended_tokens(prev, cur))
        if "tokens" in prev and "tokens" in cur:
            shared = common_prefix(prev["tokens"], cur["tokens"])
            row.update(shared_tokens=shared, broke=shared < len(prev["tokens"]),
                       server_shortfall=max(0, shared - cache))  # matched, yet not reused: checkpoint spacing
        out.append(row)
    return out


def prefill_rate(rows: list[dict]) -> float:
    """Milliseconds per prompt token actually processed, over the requests that processed at least 256 tokens
    (the first request of a server run is left out: it includes warm-up)."""
    ms = tokens = 0.0
    for r in rows[1:]:
        done = (r.get("prompt_tokens") or 0) - (r.get("cache_tokens") or 0)
        if done >= 256 and r.get("prompt_ms"):
            ms += r["prompt_ms"]
            tokens += done
    return ms / tokens if tokens else 0.0


def summary(rows: list[dict]) -> dict:
    """Per-session reuse (cached / prompt tokens over its requests) and the cost of each break class."""
    rate = prefill_rate(rows)
    sessions: dict[str, dict] = {}
    for r in rows:
        s = sessions.setdefault(r.get("session") or "?", {"requests": 0, "prompt_tokens": 0, "cache_tokens": 0})
        s["requests"] += 1
        s["prompt_tokens"] += r.get("prompt_tokens") or 0
        s["cache_tokens"] += r.get("cache_tokens") or 0
    for s in sessions.values():
        s["reuse"] = round(s["cache_tokens"] / s["prompt_tokens"], 3) if s["prompt_tokens"] else None
    breaks: dict[str, dict] = {}
    for p in pairs(rows):
        if not p["class"] or p["class"] == "other session":
            continue
        b = breaks.setdefault(p["class"], {"count": 0, "lost_tokens": 0})
        b["count"] += 1
        b["lost_tokens"] += p.get("lost_tokens", 0)
    for b in breaks.values():
        b["seconds"] = round(b["lost_tokens"] * rate / 1000, 1)
    return {"ms_per_token": round(rate, 3), "sessions": sessions, "breaks": breaks}


def render(rows: list[dict], server: str) -> list[dict]:
    """`rows` with `prompt` and `tokens` as the server's chat template and tokenizer make them."""
    import httpx
    out: list[dict] = []
    with httpx.Client(base_url=server, timeout=120) as client:
        def tokenize(text: str) -> list[int]:
            return client.post("/tokenize", json={"content": text, "add_special": False,
                                                  "parse_special": True}).raise_for_status().json()["tokens"]

        for r in rows:
            body = {"messages": r["messages"], **({"tools": r["tools"]} if r.get("tools") else {})}
            prompt = client.post("/apply-template", json=body).raise_for_status().json()["prompt"]
            row = {**r, "prompt": prompt, "tokens": tokenize(prompt)}
            if out:
                row["new_tokens"] = sum(len(tokenize(_text(m))) + MESSAGE_OVERHEAD for m in new_messages(out[-1], r))
            out.append(row)
    return out


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bakeoff.prefix_diff")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("prompts", type=Path)
    r.add_argument("out", type=Path)
    r.add_argument("--server", default="http://127.0.0.1:8082")
    rep = sub.add_parser("report")
    rep.add_argument("rendered", type=Path)
    rep.add_argument("--json", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "render":
        rows = render(read_jsonl(a.prompts), a.server)
        a.out.write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        print(f"{len(rows)} requests rendered into {a.out}")
        return 0
    rows = read_jsonl(a.rendered)
    result = {"pairs": pairs(rows), **summary(rows)}
    if a.json:
        a.json.write_text(json.dumps(result, indent=1), encoding="utf-8")
    for p in result["pairs"]:
        print(" ".join(f"{k}={p[k]}" for k in ("seq", "prompt_tokens", "cache_tokens", "shared_tokens",
                                                 "lost_tokens", "message", "offset", "class") if k in p))
    print(json.dumps({k: result[k] for k in ("ms_per_token", "sessions", "breaks")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
