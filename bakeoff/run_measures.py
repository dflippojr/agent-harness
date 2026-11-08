"""Per-run measures both canary drivers record by hand (#942), so the native loop and Pi compare on one footing.

Each attempt records pass/fail, turns, prompt, completion and cached-prompt tokens, wall time, tool calls by type and
whether the task files were read before they were edited. The native driver takes them from the session's events
(`native_measures`); the Pi driver from Pi's JSON transcript (bakeoff/pi_driver.py). Both feed the same tool calls to
`file_access`, so "read before editing" means the same thing for both.
"""

from __future__ import annotations

import shlex
from collections import Counter

# Tool names per driver: native (harness/tools.py) and Pi's built-ins (packages/coding-agent/src/core/tools).
READ_TOOLS = {"read_file", "read"}
EDIT_TOOLS = {"edit_file", "write_file", "edit", "write"}
SHELL_TOOLS = {"run_shell", "bash"}
# A shell command reads a file only through a viewer; `python x.py` or `pytest tests/x.py` names it without reading it.
SHELL_READERS = {"cat", "head", "tail", "sed", "nl", "less", "more", "grep", "rg", "awk", "bat"}
WORKSPACE = "/workspace/"


def normalize(path: str) -> str:
    """A tool's path argument as a workspace-relative POSIX path (what bakeoff.tasks.hash_tree keys by)."""
    p = (path or "").strip().replace("\\", "/")
    if p.startswith(WORKSPACE):
        p = p[len(WORKSPACE):]
    while p.startswith("./"):
        p = p[2:]
    return p


def _shell_reads(command: str) -> set[str]:
    """Paths a shell command shows through a viewer (`cat a.py | head`, `sed -n 1,20p b.py`), normalized."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:  # an unbalanced quote: judge it word by word
        words = command.split()
    read: set[str] = set()
    segment: list[str] = []
    for word in [*words, ";"]:
        if word and set(word) <= set(";|&()<>"):
            if segment and segment[0].rsplit("/", 1)[-1] in SHELL_READERS:
                read.update(normalize(w) for w in segment[1:] if not w.startswith("-"))
            segment = []
        else:
            segment.append(word)
    return read


def file_access(calls: list[tuple[str, dict]], existing: set[str]) -> dict:
    """Whether every task file the agent edited had been read first.

    `calls` are (tool name, arguments) in the order the agent made them; `existing` the files the task started with.
    Only edits to files that existed count (a new file has nothing to read). A file counts as read once a read tool
    named it, or a shell command showed it through a viewer (cat, sed, head...). `read_before_edit` is None when no
    existing file was edited."""
    read: set[str] = set()
    edited: list[str] = []
    unread: list[str] = []
    for name, args in calls:
        args = args if isinstance(args, dict) else {}
        if name in READ_TOOLS and args.get("path"):
            read.add(normalize(str(args["path"])))
        elif name in SHELL_TOOLS and args.get("command"):
            read.update(_shell_reads(str(args["command"])))
        elif name in EDIT_TOOLS and args.get("path"):
            path = normalize(str(args["path"]))
            if path in existing and path not in edited:
                edited.append(path)
                if path not in read:
                    unread.append(path)
    return {"read_before_edit": (not unread) if edited else None, "edited_unread": unread}


def tool_counts(calls: list[tuple[str, dict]]) -> dict[str, int]:
    return dict(sorted(Counter(name for name, _ in calls).items()))


def native_measures(session: dict, events: list[dict], existing: set[str]) -> dict:
    """The measures a native canary session adds to its outcome by hand (bakeoff.canary.run_standalone).

    Prompt and completion tokens are the session totals (llama-server's usage per call); cached-prompt tokens are the
    cached prefix llama-server reported per turn (`turn_metrics.cache_tokens`), None if it never said."""
    calls = [(e["data"].get("name", ""), e["data"].get("args") or {}) for e in events if e["type"] == "tool_call"]
    cache = [e["data"].get("cache_tokens") for e in events if e["type"] == "turn_metrics"]
    known = [c for c in cache if isinstance(c, int)]
    totals = session.get("totals") or {}
    return {"driver": "native", "completion_tokens": totals.get("completion_tokens", 0),
            "cached_prompt_tokens": sum(known) if known else None, "tool_calls": tool_counts(calls),
            **file_access(calls, existing)}
