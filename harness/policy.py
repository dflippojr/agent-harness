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
    {"tool": ["run_shell", "Bash", "exec_command"], "args": {"command": r"\bgit\s+push\b"}, "action": ASK,
     "reason": "git push"},
    {"tool": ["run_shell", "Bash", "exec_command"],
     "args": {"command": r"\bgit\s+(reset\s+--hard|clean\s+-\w*f)"}, "action": ASK,
     "reason": "discards uncommitted work"},
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
    {"tool": ["run_shell", "Bash", "exec_command"], "args": {"command": r"\bgit\s+push\b"}, "action": DENY,
     "reason": "the session branch is published by the harness; the user merges or pushes it from the review screen"},
]


# Always asked, whatever the project rules say (a project rule can still deny them). The memory library also refuses
# these writes without an approved approval, so the policy isn't the only gate.
ALWAYS_ASK: dict[str, str] = {
    "memory_edit": "changes your memory library",
    "memory_write": "changes your memory library",
    "open_claude_remote_control": "starts Claude Code Remote Control in a project folder on this PC",
}


# Hosted Claude Code sees the daemon's own tools through the harness MCP server as mcp__harness__<tool> (#300). They are
# decided as the native tool they name, so project rules and defaults apply unchanged. Other MCP servers aren't wired.
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


def _crosses_link(root: Path, parts: list[str]) -> bool:
    """True when a component of `parts` under the host workspace `root` is a symlink or reparse point, so the read
    could land outside /workspace. Components past the first missing one can't be links. Stops at a glob."""
    cur = root
    for part in parts:
        if _GLOB_CHARS.search(part):
            break
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
    return root is None or not _crosses_link(root, parts)


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


def _matches(rule: dict, name: str, args: dict, root: Path | None = None) -> bool:
    if not _tool_matches(rule, name):
        return False
    if "network" in rule and bool(args.get("network", False)) != bool(rule["network"]):
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


_SPLIT = re.compile(r"&&|\|\||[;|\n&]")
_DELETERS = {"rm", "rmdir", "unlink", "shred"}


def _delete_outside_scratch(command: str) -> bool:
    """True if the command may delete something outside the scratch area (or we can't tell)."""
    if re.search(r"\bfind\b[^;&|]*\s-delete\b", command) or re.search(r"\bfind\b[^;&|]*-exec\s+rm\b", command):
        return True
    words = re.findall(r"(?:^|[\s;&|(`$])(rm|rmdir|unlink|shred)\s", command)
    if not words:
        return False
    if re.search(r"\bcd\b|\$|`|\*", command.replace("*.pyc", "")):
        return True  # relative targets depend on cwd or expansion we can't evaluate
    return any(_part_deletes_outside_scratch(part) for part in _SPLIT.split(command))


def _part_deletes_outside_scratch(part: str) -> bool:
    try:
        tokens = shlex.split(part)
    except ValueError:
        return True
    while tokens and tokens[0] in ("sudo", "command", "exec", "xargs"):
        tokens = tokens[1:]
    if not tokens or tokens[0] not in _DELETERS:
        return False
    targets = [t for t in tokens[1:] if not t.startswith("-")]
    if not targets:
        return True
    return any(_target_outside_scratch(target) for target in targets)


def _target_outside_scratch(target: str) -> bool:
    path = target if target.startswith("/tmp") else normalize_path(target)
    if path in (".", "..") or path.startswith("../") or "/../" in path:
        return True
    return not any(fnmatch.fnmatch(path, g) for g in SCRATCH_GLOBS)


class Policy:
    def __init__(self, project_rules: list[dict] | None = None, repo: bool = False,
                 workspace_root: Path | None = None):
        """`workspace_root` is the host folder bind-mounted at /workspace; with it, Claude Code reads that pass
        through a symlink or junction there are asked, not allowed. Without it only the lexical check applies."""
        self.workspace_root = Path(workspace_root) if workspace_root else None
        cleaned = []
        for rule in project_rules or []:
            if rule.get("action") not in (ALLOW, ASK, DENY):
                raise ValueError(f"policy rule needs action allow|ask|deny: {rule}")
            # Project rules cannot opt into smart review or widen eligibility.
            cleaned.append({k: v for k, v in rule.items() if k != "smart_eligible"})
        self.rules = cleaned + (REPO_RULES if repo else []) + DEFAULT_RULES

    def fingerprint(self) -> str:
        """Stable id of the ordered rule set the deterministic gate used."""
        payload = json.dumps(self.rules, sort_keys=True, default=str).encode()
        return hashlib.sha256(payload).hexdigest()[:16]

    def decide(self, name: str, args: dict) -> Decision:
        alias = ""
        if name.startswith("mcp__"):
            bare = mcp_harness_tool(name)
            if bare is None:
                return Decision(DENY, "only the harness MCP server is available to hosted sessions")
            name, alias = bare, name
        for rule in self.rules:
            root = self.workspace_root
            if _matches(rule, name, args, root) or (alias and _matches(rule, alias, args, root)):
                if name in ALWAYS_ASK and rule["action"] != DENY:
                    break
                return Decision(rule["action"], rule.get("reason", ""),
                                smart_eligible=bool(rule.get("smart_eligible")))
        if name in ALWAYS_ASK:
            return Decision(ASK, ALWAYS_ASK[name])
        if name in ("run_shell", "Bash", "exec_command") and _delete_outside_scratch(args.get("command", "")):
            return Decision(ASK, "deletes files outside the scratch area")
        return Decision(ALLOW)


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
# App's schemas, and Claude Code runs with --tools "" plus the harness MCP server. Codex and Cursor have no verified
# way to drop their built-in tools yet, so they refuse.
TOOLS_ONLY = "tools_only"
TOOLS_ONLY_BACKENDS = ("local", "claude")
TOOLS_ONLY_UNSUPPORTED = "app_tools_only_unsupported"
APP_TOOLS_ONLY_DENY = "not available in an App-tools-only session; only the App's own tools are"


class AppToolsPolicy:
    """App-tools-only sessions (#329) may call exactly the App's registered tools, natively or as
    mcp__harness__<tool> from hosted Claude Code. Everything else is denied outright: no owner is present to approve."""

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
