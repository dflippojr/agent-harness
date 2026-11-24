"""Approval policy: decides allow / ask / deny for each tool call.

This is a usability gate, not the security boundary; the sandbox is. Shell classification is heuristic and
errs toward asking.
"""

from __future__ import annotations

import hashlib
import json
import fnmatch
import os
import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .storage import is_reparse_point
from .tools import normalize_path

ALLOW, ASK, DENY = "allow", "ask", "deny"

# Paths an agent may delete without asking (matched against normalized paths; /tmp is container-local).
SCRATCH_GLOBS = ["/tmp", "/tmp/*", "scratch", "scratch/*", "*__pycache__*", "*.pyc", ".pytest_cache*",
                 "*/.pytest_cache*", "build", "build/*", "dist", "dist/*", "*.egg-info", "*.egg-info/*"]

DEFAULT_RULES: list[dict] = [
    {"tool": ["run_shell", "Bash", "exec_command"], "network": True, "action": ASK,
     "reason": "command needs network access"},
    {"tool": ["run_shell", "Bash", "exec_command"], "shell": "git_push", "action": ASK,
     "reason": "git push"},
    {"tool": ["run_shell", "Bash", "exec_command"],
     "shell": "git_discard", "action": ASK,
     "reason": "discards uncommitted work"},
    {"tool": ["run_shell", "Bash", "exec_command"], "shell": "delete", "action": ASK,
     "reason": "deletes files outside the scratch area"},
    {"tool": ["run_shell", "Bash", "exec_command"], "shell": "opaque", "action": ASK,
     "reason": "cannot determine what the shell command does"},
    # Claude Code reads stay in /workspace (#370): the shared login volume holds every Claude session's history.
    {"tool": "Read", "workspace_read": "file_path", "action": ALLOW},
    {"tool": ["Glob", "Grep", "LS"], "workspace_read": "path", "action": ALLOW},
    {"tool": ["Read", "Glob", "Grep", "LS"], "action": ASK, "reason": "reads a file outside /workspace"},
    {"tool": ["Edit", "Write", "MultiEdit", "NotebookEdit"],
     "workspace_path": "file_path", "action": ALLOW},
    {"tool": ["Edit", "Write", "MultiEdit", "NotebookEdit"], "action": ASK,
     "reason": "changes a file outside /workspace or uses an unrecognized path"},
    {"tool": ["WebFetch", "WebSearch"], "action": ASK, "reason": "uses Claude Code web access"},
    {"tool": "Bash", "action": ASK, "reason": "runs a Claude Code shell command", "smart_eligible": True},
    {"tool": "apply_patch", "workspace_paths": "file_paths", "action": ALLOW},
    {"tool": "apply_patch", "action": ASK,
     "reason": "changes a file outside /workspace or uses an unrecognized path"},
    {"tool": "git_clone", "args": {"url": r"^(local:|https://(github\.com|gitlab\.com|codeberg\.org)/)"},
     "action": ALLOW},
    {"tool": "git_clone", "action": ASK, "reason": "clone from a host that isn't on the allowlist"},
    {"tool": "restart_service", "action": ASK, "reason": "restarts a homelab service"},
    {"tool": "rebuild_service", "action": ASK, "reason": "rebuilds and recreates a homelab service"},
]

# Added for projects with a repo: the daemon publishes the session branch, the user reviews and merges it.
REPO_RULES: list[dict] = [
    {"tool": ["run_shell", "Bash", "exec_command"], "shell": "git_push", "action": DENY,
     "reason": "the session branch is published by the harness; the user merges or pushes it from the review screen"},
]


# Always asked, whatever the project rules say (a project rule can still deny them). The memory library also refuses
# these writes without an approved approval, so the policy isn't the only gate.
ALWAYS_ASK: dict[str, str] = {
    "memory_edit": "changes your memory library",
    "memory_write": "changes your memory library",
    "open_claude_remote_control": "starts Claude Code Remote Control in a project folder on this PC",
}

# Edits under the workspace's .claude/ folder are asked even when a project rule would allow them (#531): hosted
# Claude Code ignores project settings, but a file there could still change a later Claude session on that folder.
CLAUDE_DIR_REASON = "changes Claude Code settings under .claude/"
_WRITE_PATH_KEYS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path",
                    "write_file": "path", "edit_file": "path"}


