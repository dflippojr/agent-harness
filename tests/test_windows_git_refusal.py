"""Windows refuses host git operations on workspaces (#981, step 2 of #973).

The host git state copy (`projects._StateDir`) is link-safe on POSIX through descriptor-relative calls. Windows has no
handle-relative layer for it yet (#973 step 3), so every host git operation on a workspace that relies on the copy, and
every entry that would write to a workspace before its first such operation, refuses there before touching anything.

The tests set `projects._REFUSE_HOST_GIT`, so they assert the refusal on every OS; on Windows the flag is already set.
`test_the_refusal_is_on_exactly_on_windows` reads the real flag: on CI's Windows jobs it asserts the live refusal, and
everywhere else that POSIX still runs host git.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from harness import checkpoints, clone, projects
from harness.checkpointer import Checkpointer
from harness.config import Project
from harness.maintenance import Maintenance
from harness.projects import GitError

REFUSAL = "aren't supported on Windows"
SID = "0123456789"


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def make_repo(path: Path, branch: str = "main") -> Path:
    """A repository built with plain git, which the refusal does not cover (no workspace state copy)."""
    path.mkdir(parents=True)
    sh(path, "init", "-q", "-b", branch)
    sh(path, "config", "user.name", "t")
    sh(path, "config", "user.email", "t@t")
    (path / "a.txt").write_bytes(b"a\n")
    sh(path, "add", "-A")
    sh(path, "commit", "-q", "-m", "init")
    return path


def tree(root: Path) -> dict[str, bytes | None]:
    """Every entry under `root` with its bytes (None for a directory), to show that nothing changed."""
    if not root.exists():
        return {}
    return {p.relative_to(root).as_posix(): None if p.is_dir() else p.read_bytes() for p in sorted(root.rglob("*"))}


@pytest.fixture
def refuse(monkeypatch, tmp_path):
    """Turn the Windows refusal on, and give tempfile a fresh directory so a temp git dir left behind shows."""
    monkeypatch.setattr(projects, "_REFUSE_HOST_GIT", True, raising=False)
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(projects, "TEMP_ROOT", temp)
    return temp


@pytest.fixture
def ws(tmp_path):
    return make_repo(tmp_path / "ws", branch="agent/s1")


def test_the_refusal_is_on_exactly_on_windows(ws):
    before = tree(ws)
    if sys.platform == "win32":
        with pytest.raises(GitError, match=REFUSAL):
            projects.git(ws, "status", "--porcelain")
        assert tree(ws) == before
    else:
        assert projects.git(ws, "status", "--porcelain").code == 0


@pytest.mark.parametrize("args", [("status", "--porcelain"), ("update-ref", "refs/heads/x", "HEAD")])
def test_isolated_git_refuses_before_touching_the_workspace(refuse, ws, args):
    before = tree(ws)
    with pytest.raises(GitError, match=REFUSAL):
        projects.git(ws, *args)
    assert tree(ws) == before
    assert tree(refuse) == {}  # no temp git dir was made


def test_isolated_git_without_check_reports_the_refusal(refuse, ws):
    before = tree(ws)
    result = projects.git(ws, "status", "--porcelain", check=False)
    assert result.code != 0 and REFUSAL in result.err
    assert tree(ws) == before


def test_isolated_git_dir_refuses_before_its_lock_and_temp_dir(refuse, ws):
    before = tree(ws)
    with pytest.raises(GitError, match=REFUSAL):
        with projects.isolated_git_dir(ws, {}):
            pytest.fail("the body must not run")
    assert tree(ws) == before
    assert tree(refuse) == {}


def test_the_state_copy_itself_refuses(refuse, ws, tmp_path):
    dest = tmp_path / "copy" / "git"
    dest.parent.mkdir()
    before = tree(ws)
    with pytest.raises(GitError, match=REFUSAL):
        projects._copy_git_state(ws / ".git", dest)
    assert tree(ws) == before and not dest.exists()


@pytest.mark.parametrize("data", [b"ref: refs/heads/other\n", None])
def test_put_state_file_refuses(refuse, ws, data):
    before = tree(ws)
    with pytest.raises(GitError, match=REFUSAL):
        projects.put_state_file(ws / ".git", "HEAD", data)
    assert tree(ws) == before


def test_the_state_dir_refuses_to_open(refuse, ws):
    with pytest.raises(GitError, match=REFUSAL):
        projects._StateDir.open(ws / ".git")


def test_prepare_refuses_before_cloning(refuse, tmp_path):
    src = make_repo(tmp_path / "src")
    workspace = tmp_path / "workspaces" / "s1"
    with pytest.raises(GitError, match=REFUSAL):
        projects.prepare(Project(name="p", repo=str(src)), workspace, "s1")
    assert not workspace.exists()


def test_member_prepare_refuses_before_cloning(refuse, tmp_path):
    src = make_repo(tmp_path / "src")
    root = tmp_path / "workspaces"
    root.mkdir()
    with pytest.raises(GitError, match=REFUSAL):
        clone.isolated_prepare(root / "s1", src, "s1", root)
    assert tree(root) == {}


def test_member_refresh_reports_the_refusal(refuse, tmp_path):
    root = tmp_path / "workspaces"
    workspace = make_repo(root / "s1")
    before = tree(workspace)
    source = make_repo(root / "src")  # account-local, so the refresh would otherwise fetch
    assert REFUSAL in clone.isolated_refresh_origin(workspace, source, root)
    assert tree(workspace) == before


@pytest.mark.parametrize("call", [
    lambda ws: projects.snapshot(ws, "message"),
    lambda ws: projects.head(ws),
    lambda ws: projects.commits_ahead(ws, "HEAD~1"),
    lambda ws: projects.refresh_origin(Project(name="p", repo=str(ws)), ws),
], ids=["snapshot", "head", "commits_ahead", "refresh_origin"])
def test_session_git_operations_refuse(refuse, ws, call):
    (ws / "new.txt").write_bytes(b"uncommitted\n")
    before = tree(ws)
    with pytest.raises(GitError, match=REFUSAL):
        call(ws)
    assert tree(ws) == before


def test_commits_ahead_does_not_read_a_refusal_as_no_commits(refuse, ws):
    """Cleanup deletes a URL project's workspace when `commits_ahead` is empty; a refusal must not look empty."""
    with pytest.raises(GitError, match=REFUSAL):
        projects.commits_ahead(ws, "HEAD")


