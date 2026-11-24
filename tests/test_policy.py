"""The approval policy as a decision table: (tool, args, project rules) -> allow / ask / deny, and why."""

from __future__ import annotations

import pytest
import shlex

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


# Every destructive family already covered by the default policy. These strings are only
# classified, never executed. The cross product checks the invariant across tool aliases too.
DESTRUCTIVE_SHELL = [
    ("rm", "rm -rf src", "rm src -fr"),
    ("rmdir", "rmdir src", "rmdir --parents src"),
    ("unlink", "unlink notes.md", "unlink -- notes.md"),
    ("shred", "shred -u notes.md", "shred --remove notes.md"),
    ("find-delete", "find . -name '*.log' -delete", "find . -delete -name '*.log'"),
    ("find-exec", "find . -exec rm {} +", "find . -exec /bin/rm -f {} +"),
    ("git-push", "git push origin HEAD", "git push --force origin HEAD"),
    ("git-reset", "git reset --hard HEAD", "git reset HEAD --hard"),
    ("git-clean", "git clean -fd", "git clean --force -d"),
]
SHELL_SPELLINGS = [
    ("plain", "{cmd}"),
    ("command", "command -- {cmd}"),
    ("exec", "exec {cmd}"),
    ("env", "env -i PATH=/usr/bin:/bin {cmd}"),
    ("sudo", "sudo -u root -- {cmd}"),
    ("xargs", "printf '' | xargs -r {cmd}"),
    ("timeout", "timeout --signal=TERM 10 {cmd}"),
    ("nice", "nice -n 5 {cmd}"),
    ("nohup", "nohup {cmd}"),
    ("stdbuf", "stdbuf -oL {cmd}"),
    ("nested-shell", "sh -c {quoted}"),
    ("login-shell", "/bin/bash -lc {quoted}"),
    ("subshell", "({cmd})"),
    ("brace-group", "{{ {cmd}; }}"),
    ("path", "/usr/bin/{cmd}"),
    ("flags", "{flags}"),
    ("working-directory", "cd sub && {cmd}"),
    ("wrapper-directory", "env --chdir=sub {cmd}"),
    ("composed", "env LC_ALL=C timeout 10 bash -c {quoted}"),
]
SHELL_TOOLS = ["run_shell", "exec_command", "Bash", "mcp__harness__run_shell", "mcp__harness__exec_command"]
RATING = {ALLOW: 0, ASK: 1, DENY: 2}


@pytest.mark.parametrize("tool", SHELL_TOOLS)
@pytest.mark.parametrize("repo", [False, True], ids=["folder", "repo"])
@pytest.mark.parametrize("spelling,template", SHELL_SPELLINGS, ids=[row[0] for row in SHELL_SPELLINGS])
@pytest.mark.parametrize("family,plain,flags", DESTRUCTIVE_SHELL, ids=[row[0] for row in DESTRUCTIVE_SHELL])
def test_destructive_shell_equivalence(tool, repo, spelling, template, family, plain, flags):
    policy = Policy(repo=repo)
    baseline = policy.decide(tool, {"command": plain}).action
    assert baseline == (DENY if repo and family == "git-push" else ASK)
    command = template.format(cmd=plain, quoted=shlex.quote(plain), flags=flags)
    actual = policy.decide(tool, {"command": command}).action
    assert RATING[actual] >= RATING[baseline], (spelling, command, baseline, actual)


@pytest.mark.parametrize("options", [
    "-C sub", "-Csub", "-c color.ui=false -C sub", "--git-dir=.git --work-tree=sub",
    "--git-dir .git --work-tree sub", "--namespace=work --no-pager",
    "--config-env=alias.test=GIT_TEST_CONFIG -C sub -C nested", "--bare", "--literal-pathspecs",
])
@pytest.mark.parametrize("tool", SHELL_TOOLS)
def test_repo_push_with_global_git_options(tool, options):
    assert Policy(repo=True).decide(tool, {"command": f"git {options} push origin HEAD"}).action == DENY


@pytest.mark.parametrize("command", [
    "env --unknown-option payload", "sudo --unknown-option payload", "timeout --unknown-option 10 payload",
    "xargs -I{} {}", "sh script.sh", "bash -c \"$COMMAND\"", "sh -s", "eval \"$COMMAND\"",
    "env -S '$COMMAND'", "command \"$COMMAND\"", "exec \"$COMMAND\"", "git --unknown-option payload",
    "sh -c 'unterminated", "env", "sudo", "timeout 10", "rm", "rm --files0-from=list",
])
@pytest.mark.parametrize("tool", ["run_shell", "exec_command"])
def test_opaque_shell_commands_ask(command, tool):
    decision = Policy().decide(tool, {"command": command})
    assert decision.action == ASK
    assert not decision.smart_eligible


@pytest.mark.parametrize("command", [
    "ls -la", "cat README.md", "echo rm is a word", "pytest -q", "python -m build", "npm run build",
    "git status --short", "git -C sub status", "git -c color.ui=false log --oneline",
    "git --git-dir=.git --work-tree=sub diff", "git commit -am x", "git reset --soft HEAD~1", "git clean -nd",
    "rm -rf __pycache__ .pytest_cache", "rm -f /tmp/out.txt", "rm -f build/out.txt dist/out.whl",
    "command -- ls", "env LC_ALL=C git status", "timeout 10 pytest -q", "sh -c 'git status'",
    "(git status)", "{ git status; }", "echo ready && python -m build", "python -m build >build.log",
])
@pytest.mark.parametrize("repo", [False, True])
@pytest.mark.parametrize("tool", ["run_shell", "exec_command"])
def test_harmless_shell_ratings_unchanged(command, repo, tool):
    assert Policy(repo=repo).decide(tool, {"command": command}).action == ALLOW