# Hosted Claude Code sees the daemon's own tools through the harness MCP server as mcp__harness__<tool> (#300). They are
# decided as the native tool they name, so project rules and defaults apply unchanged. Native client servers are
# separately allowlisted per session and retain their full names for rules.
MCP_SERVER = "harness"
MCP_PREFIX = f"mcp__{MCP_SERVER}__"


def mcp_harness_tool(name: str) -> str | None:
    """The harness tool behind a Claude Code MCP tool name (mcp__harness__web_search -> web_search), else None."""
    bare = name[len(MCP_PREFIX):] if name.startswith(MCP_PREFIX) else ""
    return bare or None


@dataclass
class Decision:
    action: str
    reason: str = ""
    smart_eligible: bool = False


def _in_workspace(value) -> bool:
    raw = str(value).strip().replace("\\", "/")
    normalized = posixpath.normpath(raw)
    return normalized == "/workspace" or normalized.startswith("/workspace/")


_GLOB_CHARS = re.compile(r"[*?\[{]")


def _workspace_parts(value: str) -> list[str] | None:
    """The components under /workspace of a Claude Code read path (relative paths start at its cwd, /workspace),
    or None when the path leaves /workspace or uses a form the CLI may expand differently (~, $VAR). Any `..` is
    refused: normalizing it away would hide a link before it (`link/../x` opens x beside the link's target)."""
    raw = value.strip().replace("\\", "/")
    if raw.startswith(("~", "$")) or "\0" in raw or ".." in raw.split("/"):
        return None
    normalized = posixpath.normpath(raw if raw.startswith("/") else "/workspace/" + raw)
    if normalized == "/workspace":
        return []
    if not normalized.startswith("/workspace/"):
        return None
    return normalized[len("/workspace/"):].split("/")


def _crosses_link(root: Path, parts: list[str], glob: bool = False) -> bool:
    """True when a component of `parts` under the host workspace `root` is a symlink or reparse point, so the read
    could land outside /workspace. Components past the first missing one can't be links. Every component is literal,
    except in a Glob pattern: there a wildcard may match any folder, link or not, so a literal folder after it (as in
    `**/link/x`) can't be checked and counts as crossing. A trailing name after a wildcard only lists paths."""
    cur = root
    for i, part in enumerate(parts):
        if glob and _GLOB_CHARS.search(part):
            return i < len(parts) - 2
        if os.name == "nt" and ":" in part:
            return True  # a drive or stream name would check a different host path than the container reads
        cur = cur / part
        if not os.path.lexists(cur):
            break
        if is_reparse_point(cur):
            return True
    return False


def _workspace_read_matches(rule: dict, name: str, args: dict, root: Path | None) -> bool:
    """A Claude Code read whose target stays inside /workspace. A missing `path` means the cwd, /workspace, except for
    Read, which needs its file_path. With a host workspace root, a link anywhere along the path fails the match."""
    key = rule["workspace_read"]
    value = args.get(key)
    if value is None or value == "":
        if key == "file_path":
            return False
        value = "/workspace"
    if not isinstance(value, str):
        return False
    parts = _workspace_parts(value)
    if parts is None:
        return False
    pattern = args.get("pattern") if name == "Glob" else None
    if pattern is not None:
        # Glob's pattern may be absolute or climb out of its base path.
        if not isinstance(pattern, str):
            return False
        joined = pattern if pattern.strip().startswith("/") else "/workspace/" + "/".join(parts + [pattern])
        parts = _workspace_parts(joined)
        if parts is None:
            return False
    return root is None or not _crosses_link(root, parts, glob=pattern is not None)


def _path_rule_matches(rule: dict, args: dict) -> bool:
    if "path" not in args:
        return False
    globs = [rule["path"]] if isinstance(rule["path"], str) else rule["path"]
    path = normalize_path(args["path"])
    return any(fnmatch.fnmatch(path, g) for g in globs)


def _workspace_paths_match(rule: dict, args: dict) -> bool:
    values = args.get(rule["workspace_paths"])
    if not isinstance(values, list) or not values:
        return False
    return all(_in_workspace(value) for value in values)


def _tool_matches(rule: dict, name: str) -> bool:
    tools = rule.get("tool", "*")
    tools = [tools] if isinstance(tools, str) else tools
    aliases = {name, "run_shell"} if name in ("Bash", "exec_command") else {name}
    return "*" in tools or bool(aliases.intersection(tools))


