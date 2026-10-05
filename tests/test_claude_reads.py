"""Hosted Claude Code reads stay inside /workspace (#370). Synthetic paths only; no real login volume is touched."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.manager import Manager
from harness.policy import ALLOW, ASK, DENY, Policy
from test_daemon import make_cfg


def _link_dir(link: Path, target: Path) -> None:
    """A directory symlink, or a Windows junction where symlinks need privileges the test may not have."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
        return
    pytest.skip("can't create a directory link here")


@pytest.mark.parametrize("name,args", [
    ("Read", {"file_path": "/workspace/src/a.py"}),
    ("Read", {"file_path": "src/a.py"}),
    ("Grep", {"pattern": "TODO"}),
    ("Grep", {"pattern": "TODO", "path": ""}),
    ("Glob", {"pattern": "**/*.py", "path": "/workspace/src"}),
    ("Glob", {"pattern": "**/*.py"}),
    ("LS", {"path": "/workspace"}),
])
def test_reads_inside_workspace_are_allowed(name, args):
    assert Policy().decide(name, args).action == ALLOW


@pytest.mark.parametrize("name,args", [
    ("Read", {"file_path": "/home/agent/.claude/projects/-workspace/x.jsonl"}),
    ("Read", {"file_path": "/workspace/../home/agent/.claude/.credentials.json"}),
    ("Read", {"file_path": "~/.claude/.credentials.json"}),
    ("Read", {"file_path": "$HOME/.claude/.credentials.json"}),
    ("Read", {"file_path": "../home/agent/.claude/.credentials.json"}),
    ("Read", {"file_path": "/workspacex/a.py"}),
    ("Read", {"file_path": "/workspace/src/../a.py"}),
    ("Read", {}),
    ("Grep", {"pattern": "token", "path": "/home/agent"}),
    ("LS", {"path": "/"}),
    ("Glob", {"pattern": "/home/agent/.claude/**"}),
    ("Glob", {"pattern": "../home/agent/**", "path": "/workspace"}),
    ("Glob", {"pattern": "**", "path": "/home/agent/.claude"}),
])
def test_reads_outside_workspace_are_asked(name, args):
    decision = Policy().decide(name, args)
    assert decision.action == ASK
    assert decision.reason == "reads a file outside /workspace"
    assert not decision.smart_eligible


def test_read_through_a_link_in_the_host_workspace_is_asked(tmp_path):
    workspace, outside = tmp_path / "ws", tmp_path / "login-volume"
    (workspace / "src").mkdir(parents=True)
    outside.mkdir()
    (outside / "x").write_text("secret")
    (workspace / "src" / "a.py").write_text("print(1)")
    _link_dir(workspace / "link", outside)
    policy = Policy(workspace_root=workspace)

    assert policy.decide("Read", {"file_path": "/workspace/src/a.py"}).action == ALLOW
    assert policy.decide("Read", {"file_path": "/workspace/missing/new.py"}).action == ALLOW
    assert policy.decide("Grep", {"pattern": "x"}).action == ALLOW
    assert policy.decide("Read", {"file_path": "/workspace/link/x"}).action == ASK
    assert policy.decide("Read", {"file_path": "/workspace/link"}).action == ASK
    assert policy.decide("Read", {"file_path": "/workspace/link/../.credentials.json"}).action == ASK
    assert policy.decide("Glob", {"pattern": "link/../*.json"}).action == ASK
    # A wildcard folder may be the link, so a literal folder after one can't be checked.
    assert policy.decide("Glob", {"pattern": "**/link/x"}).action == ASK
    assert policy.decide("Glob", {"pattern": "*/x/*.jsonl"}).action == ASK
    assert policy.decide("Glob", {"pattern": "src/**/*.py"}).action == ALLOW
    assert policy.decide("Glob", {"pattern": "**/package.json"}).action == ALLOW
    # Read, Grep and LS paths are literal: glob characters in a folder name don't end the walk.
    (workspace / "[x]").mkdir()
    _link_dir(workspace / "[x]" / "link", outside)
    assert policy.decide("Read", {"file_path": "/workspace/[x]/link/x"}).action == ASK
    assert policy.decide("Grep", {"pattern": "x", "path": "/workspace/[x]/link"}).action == ASK
    assert policy.decide("Read", {"file_path": "/workspace/[x]/other.py"}).action == ALLOW
    assert policy.decide("Grep", {"pattern": "x", "path": "/workspace/link"}).action == ASK
    assert policy.decide("LS", {"path": "/workspace/link"}).action == ASK
    assert policy.decide("Glob", {"pattern": "link/**"}).action == ASK
    assert policy.decide("Glob", {"pattern": "**", "path": "/workspace/link"}).action == ASK
    # Without the host root only the lexical check can run.
    assert Policy().decide("Read", {"file_path": "/workspace/link/x"}).action == ALLOW


def test_project_rules_still_win_for_outside_paths():
    allow = Policy([{"tool": "Read", "args": {"file_path": r"^/opt/docs/"}, "action": "allow"}])
    assert allow.decide("Read", {"file_path": "/opt/docs/readme.md"}).action == ALLOW
    assert allow.decide("Read", {"file_path": "/opt/other"}).action == ASK
    deny = Policy([{"tool": "Read", "args": {"file_path": r"secret"}, "action": "deny", "reason": "no"}])
    assert deny.decide("Read", {"file_path": "/workspace/secret.txt"}).action == DENY


def test_runner_policy_checks_the_session_host_workspace(tmp_path):
    m = Manager(make_cfg(tmp_path))
    s = {"id": "s1", "kind": "agent", "project": "", "workspace": str(tmp_path / "ws")}
    policy = m.runner.policy(s)
    assert isinstance(policy, Policy) and policy.workspace_root == tmp_path / "ws"
