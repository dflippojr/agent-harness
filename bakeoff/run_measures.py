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


# `<<WORD` or `<<-WORD` (WORD whole, quoted or not); not a here-string `<<<`
HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)\\?([^\s'\";&|()<>]+)\2")


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
        # only a real marker: not in a comment or inside quotes (_protect drops the one, hides the other's `<`)
        ends = [(m.group(3).replace("\\", ""), m.group(1) == "-") for m in HEREDOC.finditer(_protect(line))]
    return "\n".join(out)


OPERATOR_CHARS = ";&|()<>\n"
# Quoted or escaped operator characters are data: they stand in as private-use characters while shlex splits the
# command (posix shlex drops the quotes that would tell them apart), and come back in the words afterwards.
_HIDE = {ord(c): chr(0xE000 + i) for i, c in enumerate(OPERATOR_CHARS)}
_SHOW = {ord(v): k for k, v in ((chr(k), v) for k, v in _HIDE.items())}


def _protect(command: str) -> str:
    """The command with unquoted `# ...` comments removed (keeping the newline that ends them), line continuations
    joined, and quoted or backslash-escaped operator characters hidden from the lexer."""
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
                c = command[i].translate(_HIDE)
            else:
                c = c.translate(_HIDE)
        elif c in "'\"":
            quote = c
        elif c == "\\" and i + 1 < len(command):
            i += 1
            if command[i] == "\n":  # a line continuation: no character at all
                i += 1
                continue
            out.append(c)
            c = command[i].translate(_HIDE)
        elif c == "#" and (not out or out[-1] in " \t\n;&|()"):
            while i < len(command) and command[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _words(command: str) -> list[str]:
    """Shell words and operators; an unquoted newline is an operator (it ends a command, like `;`)."""
    lexer = shlex.shlex(_protect(_without_heredoc_bodies(command)), posix=True, punctuation_chars=OPERATOR_CHARS)
    lexer.commenters = ""
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    try:
        return list(lexer)  # quoted operator characters stay hidden until shell_access has classified the words
    except ValueError:  # an unbalanced quote: judge it word by word
        return command.replace("\n", " ; ").split()


def _in_place(cmd: str, args: list[str]) -> bool:
    """`sed -i`, `sed -i.bak`, `sed --in-place`, `perl -pi`: an i among the short flags before any that takes a value
    (so not the i in an attached script such as `-e'/fix/p'`), or --in-place."""
    takes = VALUE_OPTIONS.get(cmd, set())
    for a in args:
        if a.split("=", 1)[0] == "--in-place":
            return True
        if a[:1] == "-" and a[1:2] not in ("", "-"):
            for c in a[1:]:
                if c == "i":
                    return True
                if not c.isalpha() or f"-{c}" in takes:
                    break
    return False


OPERATORS = sorted(["&&", "||", ";;", "<<<", "<<-", "<<", ">>", ">|", "&>>", "&>", ">&", "<&", "<>", ";", "&", "|",
                    "(", ")", "<", ">", "\n"], key=len, reverse=True)


def _operators(run: str) -> list[str]:
    """shlex runs adjacent punctuation together (`\n>`, `);`): split it into shell operators, longest first."""
    out, i = [], 0
    while i < len(run):
        op = next(o for o in [*OPERATORS, run[i]] if run.startswith(o, i))
        out.append(op)
        i += len(op)
    return out


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
    reads, then what it writes. A subshell `( ... ) > f` is one command too: the redirects after its `)` open
    before anything in it runs, so `(cat f) > f` empties f before reading it. `cd` moves where later relative paths
    resolve (`cd -` back to the previous directory), until the subshell it ran in ends; the command starts in the
    workspace with no previous directory."""
    events: list[tuple[str, str]] = []
    cwd, oldpwd = "", None
    outer: list[tuple[str, str | None, int]] = []  # each open subshell: its directories then, where its events begin
    segment: list[str] = []
    redirects: list[tuple[str, str]] = []
    words = [w for word in _words(command) for w in (_operators(word) if _is_operator(word) else [word])] + [";"]
    i = 0
    while i < len(words):
        word = words[i]
        if word in REDIRECTS:
            if _is_operator(words[i + 1]):  # a dangling redirect (a syntax error): skip it, keep what follows
                i += 1
                continue
            redirects += _redirect(word, words[i + 1])
            i += 2
            continue
        if _is_operator(word):  # end of a simple command (`>&` and friends end up here too)
            events += [(kind, normalize(t, cwd)) for kind, t in redirects]
            if segment and segment[0].rsplit("/", 1)[-1] == "cd":
                cwd, oldpwd = _cd(segment[1:], cwd, oldpwd)
            else:
                _segment_access(segment, cwd, events)
            segment, redirects = [], []
            if word == "(":
                outer.append((cwd, oldpwd, len(events)))
            elif word == ")" and outer:
                cwd, oldpwd, start = outer.pop()
                body = events[start:]
                del events[start:]
                while i + 1 < len(words):  # the subshell's own redirects come first (`2>log`, `2>&1`, `1> f`)
                    digit = words[i + 1].isdigit() and i + 2 < len(words) and words[i + 2] in REDIRECTS + DUPS
                    j = i + 2 if digit else i + 1
                    if words[j] not in REDIRECTS + DUPS:
                        break
                    if j + 1 >= len(words) or _is_operator(words[j + 1]):  # dangling: skip it, keep what follows
                        i = j
                        break
                    if words[j] in REDIRECTS:
                        events += [(kind, normalize(t, cwd)) for kind, t in _redirect(words[j], words[j + 1])]
                    i = j + 1  # a descriptor copy (`>&`, `<&`) opens no file
                events += body
        else:
            segment.append(word.translate(_SHOW))
        i += 1
    return events


REDIRECTS = (">", ">>", ">|", "&>", "&>>", "<")
DUPS = (">&", "<&")  # descriptor copies: `2>&1`


def _redirect(op: str, target: str) -> list[tuple[str, str]]:
    """The access one redirect makes: `< f` reads f, the others write it (nothing for /dev/null or a missing word)."""
    if _is_operator(target) or target == "/dev/null":
        return []
    return [("read" if op == "<" else "write", target.translate(_SHOW))]


# Options that take a value as the next word, and options that give the pattern or script (so the first operand is a
# file), per command. A value joined to its option (`-n5`, `--regexp=x`) needs no skipping.
VALUE_OPTIONS = {
    "head": {"-n", "-c"}, "tail": {"-n", "-c"},
    "grep": {"-e", "-f", "-A", "-B", "-C", "-m", "-d", "-D", "--regexp", "--file", "--include", "--exclude",
             "--exclude-dir", "--exclude-from", "--max-count", "--context", "--after-context", "--before-context",
             "--label", "--binary-files", "--devices", "--directories"},  # --color[=WHEN]: optional, joined
    "rg": {"-e", "-f", "-A", "-B", "-C", "-m", "-g", "-t", "-T", "-M", "-E", "--regexp", "--file", "--glob",
           "--iglob", "--type", "--type-not", "--max-count", "--context", "--after-context", "--before-context",
           "--max-columns", "--encoding", "--max-depth", "--replace", "-r", "--sort", "--sortr", "--color",
           "--colors", "--path-separator", "--pre", "--pre-glob", "--ignore-file", "--threads", "-j",
           "--max-filesize", "--engine"},
    "sed": {"-e", "-f", "--expression", "--file"}, "perl": {"-e", "-E"}, "awk": {"-F", "-v", "-f"},
    "nl": {"-b", "-w", "-s", "-v", "-i"}, "truncate": {"-s", "-r", "--size", "--reference"},
    "cp": {"-t", "--target-directory", "-S", "--suffix"}, "mv": {"-t", "--target-directory", "-S", "--suffix"},
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
        elif a.startswith("--"):
            name = a.split("=", 1)[0]
            script_given = script_given or name in scripts
            if name in takes and "=" not in a:
                i += 1
        else:  # a cluster of short options (`-ne`, `-pie`); the first that takes a value ends it
            for j, c in enumerate(a[1:], 1):
                if not c.isalpha():
                    break
                if f"-{c}" in takes:
                    script_given = script_given or f"-{c}" in scripts
                    if j == len(a) - 1:  # its value is the next word; otherwise it is attached (`-e's/a/b/'`, `-n5`)
                        i += 1
                    break
        i += 1
    return out[1:] if scripts and not script_given else out


def _target_directory(args: list[str]) -> str | None:
    """The directory `cp`/`mv -t DIR` (`-tDIR`, `--target-directory[=]DIR`) copies into, or None."""
    for i, a in enumerate(args):
        if a in ("-t", "--target-directory"):
            return args[i + 1] if i + 1 < len(args) else None
        if a.startswith("--target-directory="):
            return a.split("=", 1)[1]
        if a.startswith("-t") and len(a) > 2 and not a.startswith("--"):
            return a[2:]
    return None


def _cd(args: list[str], cwd: str, oldpwd: str | None) -> tuple[str, str | None]:
    """(working directory, previous one) after `cd ARGS`. `cd -` goes back (and stays put when there is no previous
    directory, as a fresh shell has none); `cd` alone or `cd ~...` leaves the workspace; a directory outside it stays
    absolute."""
    ops = operands("cd", args)
    if ops and ops[0] == "-":
        return (oldpwd, cwd) if oldpwd is not None else (cwd, oldpwd)
    if not ops or ops[0].startswith("~"):
        return HOME, cwd
    return normalize(ops[0], cwd), cwd


def _segment_access(segment: list[str], cwd: str, events: list[tuple[str, str]]) -> None:
    """Append one simple command's own reads and writes (`cd` is shell_access's own business)."""
    if not segment:
        return
    cmd, args = segment[0].rsplit("/", 1)[-1], segment[1:]
    files = [normalize(a, cwd) for a in operands(cmd, args)]
    if cmd in ("sed", "perl") and _in_place(cmd, args):
        events += [("write", f) for f in files]
    elif cmd in SHELL_READERS:
        events += [("read", f) for f in files]
    elif cmd in ("tee", "truncate"):
        events += [("write", f) for f in files]
    elif cmd in ("cp", "mv"):  # onto a file, or into a directory under each source's name (`-t DIR` names it)
        target = _target_directory(args)
        dest, sources = (normalize(target, cwd), files) if target is not None else (
            (files[-1], files[:-1]) if len(files) >= 2 else ("", []))
        if sources:
            events.append(("write", dest))
            events += [("write", posixpath.join(dest, posixpath.basename(src))) for src in sources]


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