def _matches(rule: dict, name: str, args: dict, root: Path | None = None,
             shell: _ShellAnalysis | None = None) -> bool:
    if not _tool_matches(rule, name):
        return False
    if "network" in rule and bool(args.get("network", False)) != bool(rule["network"]):
        return False
    if "shell" in rule and (shell is None or not getattr(shell, rule["shell"])):
        return False
    for key, pattern in (rule.get("args") or {}).items():
        if not re.search(pattern, str(args.get(key, ""))):
            return False
    if "path" in rule and not _path_rule_matches(rule, args):
        return False
    if "workspace_path" in rule and not _in_workspace(args.get(rule["workspace_path"], "")):
        return False
    if "workspace_paths" in rule and not _workspace_paths_match(rule, args):
        return False
    if "workspace_read" in rule and not _workspace_read_matches(rule, name, args, root):
        return False
    return True


_DELETERS = {"rm", "rmdir", "unlink", "shred"}


@dataclass
class _ShellAnalysis:
    git_push: bool = False
    git_discard: bool = False
    delete: bool = False
    opaque: bool = False


# Only skip options whose arity we know. Guessing where a wrapper's command starts
# could turn an unrecognized invocation into an allow. Unknown options ask instead.
_WRAPPER_OPTIONS = {
    "command": ({"-p"}, set()),
    "exec": ({"-c", "-l"}, {"-a"}),
    "env": ({"-i", "--ignore-environment", "-0", "--null"}, {"-u", "--unset", "-C", "--chdir"}),
    "sudo": ({"-E", "-H", "-n", "-S", "-k", "-b", "--non-interactive", "--preserve-env"},
             {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "--close-from",
              "-D", "--chdir", "-R", "--chroot"}),
    "timeout": ({"--foreground", "--preserve-status", "-v", "--verbose"},
                {"-s", "--signal", "-k", "--kill-after"}),
    "nice": (set(), {"-n", "--adjustment"}),
    "nohup": (set(), set()),
    "time": ({"-p", "-a", "--append", "-v", "--verbose"}, {"-f", "--format", "-o", "--output"}),
    "builtin": (set(), set()),
    "stdbuf": (set(), {"-i", "--input", "-o", "--output", "-e", "--error"}),
    "xargs": ({"-0", "--null", "-r", "--no-run-if-empty", "-t", "--verbose", "-p", "--interactive", "-x"},
              {"-n", "--max-args", "-P", "--max-procs", "-s", "--max-chars", "-d", "--delimiter",
               "-I", "--replace", "-L", "--max-lines", "-E", "--eof"}),
}
_GIT_FLAGS = {"-p", "--paginate", "-P", "--no-pager", "--bare", "--no-replace-objects", "--no-lazy-fetch",
              "--no-optional-locks", "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs",
              "--icase-pathspecs", "--no-advice"}
_GIT_VALUES = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--exec-path",
               "--super-prefix", "--attr-source"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
_SHELL_PUNCTUATION = re.compile(r"&&|\|\||&>>?|[<>]&|<<-?|>>|[;&|()<>\n]")
_INLINE_CODE_FLAGS = {"python": {"-c"}, "python3": {"-c"}, "perl": {"-e", "-E"}, "ruby": {"-e"},
                      "node": {"-e", "--eval", "-p", "--print"}}


def _after_options(argv: list[str], flags: set[str], values: set[str]) -> int | None:
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        word = argv[i]
        if word == "--":
            return i + 1
        if word in flags:
            i += 1
        elif word in values:
            if i + 1 == len(argv):
                return None
            i += 2
        elif (word.startswith("--") and "=" in word and word.split("=", 1)[0] in values
              or not word.startswith("--") and word[:2] in values and len(word) > 2):
            i += 1
        else:
            return None
    return i


