"""Checkpoint git state writes refuse links (#972), like the host git state copy (#966).

`put_git_state` puts HEAD and the index back after a failed rewind. Both live in the agent-writable workspace git
dir, so a link at either name, or at a directory above them, must be refused rather than written through.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from harness import checkpoints, projects
from harness.projects import GitError

BRANCHES = ["", "agent/x"]


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sh(ws, "init", "-q", "-b", "agent/x")
    sh(ws, "config", "user.name", "t")
    sh(ws, "config", "user.email", "t@t")
    (ws / "a.txt").write_bytes(b"a\n")
    sh(ws, "add", "-A")
    sh(ws, "commit", "-q", "-m", "init")
    return ws


def _state(ws: Path, branch: str) -> dict:
    """The state a rewind saves, then a workspace that differs from it: HEAD detached and a new file staged."""
    state = checkpoints.git_state(ws, branch)
    sh(ws, "checkout", "-q", "--detach")
    (ws / "b.txt").write_bytes(b"b\n")
    sh(ws, "add", "b.txt")
    assert (ws / ".git" / "HEAD").read_bytes() != state["head"]
    assert (ws / ".git" / "index").read_bytes() != state["index"]
    return state


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as e:  # Windows without symlink privilege
        pytest.skip(f"symlinks unavailable: {e}")


def _swap_for_link(directory: Path, outside: Path) -> bool:
    """Replace `directory` with a link to `outside`, as a concurrent workspace process would."""
    try:
        directory.rename(directory.with_name(directory.name + "-moved"))
    except PermissionError:
        return False
    _symlink_or_skip(directory, outside)
    return True


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("branch", BRANCHES)
def test_put_git_state_refuses_a_link_at_head(ws, tmp_path, branch):
    state = _state(ws, branch)
    outside = tmp_path / "outside-head"
    outside.write_bytes(b"outside\n")
    (ws / ".git" / "HEAD").unlink()
    _symlink_or_skip(ws / ".git" / "HEAD", outside)

    with pytest.raises(GitError, match="HEAD"):
        checkpoints.put_git_state(ws, state)

    assert outside.read_bytes() == b"outside\n"


@pytest.mark.parametrize("branch", BRANCHES)
def test_put_git_state_refuses_a_link_at_the_index(ws, tmp_path, branch):
    state = _state(ws, branch)
    outside = tmp_path / "outside-index"
    outside.write_bytes(b"outside\n")
    (ws / ".git" / "index").unlink()
    _symlink_or_skip(ws / ".git" / "index", outside)

    with pytest.raises(GitError, match="index"):
        checkpoints.put_git_state(ws, state)

    assert outside.read_bytes() == b"outside\n"


@pytest.mark.parametrize("branch", BRANCHES)
def test_put_git_state_refuses_removing_the_index_through_a_link(ws, tmp_path, branch):
    """A checkpoint taken before the first `git add` has no index: putting it back removes the index, but never a
    file a link at that name points to."""
    state = {**_state(ws, branch), "index": None}
    outside = tmp_path / "outside-index"
    outside.write_bytes(b"outside\n")
    (ws / ".git" / "index").unlink()
    _symlink_or_skip(ws / ".git" / "index", outside)

    with pytest.raises(GitError, match="index"):
        checkpoints.put_git_state(ws, state)

    assert outside.read_bytes() == b"outside\n"


def _swap_git_dir_after_last_check(monkeypatch, ws: Path, branch: str, outside: Path) -> list:
    """Swap the workspace git dir for a link to `outside` (a copy of it) right after the last check `put_git_state`
    makes before its writes: its own resolve when there is no branch, the isolated `update-ref` otherwise."""
    shutil.copytree(ws / ".git", outside)
    swapped: list = []
    name = "git" if branch else "_resolve_workspace_git"
    real = getattr(projects, name)

    def call(*args, **kwargs):
        result = real(*args, **kwargs)
        if not swapped:
            swapped.append(_swap_for_link(ws / ".git", outside))
        return result

    monkeypatch.setattr(projects, name, call)
    return swapped


@pytest.mark.parametrize("branch", BRANCHES)
@pytest.mark.parametrize("index", ["kept", "none"])
def test_put_git_state_refuses_a_git_dir_swapped_for_a_link(ws, tmp_path, monkeypatch, branch, index):
    state = _state(ws, branch)
    if index == "none":
        state["index"] = None
    outside = tmp_path / "outside-git"
    swapped = _swap_git_dir_after_last_check(monkeypatch, ws, branch, outside)
    before = _tree(outside)

    with pytest.raises(GitError):
        checkpoints.put_git_state(ws, state)

    assert swapped == [True]
    assert _tree(outside) == before


@pytest.mark.parametrize("branch", BRANCHES)
def test_put_git_state_refuses_a_directory_at_the_index(ws, branch):
    state = _state(ws, branch)
    (ws / ".git" / "index").unlink()
    (ws / ".git" / "index").mkdir()

    with pytest.raises(GitError, match="index"):
        checkpoints.put_git_state(ws, state)

    assert (ws / ".git" / "index").is_dir()


def test_put_git_state_fails_closed_without_link_safe_file_access(ws, monkeypatch):
    """A platform with neither descriptor-relative calls nor Windows handles cannot check the path: refuse."""
    state = _state(ws, "")
    head, index = (ws / ".git" / "HEAD").read_bytes(), (ws / ".git" / "index").read_bytes()
    monkeypatch.setattr(projects, "_FD_OPS", False)
    monkeypatch.setattr(projects, "_WIN_HANDLES", False)

    with pytest.raises(GitError):
        checkpoints.put_git_state(ws, state)

    assert (ws / ".git" / "HEAD").read_bytes() == head
    assert (ws / ".git" / "index").read_bytes() == index


def test_put_git_state_reports_a_failed_write_as_a_git_error(ws, monkeypatch):
    state = _state(ws, "")
    head = (ws / ".git" / "HEAD").read_bytes()

    def refuse(*_args, **_kwargs):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(GitError, match="denied"):
        checkpoints.put_git_state(ws, state)

    assert (ws / ".git" / "HEAD").read_bytes() == head
    assert sorted(p.name for p in (ws / ".git").iterdir() if p.name.startswith(".tmp-")) == []


@pytest.mark.parametrize("branch", BRANCHES)
def test_put_git_state_writes_head_and_index_back(ws, branch):
    state = _state(ws, branch)

    checkpoints.put_git_state(ws, state)

    assert (ws / ".git" / "HEAD").read_bytes() == state["head"]
    assert (ws / ".git" / "index").read_bytes() == state["index"]
    assert sh(ws, "status", "--porcelain") == "?? b.txt\n"
    probe = ws / ".git" / "probe"  # a plain new file gets the mode git gives one, not the temp file's private 0600
    probe.write_bytes(b"")
    assert (ws / ".git" / "HEAD").stat().st_mode & 0o777 == probe.stat().st_mode & 0o777


@pytest.mark.parametrize("branch", BRANCHES)
def test_put_git_state_removes_an_index_the_checkpoint_did_not_have(ws, branch):
    state = {**_state(ws, branch), "index": None}

    checkpoints.put_git_state(ws, state)

    assert (ws / ".git" / "HEAD").read_bytes() == state["head"]
    assert not (ws / ".git" / "index").exists()

    checkpoints.put_git_state(ws, state)  # no index to remove: nothing to do

    assert not (ws / ".git" / "index").exists()
