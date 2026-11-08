"""Per-run measures both canary drivers record by hand (#942), so the native loop and Pi compare on one footing.

Each attempt records pass/fail, turns, prompt, completion and cached-prompt tokens, wall time, tool calls by type and
whether the task files were read before they were edited. The native driver takes them from the session's events
(`native_measures`); the Pi driver from Pi's JSON transcript (bakeoff/pi_driver.py). Both feed the same tool calls to
`file_access`, so "read before editing" means the same thing for both.
"""

from __future__ import annotations

import posixpath
import re
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


HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def _without_heredoc_bodies(command: str) -> str:
    """The command with each here-document's body dropped (its text is data, not commands)."""
    out: list[str] = []
    ends: list[tuple[str, bool]] = []
    for line in command.split("\n"):
        if ends:
            delim, tabs = ends[0]
            if (line.lstrip("\t") if tabs else line) == delim:
                ends.pop(0)
            continue
        out.append(line)
        ends = [(m.group(3), m.group(1) == "-") for m in HEREDOC.finditer(line)]
    return "\n".join(out)


def _without_comments(command: str) -> str:
    """The command with unquoted `# ...` comments removed up to, not including, the newline that ends them."""
    out: list[str] = []
    quote, i = "", 0
    while i < len(command):
        c = command[i]
        if quote:
            if c == quote:
                quote = ""
            elif c == "\\" and quote == '"' and i + 1 < len(command):
                out.append(c)
                i += 1
                c = command[i]
        elif c in "'\"":
            quote = c
        elif c == "\\" and i + 1 < len(command):
            out.append(c)
            i += 1
            c = command[i]
        elif c == "#" and (not out or out[-1] in " \t\n;&|()"):
            while i < len(command) and command[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _words(command: str) -> list[str]:
    """Shell words and operators; an unquoted newline is an operator (it ends a command, like `;`)."""
    lexer = shlex.shlex(_without_comments(_without_heredoc_bodies(command)), posix=True,
                        punctuation_chars=";&|()<>\n")
    lexer.commenters = ""
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    try:
        return list(lexer)
    except ValueError:  # an unbalanced quote: judge it word by word
        return command.replace("\n", " ; ").split()


def _in_place(args: list[str]) -> bool:
    """`sed -i`, `sed -i.bak`, `sed --in-place`, `perl -pi`: short flags that include i, or --in-place."""
    return any(a.split("=", 1)[0] == "--in-place" or (a[:1] == "-" and a[1:2] != "-" and "i" in a[1:])
               for a in args)


def _is_operator(word: str) -> bool:
    return bool(word) and set(word) <= set(";|&()<>\n")


def shell_access(command: str) -> list[tuple[str, str]]:
    """What a shell command shows and writes, in order: ("read" | "write", workspace-relative path).

    Shown: through a viewer (`cat a.py | head`, `sed -n 1,20p b.py`) or an input redirect (`< c.py`). Written: an
    output redirect (`>`, `>>`), `tee`, `sed -i` / `perl -pi` (which edit without showing), the target of `cp` or
    `mv` (a file, or a directory the sources land in under their own names), or `truncate`. Only file operands
    count: a viewer's pattern or script (`grep calc.py README.md`, `sed -n 1p x`) and option values are not files.
    Running a file (`python x.py`, `pytest tests/x.py`) neither shows nor writes it. Simple commands count in order;
    within one, its redirects come first (the shell opens them before it runs the command), then what the command
    reads, then what it writes. `cd` moves where later relative paths resolve, until the subshell `( ... )` it ran in
    ends; the command starts in the workspace."""
    events: list[tuple[str, str]] = []
    cwd = ""
    outer: list[str] = []  # the directory each open subshell started in
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
            for c in word:  # punctuation runs together: `);` or `((`
                if c == "(":
                    outer.append(cwd)
                elif c == ")" and outer:
                    cwd = outer.pop()
        else:
            segment.append(word)
        i += 1
    return events


# Options that take a value as the next word, and options that give the pattern or script (so the first operand is a
# file), per command. A value joined to its option (`-n5`, `--regexp=x`) needs no skipping.
VALUE_OPTIONS = {
    "head": {"-n", "-c"}, "tail": {"-n", "-c"},
    "grep": {"-e", "-f", "-A", "-B", "-C", "-m", "--regexp", "--file"},
    "rg": {"-e", "-f", "-A", "-B", "-C", "-m", "-g", "-t", "-T", "--regexp", "--file", "--glob", "--type"},
    "sed": {"-e", "-f", "--expression", "--file"}, "perl": {"-e", "-E"}, "awk": {"-F", "-v", "-f"},
    "nl": {"-b", "-w", "-s", "-v", "-i"}, "truncate": {"-s", "-r", "--size", "--reference"},
}
SCRIPT_OPTIONS = {"grep": {"-e", "-f", "--regexp", "--file"}, "rg": {"-e", "-f", "--regexp", "--file"},
                  "sed": {"-e", "-f", "--expression", "--file"}, "awk": {"-f"}, "perl": {"-e", "-E"}}


def operands(cmd: str, args: list[str]) -> list[str]:
    """A command's file operands: no options or option values, and for grep, rg, sed, awk and perl no pattern or
    script (their first operand, unless an option gave it)."""
    takes, scripts = VALUE_OPTIONS.get(cmd, set()), SCRIPT_OPTIONS.get(cmd, set())
    out: list[str] = []
    script_given, ended, i = False, False, 0
    while i < len(args):
        a = args[i]
        if ended or a == "-" or not a.startswith("-"):
            out.append(a)
        elif a == "--":
            ended = True
        else:
            name = a.split("=", 1)[0]
            script_given = script_given or name in scripts
            if name in takes and "=" not in a:
                i += 1
        i += 1
    return out[1:] if scripts and not script_given else out


def _segment_access(segment: list[str], cwd: str, events: list[tuple[str, str]]) -> str:
    """Append one simple command's own reads and writes; return the working directory after it."""
    if not segment:
        return cwd
    cmd, args = segment[0].rsplit("/", 1)[-1], segment[1:]
    files = [normalize(a, cwd) for a in operands(cmd, args)]
    if cmd == "cd":  # a directory outside the workspace stays absolute; `cd` alone (or `cd ~`, `cd -`) leaves it
        return files[0] if files and not files[0].startswith(("~", "-")) else HOME
    if cmd in ("sed", "perl") and _in_place(args):
        events += [("write", f) for f in files]
    elif cmd in SHELL_READERS:
        events += [("read", f) for f in files]
    elif cmd in ("tee", "truncate"):
        events += [("write", f) for f in files]
    elif cmd in ("cp", "mv") and len(files) >= 2:  # onto a file, or into a directory under each source's name
        dest = files[-1]
        events.append(("write", dest))
        events += [("write", posixpath.join(dest, posixpath.basename(src))) for src in files[:-1]]
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

    Prompt and completion tokens are the session totals (llama-server's usage per call, compaction summaries
    included); cached-prompt tokens are the cached prefix llama-server reported per agent turn
    (`turn_metrics.cache_tokens`), None if it never said. A summary's cached prefix isn't recorded, so neither driver
    counts it there."""
    calls = [(e["data"].get("name", ""), e["data"].get("args") or {}) for e in events if e["type"] == "tool_call"]
    cache = [e["data"].get("cache_tokens") for e in events if e["type"] == "turn_metrics"]
    known = [c for c in cache if isinstance(c, int)]
    totals = session.get("totals") or {}
    return {"driver": "native", "completion_tokens": totals.get("completion_tokens", 0),
            "cached_prompt_tokens": sum(known) if known else None, "tool_calls": tool_counts(calls),
            **file_access(calls, existing)}