def _analyze_shell(command: str, result: _ShellAnalysis, changed_cwd: bool = False, depth: int = 0) -> None:
    """Inspect literal shell commands without executing or expanding any input.

    Groups and command lists share this path with nested shells and wrappers. This is
    deliberately a bounded heuristic, not a shell interpreter; unsupported execution
    syntax asks. Keep collecting known hazards even when another part is opaque.
    """
    if depth > 20:
        result.opaque = True
        return
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""  # A mid-word # is literal in a shell; never hide a following command.
        tokens = []
        for token in lexer:
            # shlex joins adjacent punctuation, including `((` and `);`. A group
            # closing beside a separator must not hide the next executable.
            tokens.extend(_SHELL_PUNCTUATION.findall(token) if token and all(
                c in ";&|()<>\n" for c in token) else [token])
    except ValueError:
        result.opaque = True
        return
    if "$(" in command or "`" in command:
        result.opaque = True
    changed_cwd = changed_cwd or any(posixpath.basename(t) in {"cd", "pushd", "popd"} for t in tokens)
    part: list[str] = []
    for token in tokens + [";"]:
        if token in {";", "&&", "||", "|", "&", "(", ")", "{", "}", "\n"}:
            if part:
                _analyze_argv(part, result, changed_cwd, depth)
                part = []
        else:
            part.append(token)


def _analyze_argv(argv: list[str], result: _ShellAnalysis, changed_cwd: bool, depth: int) -> None:
    # Redirects may precede an executable. Remove literal file redirects before
    # unwrapping, while refusing heredocs and descriptor tricks we cannot resolve.
    words = []
    i = 0
    while i < len(argv):
        word = argv[i]
        if word in {"<", ">", ">>", "<&", ">&", "&>", "&>>"}:
            if words and words[-1].isdigit():
                words.pop()
            if i + 1 == len(argv):
                result.opaque = True
                return
            i += 2
            continue
        if word.startswith("<<"):
            result.opaque = True
        words.append(word)
        i += 1
    argv = words
    for _ in range(21 - depth):
        while argv and (_ASSIGNMENT.match(argv[0]) or argv[0] in {"!", "if", "then", "elif", "else", "do",
                                                                 "while", "until"}):
            argv = argv[1:]
        if not argv:
            return
        name = posixpath.basename(argv[0])
        if any(c in argv[0] for c in "$`*?[{}"):
            result.opaque = True
            return
        if name in {"sh", "bash", "dash", "zsh", "ksh", "ash"}:
            _analyze_shell_script(argv, result, changed_cwd, depth)
            return
        if name == "eval":
            _analyze_shell(" ".join(argv[1:]), result, changed_cwd, depth + 1)
            return
        if name in {"source", "."}:
            result.opaque = True
            return
        if name in _INLINE_CODE_FLAGS:
            for word in argv[1:]:
                if not word.startswith("-") or word in {"-m", "--"}:
                    break  # module/script arguments are not interpreter options
                if any(word == flag or word.startswith(flag + "=") or len(flag) == 2 and word.startswith(flag)
                       for flag in _INLINE_CODE_FLAGS[name]):
                    result.opaque = True
                    return
        if name not in _WRAPPER_OPTIONS:
            break
        if name == "command" and len(argv) > 1 and argv[1] in {"-v", "-V"}:
            return  # describes the command, does not execute it
        if name == "env" and len(argv) > 2 and argv[1] in {"-S", "--split-string"}:
            _analyze_shell(argv[2] + " " + shlex.join(argv[3:]), result, changed_cwd, depth + 1)
            return
        if name == "xargs":
            result.opaque = True  # stdin can supply commands, extra targets or replacement text
        flags, values = _WRAPPER_OPTIONS[name]
        start = _after_options(argv, flags, values)
        if start is None:
            result.opaque = True
            return
        changed_cwd = changed_cwd or name == "sudo" or name == "env" and any(
            t == "-C" or t.startswith(("-C", "--chdir")) for t in argv[1:start])
        if name == "timeout":
            start += 1  # duration precedes the command
        argv = argv[start:]
        if not argv:
            result.opaque = True
            return
    else:
        result.opaque = True
        return
    if name == "git":
        start = _after_options(argv, _GIT_FLAGS, _GIT_VALUES)
        if start is None:
            result.opaque = True
            return
        args = argv[start:]
        if args:
            result.git_push |= args[0] == "push"
            # Stop at --: a path named --hard or -f is not an option.
            options = args[1:args.index("--")] if "--" in args else args[1:]
            result.git_discard |= (args[0] == "reset" and "--hard" in options or args[0] == "clean" and any(
                t == "--force" or t.startswith("-") and not t.startswith("--") and "f" in t for t in options))
            result.opaque |= any(c in args[0] for c in "$`*?[{}")
    elif name in _DELETERS:
        end = argv.index("--") if "--" in argv else len(argv)
        targets = [t for t in argv[1:end] if not t.startswith("-")] + argv[end + 1:]
        result.delete |= (changed_cwd or not targets or any(
            _target_outside_scratch(t) or any(c in t.replace("*.pyc", "") for c in "$`*?[") for t in targets))
    elif name == "find":
        result.delete |= "-delete" in argv
        for i, word in enumerate(argv):
            if word in {"-exec", "-execdir", "-ok", "-okdir"}:
                child = argv[i + 1:]
                end = next((j for j, t in enumerate(child) if t in {";", "+"}), len(child))
                if child[:end] and "{}" not in child[0]:
                    _analyze_argv(child[:end], result, changed_cwd or word.endswith("dir"), depth + 1)
                else:
                    result.opaque = True


