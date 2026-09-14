"""Approval policy: decides allow / ask / deny for each tool call.

This is a usability gate, not the security boundary; the sandbox is. Shell classification is heuristic and
errs toward asking.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass

from .tools import normalize_path

ALLOW, ASK, DENY = "allow", "ask", "deny"

# Paths an agent may delete without asking (matched against normalized paths; /tmp is container-local).
SCRATCH_GLOBS = ["/tmp", "/tmp/*", "scratch", "scratch/*", "*__pycache__*", "*.pyc", ".pytest_cache*",
                 "*/.pytest_cache*", "build", "build/*", "dist", "dist/*", "*.egg-info", "*.egg-info/*"]

DEFAULT_RULES: list[dict] = [
    {"tool": "run_shell", "network": True, "action": ASK, "reason": "command needs network access"},
    {"tool": "run_shell", "args": {"command": r"\bgit\s+push\b"}, "action": ASK, "reason": "git push"},
    {"tool": "run_shell", "args": {"command": r"\bgit\s+(reset\s+--hard|clean\s+-\w*f)"}, "action": ASK,
     "reason": "discards uncommitted work"},
    {"tool": "git_clone", "args": {"url": r"^(local:|https://(github\.com|gitlab\.com|codeberg\.org)/)"},
     "action": ALLOW},
    {"tool": "git_clone", "action": ASK, "reason": "clone from a host that isn't on the allowlist"},
]


@dataclass
class Decision:
    action: str
    reason: str = ""


def _matches(rule: dict, name: str, args: dict) -> bool:
    tools = rule.get("tool", "*")
    tools = [tools] if isinstance(tools, str) else tools
    if "*" not in tools and name not in tools:
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
    def __init__(self, project_rules: list[dict] | None = None):
        for rule in project_rules or []:
            if rule.get("action") not in (ALLOW, ASK, DENY):
                raise ValueError(f"policy rule needs action allow|ask|deny: {rule}")
        self.rules = list(project_rules or []) + DEFAULT_RULES

    def decide(self, name: str, args: dict) -> Decision:
        for rule in self.rules:
            if _matches(rule, name, args):
                return Decision(rule["action"], rule.get("reason", ""))
        if name == "run_shell" and _delete_outside_scratch(args.get("command", "")):
            return Decision(ASK, "deletes files outside the scratch area")
        return Decision(ALLOW)
