"""Checkpoint store (#261): exact restore, untouched branch and index, ignored files, CRLF."""
import subprocess
from pathlib import Path

import pytest

from harness import checkpoints, projects


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sh(ws, "init", "-q", "-b", "agent/x")
    sh(ws, "config", "user.name", "t")
    sh(ws, "config", "user.email", "t@t")
    (ws / ".gitignore").write_text("ignored/\n")
    (ws / "keep.txt").write_text("keep\n")
    (ws / "edit.txt").write_text("v1\n")
    (ws / "gone.txt").write_text("gone\n")
    sh(ws, "add", "-A")
    sh(ws, "commit", "-q", "-m", "init")
    return ws, checkpoints.Store(tmp_path / "ckpt")


def test_rewind_restores_tracked_untracked_deleted(repo):
    ws, store = repo
    (ws / "edit.txt").write_text("v2\n")           # tracked, modified
    (ws / "new").mkdir()
    (ws / "new" / "u.txt").write_text("untracked\n")  # untracked
    (ws / "gone.txt").unlink()                      # deleted
    sha = store.snapshot(ws, "x", 1, *checkpoints.head_and_branch(ws))
    (ws / "edit.txt").write_text("v3 later\n")
    (ws / "new" / "u.txt").unlink()
    (ws / "late.txt").write_text("late\n")
    (ws / "gone.txt").write_text("back\n")
    assert store.restore(ws, sha) == []
    assert (ws / "edit.txt").read_text() == "v2\n"
    assert (ws / "new" / "u.txt").read_text() == "untracked\n"
    assert not (ws / "gone.txt").exists() and not (ws / "late.txt").exists()
    assert (ws / "keep.txt").read_text() == "keep\n"


def test_snapshot_leaves_branch_index_and_refs_alone(repo):
    ws, store = repo
    (ws / "edit.txt").write_text("v2\n")
    (ws / "staged.txt").write_text("s\n")
    sh(ws, "add", "staged.txt")
    before = (sh(ws, "rev-parse", "HEAD"), sh(ws, "status", "--porcelain"), sh(ws, "for-each-ref"),
              (ws / ".git" / "index").read_bytes())
    store.snapshot(ws, "x", 1, "", "")
    after = (sh(ws, "rev-parse", "HEAD"), sh(ws, "status", "--porcelain"), sh(ws, "for-each-ref"),
             (ws / ".git" / "index").read_bytes())
    assert before == after
    assert "refs/harness/checkpoints/x/1" in sh(store.repo, "for-each-ref")
    assert "refs/harness" not in sh(ws, "for-each-ref")


def test_ignored_files_are_neither_saved_nor_removed(repo):
    ws, store = repo
    (ws / "ignored").mkdir()
    (ws / "ignored" / "venv.bin").write_text("x")
    sha = store.snapshot(ws, "x", 1, "", "")
    (ws / "ignored" / "venv.bin").write_text("changed")
    assert store.restore(ws, sha) == []
    assert (ws / "ignored" / "venv.bin").read_text() == "changed"
    assert "venv.bin" not in sh(store.repo, "ls-tree", "-r", "--name-only", sha)


def test_crlf_round_trips_byte_exact(repo):
    ws, store = repo
    (ws / "win.txt").write_bytes(b"a\r\nb\r\n")
    sha = store.snapshot(ws, "x", 1, "", "")
    (ws / "win.txt").write_bytes(b"other\n")
    assert store.restore(ws, sha) == []
    assert (ws / "win.txt").read_bytes() == b"a\r\nb\r\n"


def test_reset_branch_rewinds_commits(repo):
    ws, store = repo
    head, branch = checkpoints.head_and_branch(ws)
    sha = store.snapshot(ws, "x", 1, head, branch)
    (ws / "edit.txt").write_text("committed later\n")
    sh(ws, "commit", "-qam", "later")
    checkpoints.reset_branch(ws, head, branch)
    assert sh(ws, "rev-parse", "HEAD").strip() == head
    assert store.restore(ws, sha) == []
    assert (ws / "edit.txt").read_text() == "v1\n"


def test_agent_tampering_with_workspace_git_cannot_touch_store(repo):
    ws, store = repo
    sha = store.snapshot(ws, "x", 1, "", "")
    import shutil
    shutil.rmtree(ws / ".git" / "refs")
    assert store.restore(ws, sha) == []
    assert "refs/harness/checkpoints/x/1" in sh(store.repo, "for-each-ref")