@pytest.mark.parametrize("call", [
    lambda ws: checkpoints.git_state(ws, "agent/s1"),
    lambda ws: checkpoints.put_git_state(ws, {"head": b"x\n", "index": None, "branch": "", "tip": ""}),
    lambda ws: checkpoints.reset_branch(ws, sh(ws, "rev-parse", "HEAD").strip(), "agent/s1"),
    lambda ws: checkpoints.head_and_branch(ws),
], ids=["git_state", "put_git_state", "reset_branch", "head_and_branch"])
def test_checkpoint_git_operations_refuse(refuse, ws, call):
    before = tree(ws)
    with pytest.raises(GitError, match=REFUSAL):
        call(ws)
    assert tree(ws) == before


def test_a_refused_workspace_is_not_read_as_a_scratch_workspace(refuse, ws):
    """`head_and_branch` answers ("", "") for a workspace without git; a refused one must raise instead, so a
    checkpoint is skipped rather than recorded without its commit."""
    with pytest.raises(GitError, match=REFUSAL):
        checkpoints.head_and_branch(ws)


def test_fork_refuses_before_creating_the_workspace(refuse, ws, tmp_path):
    parent = {"id": "s1", "workspace": str(ws), "base_branch": "main", "base_commit": ""}
    fork = tmp_path / "workspaces" / "s2"
    ckpt = {"head": sh(ws, "rev-parse", "HEAD").strip(), "sha": "0" * 40}
    with pytest.raises(GitError, match=REFUSAL):
        Checkpointer._fork_clone(parent, ckpt, "s2", fork)
    assert not fork.exists()


def test_cleanup_keeps_a_workspace_it_cannot_check(refuse, ws, monkeypatch):
    from harness import catalog

    project = Project(name="p", repo="https://example.invalid/r.git")
    monkeypatch.setattr(catalog, "get_project", lambda *_a: project)
    maintenance = object.__new__(Maintenance)
    maintenance.cfg = maintenance.db = None
    base = sh(ws, "rev-parse", "HEAD").strip()
    s = {"id": "s1", "workspace": str(ws), "project": "p", "base_commit": base, "branch": "agent/s1"}
    assert REFUSAL in maintenance.unsaved_work({**s, "review": ""})         # never pushed: commits_ahead
    assert maintenance.unsaved_work({**s, "review": "pushed"})              # pushed: the unpushed check


def test_runner_cleanup_keeps_a_workspace_it_cannot_check(refuse, tmp_path):
    from macrunner import harness_runner

    (tmp_path / "workspaces").mkdir()
    executor = harness_runner.Executor(workspaces=tmp_path / "workspaces", repo_roots=[], profile=None,
                                       home=tmp_path / "home", min_free_gb=0)
    workspace = make_repo(executor.workspace(SID), branch=f"agent/{SID}")
    base = sh(workspace, "rev-parse", "HEAD").strip()
    before = tree(workspace)
    with pytest.raises(GitError, match=REFUSAL):
        executor.op_cleanup_workspace({"session": SID, "repo": "https://example.invalid/r.git",
                                       "branch": f"agent/{SID}", "base_commit": base, "review": ""})
    assert tree(workspace) == before
