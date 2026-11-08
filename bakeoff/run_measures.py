"""Per-run measures both canary drivers record by hand (#942), so the native loop and Pi compare on one footing.

Each attempt records pass/fail, turns, prompt, completion and cached-prompt tokens, wall time, tool calls by type and
whether the task files were read before they were edited. The native driver takes them from the session's events
(`native_measures`); the Pi driver from Pi's JSON transcript (bakeoff/pi_driver.py). Both feed the same tool calls to
`file_access`, so "read before editing" means the same thing for both.
"""

from __future__ import annotations

import posixpath
import shlex
from collections import Counter

# Tool names per driver: native (harness/tools.py) and Pi's built-ins (packages/coding-agent/src/core/tools).
READ_TOOLS = {"read_file", "read"}
EDIT_TOOLS = {"edit_file", "write_file", "edit", "write"}
SHELL_TOOLS = {"run_shell", "bash"}
# A shell command reads a file only through a viewer; `python x.py` or `pytest tests/x.py` names it without reading it.
SHELL_READERS = {"cat", "head", "tail", "sed", "nl", "less", "more", "grep", "rg", "awk", "bat"}
WORKSPACE = "/workspace/"
HOME = "/~"  # stands for "somewhere outside the workspace" after `cd` alone


def normalize(path: str, cwd: str = "") -> str:
    """A path as the workspace-relative POSIX path bakeoff.tasks.hash_tree keys by. A relative path resolves against
    `cwd` (workspace-relative, "" for the workspace root); a path outside the workspace stays absolute."""
    p = (path or "").strip().replace("\\", "/")
    if p.startswith("/"):
        if not (p + "/").startswith(WORKSPACE):
            return posixpath.normpath(p)
        p = p[len(WORKSPACE):]
    else:
        p = posixpath.join(cwd, p)
    p = posixpath.normpath(p) if p else "."
    return "" if p == "." else p


def _words(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:  # an unbalanced quote: judge it word by word
        return command.split()


def _in_place(args: list[str]) -> bool:
    """`sed -i`, `sed -i.bak`, `sed --in-place`, `perl -pi`: short flags that include i, or --in-place."""
    return any(a.split("=", 1)[0] == "--in-place" or (a[:1] == "-" and a[1:2] != "-" and "i" in a[1:])
               for a in args)


def _is_operator(word: str) -> bool:
    return bool(word) and set(word) <= set(";|&()<>")


def shell_access(command: str) -> list[tuple[str, str]]:
    """What a shell command shows and writes, in order: ("read" | "write", workspace-relative path).

    Shown: through a viewer (`cat a.py | head`, `sed -n 1,20p b.py`) or an input redirect (`< c.py`). Written: an
    output redirect (`>`, `>>`), `tee`, `sed -i` / `perl -pi` (which edit without showing), or the target of `cp`,
    `mv` or `truncate`. Running a file (`python x.py`, `pytest tests/x.py`) neither shows nor writes it. Simple
    commands count in order; within one, its redirects come first (the shell opens them before it runs the command),
    then what the command reads, then what it writes. `cd` moves where later relative paths resolve; the command
    starts in the workspace."""
    events: list[tuple[str, str]] = []
    cwd = ""
    segment: list[str] = []
    redirects: list[tuple[str, str]] = []
    words = [*_words(command), ";"]
    i = 0
    while i < len(words):
        word = words[i]
        if word in (">", ">>", ">|", "<") and i + 1 < len(words):
            target = words[i + 1]
            if not _is_operator(target) and target != "/dev/null":
                redirects.append(("read" if word == "<" else "write", target))
            i += 2
            continue
        if _is_operator(word):  # end of a simple command (`>&` and friends end up here too)
            events += [(kind, normalize(t, cwd)) for kind, t in redirects]
            cwd = _segment_access(segment, cwd, events)
            segment, redirects = [], []
        else:
            segment.append(word)
        i += 1
    return events


def _segment_access(segment: list[str], cwd: str, events: list[tuple[str, str]]) -> str:
    """Append one simple command's own reads and writes; return the working directory after it."""
    if not segment:
        return cwd
    cmd, args = segment[0].rsplit("/", 1)[-1], segment[1:]
    files = [normalize(a, cwd) for a in args if not a.startswith("-")]
    if cmd == "cd":  # a directory outside the workspace stays absolute; `cd` alone (or `cd ~`, `cd -`) leaves it
        return files[0] if files and not files[0].startswith(("~", "-")) else HOME
    if cmd in ("sed", "perl") and _in_place(args):
        events += [("write", f) for f in files]
    elif cmd in SHELL_READERS:
        events += [("read", f) for f in files]
    elif cmd in ("tee", "truncate"):
        events += [("write", f) for f in files]
    elif cmd in ("cp", "mv") and len(files) >= 2:
        events.append(("write", files[-1]))
    return cwd


def file_access(calls: list[tuple[str, dict]], existing: set[str]) -> dict:
    """Whether every task file the agent edited had been read first.

    `calls` are (tool name, arguments) in the order the agent made them; `existing` the files the task started with.
    Only edits to files that existed count (a new file has nothing to read). An edit is an edit or write tool call, or
    a shell command that writes the file; a file counts as read once a read tool named it, or a shell command showed
    it, in the order `shell_access` gives. `read_before_edit` is None when no existing file was edited."""
    read: set[str] = set()
    edited: list[str] = []
    unread: list[str] = []

    def edit(path: str) -> None:
        if path in existing and path not in edited:
            edited.append(path)
            if path not in read:
                unread.append(path)

    for name, args in calls:
        args = args if isinstance(args, dict) else {}
        if name in READ_TOOLS and args.get("path"):
            read.add(normalize(str(args["path"])))
        elif name in SHELL_TOOLS and args.get("command"):
            for kind, path in shell_access(str(args["command"])):
                if kind == "read":
                    read.add(path)
                else:
                    edit(path)
        elif name in EDIT_TOOLS and args.get("path"):
            edit(normalize(str(args["path"])))
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