def test_scratch_workspace_without_git(tmp_path):
    ws = tmp_path / "scratch"
    (ws / "d").mkdir(parents=True)
    (ws / "d" / "a.txt").write_text("a")
    store = checkpoints.Store(tmp_path / "c")
    sha = store.snapshot(ws, "s", 1, "", "")
    (ws / "d" / "a.txt").write_text("b")
    (ws / "z.txt").write_text("z")
    assert store.restore(ws, sha) == []
    assert (ws / "d" / "a.txt").read_text() == "a" and not (ws / "z.txt").exists()


def test_head_and_branch_reads_the_repository_files_without_git(repo, monkeypatch):
    ws, _ = repo
    want = sh(ws, "rev-parse", "HEAD").strip()
    calls = []
    real = projects.git
    monkeypatch.setattr(projects, "git", lambda *a, **k: calls.append(a) or real(*a, **k))
    assert checkpoints.head_and_branch(ws) == (want, "agent/x")
    assert calls == []              # a git process costs about 0.4 s here, and a snapshot runs every turn
    sh(ws, "pack-refs", "--all")    # the branch now lives in packed-refs only
    assert checkpoints.head_and_branch(ws) == (want, "agent/x") and calls == []
    sh(ws, "checkout", "-q", "--detach")
    assert checkpoints.head_and_branch(ws) == (want, "") and calls == []


def test_head_and_branch_asks_git_for_what_it_cannot_read(repo, monkeypatch):
    ws, _ = repo
    calls = []
    real = projects.git
    monkeypatch.setattr(projects, "git", lambda *a, **k: calls.append(a) or real(*a, **k))
    monkeypatch.setattr(checkpoints, "_read_head", lambda *_: None)
    assert checkpoints.head_and_branch(ws)[1] == "agent/x" and len(calls) == 1


def test_snapshots_reuse_the_index_and_drop_files_that_became_ignored(repo):
    ws, store = repo
    first = store.snapshot(ws, "x", 1, "", "")
    assert (store.base / "index").is_file()
    (ws / "edit.txt").write_text("changed")
    (ws / "new.txt").write_text("n")
    second = store.snapshot(ws, "x", 2, "", "")
    names = sh(store.repo.parent, "--git-dir", str(store.repo), "ls-tree", "-r", "--name-only", second).split()
    assert "new.txt" in names and first != second
    (ws / ".gitignore").write_text("new.txt\n")                 # tracked in the kept index, ignored from now on
    third = store.snapshot(ws, "x", 3, "", "")
    names = sh(store.repo.parent, "--git-dir", str(store.repo), "ls-tree", "-r", "--name-only", third).split()
    assert "new.txt" not in names and ".gitignore" in names


def test_head_and_branch_of_an_unborn_branch_is_empty(tmp_path):
    ws = tmp_path / "unborn"
    ws.mkdir()
    sh(ws, "init", "-q")
    assert checkpoints.head_and_branch(ws) == ("", "")


def test_snapshot_reports_files_and_bytes_from_its_index(repo):
    ws, store = repo
    (ws / "ignored").mkdir()
    (ws / "ignored" / "big.bin").write_bytes(b"x" * 5000)      # ignored: not in the snapshot
    (ws / "new.txt").write_bytes(b"12345678")
    (ws / "gone.txt").unlink()
    store.snapshot(ws, "x", 1, "", "")
    kept = [ws / ".gitignore", ws / "keep.txt", ws / "edit.txt", ws / "new.txt"]
    assert (store.files, store.bytes) == (len(kept), sum(p.stat().st_size for p in kept))


def test_index_stats_tolerates_junk():
    assert checkpoints.index_stats(b"") == (0, 0)
    assert checkpoints.index_stats(b"not an index at all") == (0, 0)
    assert checkpoints.index_stats(b"DIRC" + (4).to_bytes(4, "big") + (3).to_bytes(4, "big")) == (3, 0)


def test_busy_probe_leaves_the_workspace_byte_identical(tmp_path, monkeypatch):
    """The lock probe never touches another name: a sibling called like the old probe name survives, and a probe
    that ends in a locked file leaves every name and byte as it was."""
    import os
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_bytes(b"app\n")
    (ws / "app.py.harness-probe").write_bytes(b"mine\n")
    (ws / "locked.txt").write_bytes(b"locked\n")
    rename = os.rename

    def locked_rename(src, dst, *args, **kwargs):                       # Windows: open without delete sharing
        if Path(src).name == "locked.txt":
            raise PermissionError(13, "The process cannot access the file", str(src))
        return rename(src, dst, *args, **kwargs)
    monkeypatch.setattr(os, "rename", locked_rename)

    def files():
        return {p.name: p.read_bytes() for p in ws.iterdir()}
    before = files()
    plan = checkpoints.Plan("", "", [], [], ["app.py", "locked.txt"])
    locked = checkpoints.Store.busy(ws, plan)
    assert [x.split(":")[0] for x in locked] == ["locked.txt"]
    assert files() == before
