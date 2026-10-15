"""The approval policy as a decision table: (tool, args, project rules) -> allow / ask / deny, and why."""

from __future__ import annotations

import pytest

from harness.policy import ALLOW, ASK, DENY, Policy

ALLOW_ALL = [{"tool": "*", "action": "allow"}]
SHELL_FOR_PYTEST = [{"tool": "run_shell", "args": {"command": "^pytest"}, "action": "allow"}]
NO_SHELL = [{"tool": "run_shell", "action": "deny", "reason": "no shell"}]
NO_MEMORY_WRITE = [{"tool": "memory_write", "action": "deny", "reason": "not here"}]

# (id, project rules, repo project, tool, args, action, reason or None when the row doesn't pin it)
DECISIONS = [
    # Tools the harness always asks about, whatever the project rules say.
    ("remote-control", [], False, "open_claude_remote_control", {"project": "x", "reason": "y"}, ASK, None),
    ("remote-control-allow-all", ALLOW_ALL, False, "open_claude_remote_control", {}, ASK, None),
    ("memory-edit", [], False, "memory_edit", {}, ASK, "changes your memory library"),
    ("memory-edit-allow-all", ALLOW_ALL, False, "memory_edit", {"path": "x"}, ASK, None),
    ("memory-write-denied", NO_MEMORY_WRITE, False, "memory_write", {}, DENY, "not here"),
    ("allow-all-still-allows-shell", ALLOW_ALL, False, "run_shell", {"command": "ls"}, ALLOW, None),
    ("restart-service", [], False, "restart_service", {"service": "grafana"}, ASK, None),
    ("rebuild-service", [], False, "rebuild_service", {"service": "plex-webhook"}, ASK, None),
    # Hosted Claude Code's shell over MCP is decided as the native tool it names.
    ("shell-over-mcp", [], False, "mcp__harness__run_shell", {"command": "ls"}, ALLOW, None),
    ("shell-over-mcp-denied-by-native-rule", NO_SHELL, False, "mcp__harness__run_shell", {"command": "ls"}, DENY,
     "no shell"),
    # Repo projects: the harness publishes the branch, so the agent may not push it.
    ("repo-git-push", [], True, "run_shell", {"command": "git push origin HEAD"}, DENY, None),
    ("repo-git-commit", [], True, "run_shell", {"command": "git commit -am x"}, ALLOW, None),
    ("repo-claude-git-push", [], True, "Bash", {"command": "git push origin main"}, DENY, None),
    ("repo-codex-git-push", [], True, "exec_command", {"command": "git push origin main"}, DENY, None),
    # Claude Code tools: writes stay in /workspace, a shell and the web are asked.
    ("claude-write-in-workspace", [], False, "Write", {"file_path": "/workspace/a.txt"}, ALLOW, None),
    ("claude-write-outside", [], False, "Write", {"file_path": "/etc/passwd"}, ASK, None),
    ("claude-write-dotdot", [], False, "Write", {"file_path": "/workspace/../etc/passwd"}, ASK, None),
    ("claude-bash", [], False, "Bash", {"command": "pytest -q"}, ASK, None),
    ("claude-bash-allowed-by-project-rule", SHELL_FOR_PYTEST, False, "Bash", {"command": "pytest -q"}, ALLOW, None),
    ("claude-web-fetch", [], False, "WebFetch", {"url": "https://example.com"}, ASK, None),
    # Codex tools.
    ("codex-exec", [], False, "exec_command", {"command": "pytest -q"}, ALLOW, None),
    ("codex-exec-network", [], False, "exec_command", {"command": "curl https://example.com", "network": True}, ASK,
     None),
    ("codex-patch-in-workspace", [], False, "apply_patch", {"file_paths": ["/workspace/a.py"]}, ALLOW, None),
    ("codex-patch-outside", [], False, "apply_patch", {"file_paths": ["/workspace/a.py", "/etc/passwd"]}, ASK, None),
]


@pytest.mark.parametrize("rules, repo, tool, args, action, reason", [row[1:] for row in DECISIONS],
                         ids=[row[0] for row in DECISIONS])
def test_decision_table(rules, repo, tool, args, action, reason):
    decision = Policy(rules, repo=repo).decide(tool, args)
    assert decision.action == action
    if reason is not None:
        assert decision.reason == reason
