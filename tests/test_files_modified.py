"""files_modified is derived from commits since the run-start HEAD plus the worktree (#156)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from harness.state import FILES_MODIFIED_MAX, files_modified_since, snapshot_git_baseline


def _sh(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True, text=True)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "README").write_text("init\n", encoding="utf-8")
    _sh(path, "add", "README")
    _sh(path, "commit", "-qm", "init")
    return path


def test_edit_then_commit_lists_files(tmp_path):
    repo = _repo(tmp_path / "r")
    baseline = snapshot_git_baseline(repo)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    _sh(repo, "add", "a.py", "b.py")
    _sh(repo, "commit", "-qm", "work")
    paths = files_modified_since(repo, baseline)
    assert paths is not None
    assert "a.py" in paths
    assert "b.py" in paths


def test_edit_without_commit_lists_files(tmp_path):
    repo = _repo(tmp_path / "r")
    baseline = snapshot_git_baseline(repo)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "tracked.py").write_text("t\n", encoding="utf-8")
    _sh(repo, "add", "tracked.py")
    paths = files_modified_since(repo, baseline)
    assert paths is not None
    assert "a.py" in paths
    assert "tracked.py" in paths


def test_baseline_dirty_only_if_content_changed(tmp_path):
    repo = _repo(tmp_path / "r")
    (repo / "stale.py").write_text("old\n", encoding="utf-8")
    (repo / "dirty.py").write_text("old\n", encoding="utf-8")
    baseline = snapshot_git_baseline(repo)
    assert "stale.py" in baseline["dirty"]
    assert "dirty.py" in baseline["dirty"]
    (repo / "dirty.py").write_text("new\n", encoding="utf-8")
    paths = files_modified_since(repo, baseline)
    assert paths is not None
    assert "dirty.py" in paths
    assert "stale.py" not in paths


def test_baseline_dirty_committed_unchanged_is_not_listed(tmp_path):
    repo = _repo(tmp_path / "r")
    (repo / "stale.py").write_text("old\n", encoding="utf-8")
    baseline = snapshot_git_baseline(repo)
    _sh(repo, "add", "stale.py")
    _sh(repo, "commit", "-qm", "keep")
    paths = files_modified_since(repo, baseline)
    assert paths is not None
    assert "stale.py" not in paths


def test_deleted_and_renamed_files(tmp_path):
    repo = _repo(tmp_path / "r")
    (repo / "gone.py").write_text("g\n", encoding="utf-8")
    (repo / "old.py").write_text("o\n", encoding="utf-8")
    _sh(repo, "add", "gone.py", "old.py")
    _sh(repo, "commit", "-qm", "add")
    baseline = snapshot_git_baseline(repo)
    _sh(repo, "rm", "gone.py")
    _sh(repo, "mv", "old.py", "new.py")
    uncommitted = files_modified_since(repo, baseline)
    assert uncommitted is not None
    assert "gone.py" in uncommitted
    assert "old.py" in uncommitted
    assert "new.py" in uncommitted
    _sh(repo, "commit", "-qm", "move")
    committed = files_modified_since(repo, baseline)
    assert committed is not None
    assert "gone.py" in committed
    assert "old.py" in committed
    assert "new.py" in committed


def test_non_git_workspace_returns_none(tmp_path):
    assert snapshot_git_baseline(tmp_path) is None
    assert files_modified_since(tmp_path, {"head": None, "dirty": {}}) is None


def test_files_modified_is_length_capped(tmp_path):
    repo = _repo(tmp_path / "r")
    baseline = snapshot_git_baseline(repo)
    for i in range(FILES_MODIFIED_MAX + 5):
        (repo / f"f{i}.txt").write_text("x", encoding="utf-8")
    paths = files_modified_since(repo, baseline)
    assert paths is not None
    assert len(paths) == FILES_MODIFIED_MAX


def test_first_commit_from_unborn_head_is_listed(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    baseline = snapshot_git_baseline(repo)
    assert baseline is not None
    assert baseline["head"] is None
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    _sh(repo, "add", "a.py")
    _sh(repo, "commit", "-qm", "first")
    paths = files_modified_since(repo, baseline)
    assert paths is not None
    assert "a.py" in paths
