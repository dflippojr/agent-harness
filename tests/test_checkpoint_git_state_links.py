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
from host_git import needs_host_git

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
@needs_host_git
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
@needs_host_git
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
@needs_host_git
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
@needs_host_git
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
@needs_host_git
def test_put_git_state_refuses_a_directory_at_the_index(ws, branch):
    state = _state(ws, branch)
    (ws / ".git" / "index").unlink()
    (ws / ".git" / "index").mkdir()

    with pytest.raises(GitError, match="index"):
        checkpoints.put_git_state(ws, state)

    assert (ws / ".git" / "index").is_dir()


@needs_host_git
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


@needs_host_git
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
@needs_host_git
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
@needs_host_git
def test_put_git_state_removes_an_index_the_checkpoint_did_not_have(ws, branch):
    state = {**_state(ws, branch), "index": None}

    checkpoints.put_git_state(ws, state)

    assert (ws / ".git" / "HEAD").read_bytes() == state["head"]
    assert not (ws / ".git" / "index").exists()

    checkpoints.put_git_state(ws, state)  # no index to remove: nothing to do

    assert not (ws / ".git" / "index").exists()


@pytest.mark.parametrize("branch", BRANCHES)
@needs_host_git
def test_put_git_state_never_removes_a_directory_raced_in_at_the_index(ws, monkeypatch, branch):
    """Putting back an absent index removes a regular file only: a directory that replaces the index right after the
    type check is refused, not removed with its contents."""
    state = {**_state(ws, branch), "index": None}
    index = ws / ".git" / "index"
    real_kind = projects._StateDir.kind
    swapped = []

    def kind(self, name):
        result = real_kind(self, name)
        if name == "index" and not swapped:
            index.unlink()
            index.mkdir()
            (index / "keep").write_bytes(b"keep\n")
            swapped.append(True)
        return result

    monkeypatch.setattr(projects._StateDir, "kind", kind)
    with pytest.raises(GitError, match="index"):
        checkpoints.put_git_state(ws, state)

    assert swapped == [True]
    assert (index / "keep").read_bytes() == b"keep\n"


@pytest.mark.parametrize("branch", BRANCHES)
@needs_host_git
def test_put_git_state_keeps_an_owner_only_mode(ws, branch):
    state = _state(ws, branch)
    for name in ("HEAD", "index"):
        (ws / ".git" / name).chmod(0o600)
    want = {name: (ws / ".git" / name).stat().st_mode & 0o777 for name in ("HEAD", "index")}

    checkpoints.put_git_state(ws, state)

    assert {name: (ws / ".git" / name).stat().st_mode & 0o777 for name in ("HEAD", "index")} == want
    assert (ws / ".git" / "HEAD").read_bytes() == state["head"]


@pytest.mark.parametrize("branch", BRANCHES)
@needs_host_git
def test_put_git_state_fills_owner_only_temp_files(ws, monkeypatch, branch):
    """The temp file is owner-only while the content goes in; the destination's mode is applied after, and a file
    that did not exist stays owner-only."""
    state = _state(ws, branch)
    (ws / ".git" / "index").unlink()
    modes = []
    real_write_all = projects._write_all

    def write_all(fd, data):
        if bytes(data) in (state["head"], state["index"]):  # not the isolated git call's own state copy
            modes.append(os.fstat(fd).st_mode & 0o777)
        return real_write_all(fd, data)

    monkeypatch.setattr(projects, "_write_all", write_all)
    checkpoints.put_git_state(ws, state)

    assert len(modes) == 2
    assert (ws / ".git" / "index").read_bytes() == state["index"]
    if os.name == "posix":  # Windows has no group or other mode bits
        assert modes == [0o600, 0o600]
        assert (ws / ".git" / "index").stat().st_mode & 0o777 == 0o600


@needs_host_git
def test_put_git_state_refuses_removing_a_directory_at_the_index(ws):
    state = {**_state(ws, ""), "index": None}
    index = ws / ".git" / "index"
    index.unlink()
    index.mkdir()
    (index / "keep").write_bytes(b"keep\n")

    with pytest.raises(GitError, match="index"):
        checkpoints.put_git_state(ws, state)

    assert (index / "keep").read_bytes() == b"keep\n"


@pytest.mark.parametrize("branch", BRANCHES)
@needs_host_git
def test_put_git_state_keeps_a_directory_that_took_the_temp_name_after_a_failed_replace(ws, monkeypatch, branch):
    """When renaming the temp file over HEAD fails, cleanup removes the temp file only: a directory that took the
    temp name keeps its contents, and the original error is the one reported."""
    state = _state(ws, branch)
    real_write_all, real_replace = projects._write_all, os.replace
    armed, taken = [], []

    def write_all(fd, data):
        if bytes(data) == state["head"]:  # put_git_state's own HEAD write, not the isolated git call's state copy
            armed.append(True)
        return real_write_all(fd, data)

    def replace(src, dst, *args, **kwargs):
        if armed and not taken:
            temp = ws / ".git" / os.path.basename(os.fsdecode(src))
            temp.unlink()
            temp.mkdir()
            (temp / "keep").write_bytes(b"keep\n")
            taken.append(temp)
            raise PermissionError(13, "replace failed")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(projects, "_write_all", write_all)
    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(GitError, match="replace failed"):
        checkpoints.put_git_state(ws, state)

    assert len(taken) == 1
    assert (taken[0] / "keep").read_bytes() == b"keep\n"


@needs_host_git
def test_state_dir_remove_never_removes_a_directory(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "keep").write_bytes(b"keep\n")
    (tmp_path / "f").write_bytes(b"f\n")

    with projects._StateDir.open(projects._resolve(tmp_path)) as d:
        with pytest.raises(GitError, match="directory"):
            d.remove("d")
        d.remove("f")
        d.remove("missing")

    assert (tmp_path / "d" / "keep").read_bytes() == b"keep\n"
    assert not (tmp_path / "f").exists()


@needs_host_git
def test_copy_git_state_turns_a_ref_directory_into_a_ref_file(tmp_path):
    """Git's directory/file switch (refs/heads/a/b deleted, refs/heads/a created) still reaches the workspace: the
    stale ref directory is removed on purpose, through `remove_tree`."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "refs" / "heads").mkdir(parents=True)
    (src / "HEAD").write_bytes(b"ref: refs/heads/a\n")
    (src / "refs" / "heads" / "a").write_bytes(b"aaa\n")
    (dst / "refs" / "heads" / "a" / "b").mkdir(parents=True)
    (dst / "refs" / "heads" / "a" / "b" / "c").write_bytes(b"old\n")

    projects._copy_git_state(src, dst)

    assert (dst / "refs" / "heads" / "a").read_bytes() == b"aaa\n"
