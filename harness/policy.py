"""Approval policy: decides allow / ask / deny for each tool call.

This is a usability gate, not the security boundary; the sandbox is. Shell classification is heuristic and
errs toward asking.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import shlex
from dataclasses import dataclass

from .tools import normalize_path

ALLOW, ASK, DENY = "allow", "ask", "deny"

# Paths an agent may delete without asking (matched against normalized paths; /tmp is container-local).
SCRATCH_GLOBS = ["/tmp", "/tmp/*", "scratch", "scratch/*", "*__pycache__*", "*.pyc", ".pytest_cache*",
                 "*/.pytest_cache*", "build", "build/*", "dist", "dist/*", "*.egg-info", "*.egg-info/*"]

DEFAULT_RULES: list[dict] = [
    {"tool": ["run_shell", "Bash"], "network": True, "action": ASK, "reason": "command needs network access"},
    {"tool": ["run_shell", "Bash"], "args": {"command": r"\bgit\s+push\b"}, "action": ASK,
     "reason": "git push"},
    {"tool": ["run_shell", "Bash"], "args": {"command": r"\bgit\s+(reset\s+--hard|clean\s+-\w*f)"}, "action": ASK,
     "reason": "discards uncommitted work"},
    {"tool": ["Read", "Glob", "Grep", "LS"], "action": ALLOW},
    {"tool": ["Edit", "Write", "MultiEdit", "NotebookEdit"],
     "workspace_path": "file_path", "action": ALLOW},
    {"tool": ["Edit", "Write", "MultiEdit", "NotebookEdit"], "action": ASK,
     "reason": "changes a file outside /workspace or uses an unrecognized path"},
    {"tool": ["WebFetch", "WebSearch"], "action": ASK, "reason": "uses Claude Code web access"},
    {"tool": "Bash", "action": ASK, "reason": "runs a Claude Code shell command"},
    {"tool": "git_clone", "args": {"url": r"^(local:|https://(github\.com|gitlab\.com|codeberg\.org)/)"},
     "action": ALLOW},
    {"tool": "git_clone", "action": ASK, "reason": "clone from a host that isn't on the allowlist"},
    {"tool": "restart_service", "action": ASK, "reason": "restarts a homelab service"},
    {"tool": "rebuild_service", "action": ASK, "reason": "rebuilds and recreates a homelab service"},
]

# Added for projects with a repo: the daemon publishes the session branch, the user reviews and merges it.
REPO_RULES: list[dict] = [
    {"tool": ["run_shell", "Bash"], "args": {"command": r"\bgit\s+push\b"}, "action": DENY,
     "reason": "the session branch is published by the harness; the user merges or pushes it from the review screen"},
]


# Always asked, whatever the project rules say (a project rule can still deny them). The memory library also refuses
# these writes without an approved approval, so the policy isn't the only gate.
ALWAYS_ASK: dict[str, str] = {
    "memory_edit": "changes your memory library",
    "memory_write": "changes your memory library",
    "open_claude_remote_control": "starts Claude Code Remote Control in a project folder on this PC",
}


@dataclass
class Decision:
    action: str
    reason: str = ""


def _matches(rule: dict, name: str, args: dict) -> bool:
    tools = rule.get("tool", "*")
    tools = [tools] if isinstance(tools, str) else tools
    aliases = {name, "run_shell"} if name == "Bash" else {name}
    if "*" not in tools and not aliases.intersection(tools):
        return False
    if "network" in rule and bool(args.get("network", False)) != bool(rule["network"]):
        return False
    for key, pattern in (rule.get("args") or {}).items():
        if not re.search(pattern, str(args.get(key, ""))):
            return False
    if "path" in rule:
        if "path" not in args:
            return False
        globs = [rule["path"]] if isinstance(rule["path"], str) else rule["path"]
        path = normalize_path(args["path"])
        if not any(fnmatch.fnmatch(path, g) for g in globs):
            return False
    if "workspace_path" in rule:
        raw = str(args.get(rule["workspace_path"], "")).strip().replace("\\", "/")
        normalized = posixpath.normpath(raw)
        if normalized != "/workspace" and not normalized.startswith("/workspace/"):
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
    for part in _SPLIT.split(command):
        try:
            tokens = shlex.split(part)
        except ValueError:
            return True
        while tokens and tokens[0] in ("sudo", "command", "exec", "xargs"):
            tokens = tokens[1:]
        if not tokens or tokens[0] not in _DELETERS:
            continue
        targets = [t for t in tokens[1:] if not t.startswith("-")]
        if not targets:
            return True
        for target in targets:
            path = target if target.startswith("/tmp") else normalize_path(target)
            if path in (".", "..") or path.startswith("../") or "/../" in path:
                return True
            if not any(fnmatch.fnmatch(path, g) for g in SCRATCH_GLOBS):
                return True
    return False


class Policy:
    def __init__(self, project_rules: list[dict] | None = None, repo: bool = False):
        for rule in project_rules or []:
            if rule.get("action") not in (ALLOW, ASK, DENY):
                raise ValueError(f"policy rule needs action allow|ask|deny: {rule}")
        self.rules = list(project_rules or []) + (REPO_RULES if repo else []) + DEFAULT_RULES

    def decide(self, name: str, args: dict) -> Decision:
        for rule in self.rules:
            if _matches(rule, name, args):
                if name in ALWAYS_ASK and rule["action"] != DENY:
                    break
                return Decision(rule["action"], rule.get("reason", ""))
        if name in ALWAYS_ASK:
            return Decision(ASK, ALWAYS_ASK[name])
        if name in ("run_shell", "Bash") and _delete_outside_scratch(args.get("command", "")):
            return Decision(ASK, "deletes files outside the scratch area")
        return Decision(ALLOW)
