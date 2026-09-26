"""Line comments on the Changes diff, sent back to the agent as one follow-up message.

The diff is parsed into per-line records (side, line number, text) so a comment anchors to a file, a side
(old = deleted/context lines by their old number, new = added/context lines by their new number) and an inclusive
line range. Each comment quotes its lines and remembers the base/head commit it was written against; at send time
a comment whose quoted lines no longer match the current diff is marked stale and the current text is quoted.
Idea credit: Orca's diff line comments (stablyai/orca); reimplemented, no code copied.
"""

from __future__ import annotations

import re

HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
MAX_COMMENT_CHARS = 4000
MAX_QUOTED_LINES = 200
SIDES = ("old", "new")


def _path(marker: str, line: str) -> str | None:
    rest = line[len(marker):].split("\t", 1)[0].strip()
    if rest == "/dev/null":
        return None
    return rest[2:] if rest[:2] in ("a/", "b/") else rest


def _hunk_line(cur: dict, raw: str, old: int, new: int) -> tuple[int, int]:
    """Record one add/del/context line of a hunk body; returns the advanced (old, new) line numbers."""
    if raw.startswith("+"):
        cur["lines"].append({"kind": "add", "old": None, "new": new, "text": raw[1:]})
        return old, new + 1
    if raw.startswith("-"):
        cur["lines"].append({"kind": "del", "old": old, "new": None, "text": raw[1:]})
        return old + 1, new
    if raw.startswith(" "):
        cur["lines"].append({"kind": "ctx", "old": old, "new": new, "text": raw[1:]})
        return old + 1, new + 1
    return old, new


def _open_hunk(cur: dict, raw: str, old: int, new: int) -> tuple[bool, int, int]:
    """Record a `@@` header line; returns (in_hunk, old, new) with the counters reset when it parses."""
    m = HUNK.match(raw)
    if m:
        old, new = int(m.group(1)), int(m.group(2))
    cur["lines"].append({"kind": "hunk", "old": None, "new": None, "text": raw})
    return bool(m), old, new


def parse_diff(diff: str) -> list[dict]:
    """Unified diff -> [{name, lines: [{kind, old, new, text}]}]. kind: hunk | add | del | ctx.

    `old`/`new` are the 1-based line numbers on each side (None where the line isn't on that side)."""
    files: list[dict] = []
    cur: dict | None = None
    old = new = 0
    in_hunk = False
    for raw in (diff or "").split("\n"):
        if raw.startswith("diff --git "):
            m = re.search(r" b/(.+)$", raw)
            cur = {"name": m.group(1) if m else raw, "lines": []}
            files.append(cur)
            in_hunk = False
        elif cur is None:
            continue
        elif not in_hunk and raw.startswith("+++ "):
            name = _path("+++ ", raw)
            if name:
                cur["name"] = name
        elif raw.startswith("@@"):
            in_hunk, old, new = _open_hunk(cur, raw, old, new)
        elif in_hunk:
            old, new = _hunk_line(cur, raw, old, new)
        # "\ No newline at end of file", headers, and the trailing blank line carry no content
    return files


def side_lines(files: list[dict], path: str, side: str) -> dict[int, str]:
    """{line number: text} for one side of one file in a parsed diff."""
    key = "old" if side == "old" else "new"
    out: dict[int, str] = {}
    for f in files:
        if f["name"] == path:
            for ln in f["lines"]:
                if ln[key] is not None:
                    out[ln[key]] = ln["text"]
    return out


def _validate_range(body: dict) -> tuple[int, int]:
    try:
        start, end = int(body["start_line"]), int(body.get("end_line") or body["start_line"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("start_line and end_line must be numbers") from None
    if start < 1 or end < start:
        raise ValueError("line range must satisfy 1 <= start_line <= end_line")
    if end - start + 1 > MAX_QUOTED_LINES:
        raise ValueError(f"a comment can span at most {MAX_QUOTED_LINES} lines")
    return start, end


def validate(body: dict) -> dict:
    """Normalise one incoming comment; raises ValueError with a user-facing message."""
    side = body.get("side")
    if side not in SIDES:
        raise ValueError("side must be 'old' or 'new'")
    path = str(body.get("path") or "").strip()
    if not path:
        raise ValueError("path is required")
    start, end = _validate_range(body)
    comment = str(body.get("comment") or "").strip()
    if not comment:
        raise ValueError("comment is empty")
    if len(comment) > MAX_COMMENT_CHARS:
        raise ValueError(f"comment is over {MAX_COMMENT_CHARS} characters")
    quoted = body.get("quoted")
    if not isinstance(quoted, list) or len(quoted) != end - start + 1 or not all(isinstance(q, str) for q in quoted):
        raise ValueError("quoted must list the text of each commented line")
    return {"repo": str(body.get("repo") or "."), "path": path, "side": side, "start_line": start,
            "end_line": end, "quoted": quoted, "comment": comment,
            "base": str(body.get("base") or "")[:40], "head": str(body.get("head") or "")[:40]}


def current_quote(comment: dict, repos: list[dict]) -> tuple[bool, list[str]]:
    """(stale, current quoted lines). Stale when any commented line's text differs from the current diff or is
    no longer in it. When stale the current text is returned for the lines still present."""
    repo = next((r for r in repos if r["path"] == comment["repo"]), None)
    lines = side_lines(parse_diff(repo["diff"]), comment["path"], comment["side"]) if repo else {}
    numbers = range(comment["start_line"], comment["end_line"] + 1)
    now = [lines.get(n) for n in numbers]
    stale = now != list(comment["quoted"])
    return stale, [t for t in now if t is not None]


def _range(c: dict) -> str:
    return f"{c['start_line']}" if c["start_line"] == c["end_line"] else f"{c['start_line']}-{c['end_line']}"


def format_message(comments: list[dict], repos: list[dict]) -> str:
    """One structured follow-up covering every draft comment."""
    out = [f"Review comments on your changes ({len(comments)}). Address each one:"]
    for i, c in enumerate(comments, 1):
        stale, now = current_quote(c, repos)
        where = "in the removed (old) lines" if c["side"] == "old" else "in the new lines"
        repo = "" if c["repo"] == "." else f"{c['repo']}/"
        out += ["", f"{i}. {repo}{c['path']}, line {_range(c)} {where}"]
        if stale:
            out.append("   STALE: this file changed after the comment was written; the lines it pointed at "
                       "are no longer at that position. Current text of those lines:"
                       if now else
                       "   STALE: this file changed after the comment was written and those lines are no longer "
                       "in the diff. The text the comment was written against:")
        out += [f"   > {t}" for t in (now if stale and now else c["quoted"])]
        out.append(f"   Comment: {c['comment']}")
    return "\n".join(out)