def _analyze_shell_script(argv: list[str], result: _ShellAnalysis, changed_cwd: bool, depth: int) -> None:
    i = 1
    while i < len(argv):
        word = argv[i]
        if word in {"-o", "-O", "--rcfile", "--init-file"}:
            # Startup files can execute additional commands.
            result.opaque = True
            i += 2
            continue
        if word in {"--noprofile", "--norc", "--posix", "--login"}:
            i += 1
            continue
        if word.startswith("-") and not word.startswith("--") and "c" in word:
            if i + 1 < len(argv):
                _analyze_shell(argv[i + 1], result, changed_cwd, depth + 1)
                return
            break
        if not word.startswith("-") or word == "--":
            break
        i += 1
    result.opaque = True  # stdin, a script file or an unrecognized shell invocation


def _delete_outside_scratch(command: str) -> bool:
    """The shared conservative deletion check used by smart approval eligibility."""
    result = _ShellAnalysis()
    _analyze_shell(command, result)
    return result.delete or result.opaque


def _target_outside_scratch(target: str) -> bool:
    path = target if target.startswith("/tmp") else normalize_path(target)
    if path in (".", "..") or path.startswith("../") or "/../" in path:
        return True
    return not any(fnmatch.fnmatch(path, g) for g in SCRATCH_GLOBS)


def _under_claude_dir(value: str, root: Path | None) -> bool:
    """A write path inside the workspace's .claude/ folder. The host folder is often case-insensitive, so the name is
    compared without case; with the host `root`, a link elsewhere in the workspace that lands there counts too."""
    raw = value.strip().replace("\\", "/")
    normalized = posixpath.normpath(raw if raw.startswith("/") else "/workspace/" + raw)
    if normalized.lower() == "/workspace/.claude" or normalized.lower().startswith("/workspace/.claude/"):
        return True
    if root is None or not normalized.startswith("/workspace/"):
        return False
    target = os.path.realpath(root / normalized[len("/workspace/"):])
    claude_dir = os.path.normcase(os.path.realpath(root / ".claude"))
    target = os.path.normcase(target)
    return target == claude_dir or target.startswith(claude_dir + os.sep)


class Policy:
    def __init__(self, project_rules: list[dict] | None = None, repo: bool = False,
                 workspace_root: Path | None = None, mcp_servers=()):
        """`workspace_root` is the host folder bind-mounted at /workspace; with it, Claude Code reads that pass
        through a symlink or junction there are asked, not allowed. Without it only the lexical check applies."""
        self.workspace_root = Path(workspace_root) if workspace_root else None
        self.mcp_servers = frozenset(mcp_servers)
        cleaned = []
        for rule in project_rules or []:
            if rule.get("action") not in (ALLOW, ASK, DENY):
                raise ValueError(f"policy rule needs action allow|ask|deny: {rule}")
            # Project rules cannot opt into smart review or widen eligibility.
            cleaned.append({k: v for k, v in rule.items() if k != "smart_eligible"})
        self.rules = cleaned + (REPO_RULES if repo else []) + DEFAULT_RULES

    def fingerprint(self) -> str:
        """Stable id of the ordered rule set the deterministic gate used."""
        rules = [self.rules, sorted(self.mcp_servers)] if self.mcp_servers else self.rules
        payload = json.dumps(rules, sort_keys=True, default=str).encode()
        return hashlib.sha256(payload).hexdigest()[:16]

    def decide(self, name: str, args: dict) -> Decision:
        # Configured client names may themselves contain __, including harness__docs. Their complete prefix takes
        # precedence over the hosted harness relay alias; the literal server name harness remains reserved.
        bare = None if self.is_mcp_client_tool(name) else mcp_harness_tool(name)
        client = name.startswith("mcp__") and bare is None
        if client and not self.is_mcp_client_tool(name):
            reason = ("MCP server is not configured for this session" if self.mcp_servers
                      else "only the harness MCP server is available to hosted sessions")
            return Decision(DENY, reason)
        alias = name if bare else ""
        name = bare or name
        decision = self._rule_decision(name, args, alias)
        if (decision is None or decision.action != DENY) and self._writes_claude_dir(name, args):
            return Decision(ASK, CLAUDE_DIR_REASON)
        if decision is not None:
            return decision
        if name in ALWAYS_ASK:
            return Decision(ASK, ALWAYS_ASK[name])
        return Decision(ASK, "owner-configured MCP tool requires approval") if client else Decision(ALLOW)

    def _rule_decision(self, name: str, args: dict, alias: str) -> Decision | None:
        shell = None
        if name in ("run_shell", "Bash", "exec_command"):
            shell = _ShellAnalysis()
            _analyze_shell(str(args.get("command", "")), shell)
        for rule in self.rules:
            root = self.workspace_root
            if _matches(rule, name, args, root, shell) or (alias and _matches(rule, alias, args, root, shell)):
                if name in ALWAYS_ASK and rule["action"] != DENY:
                    break
                return Decision(rule["action"], rule.get("reason", ""),
                                smart_eligible=bool(rule.get("smart_eligible")))
        return None

    def _writes_claude_dir(self, name: str, args: dict) -> bool:
        """True when a file-changing call may land under /workspace/.claude/."""
        if name == "apply_patch":
            values = args.get("file_paths")
            values = values if isinstance(values, list) else []
        elif name in _WRITE_PATH_KEYS:
            values = [args.get(_WRITE_PATH_KEYS[name])]
        else:
            return False
        return any(isinstance(v, str) and _under_claude_dir(v, self.workspace_root) for v in values)

    def is_mcp_client_tool(self, name: str) -> bool:
        return any(name.startswith(f"mcp__{server}__") and len(name) > len(f"mcp__{server}__")
                   for server in self.mcp_servers)


CHAT_ALLOWED_TOOLS = frozenset({"web_search", "web_fetch", "WebSearch", "WebFetch"})


class ChatPolicy:
    """Chat conversations may only search and fetch. Everything else is denied outright, never turned into an approval."""

    rules: list[dict] = [{"tool": "*", "action": DENY, "reason": "not available in Chat; use Agents for that"}]

    def fingerprint(self) -> str:
        return "chat-allowlist"

    def decide(self, name: str, _args: dict | None = None) -> Decision:
        if name in CHAT_ALLOWED_TOOLS:
            return Decision(ALLOW)
        return Decision(DENY, "not available in Chat; use Agents for that")


# The session kind of an App-tools-only session (#329) and the backends that can run one: the local loop sends only the
# App's schemas, Claude Code runs with --tools "" and Codex with no environment and its other built-in tools switched
# off (#373), each plus the harness MCP server. Cursor has no way to drop its built-in tools, so it refuses.
TOOLS_ONLY = "tools_only"
TOOLS_ONLY_BACKENDS = ("local", "claude", "codex")
TOOLS_ONLY_UNSUPPORTED = "app_tools_only_unsupported"
APP_TOOLS_ONLY_DENY = "not available in an App-tools-only session; only the App's own tools are"


class AppToolsPolicy:
    """App-tools-only sessions (#329) may call exactly the App's registered tools, natively or as
    mcp__harness__<tool> from hosted Claude Code or Codex. Everything else is denied outright: no owner is present to approve."""

    def __init__(self, names) -> None:
        self.names = frozenset(names)
        self.rules: list[dict] = [{"tool": sorted(self.names), "action": ALLOW},
                                  {"tool": "*", "action": DENY, "reason": APP_TOOLS_ONLY_DENY}]

    def fingerprint(self) -> str:
        return "app-tools-only:" + hashlib.sha256(",".join(sorted(self.names)).encode()).hexdigest()[:16]

    def decide(self, name: str, _args: dict | None = None) -> Decision:
        if (mcp_harness_tool(name) or name) in self.names:
            return Decision(ALLOW)
        return Decision(DENY, APP_TOOLS_ONLY_DENY)
