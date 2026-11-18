"""Host Git must not execute agent-writable repository configuration (issue #104)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from harness.changes import workspace_changes
from harness.config import Project
import harness.projects as _projects
from harness.projects import GitError, _copy_git_state, git, prepare, publish_local, push, refresh_origin, snapshot
from host_git import needs_host_git

MARKER = "ISSUE104_GIT_EXEC_MARKER"


def sh(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True)


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    sh(path, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    sh(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return path


def has_marker(text: str) -> bool:
    return MARKER in text


def plant_fsmonitor(repo: Path) -> None:
    sh(repo, "config", "core.fsmonitor", f"sh -c 'echo {MARKER} >&2'")


def plant_hook(repo: Path, name: str, flag: Path | None = None) -> Path:
    hook = repo / ".git" / "hooks" / name
    hook.parent.mkdir(parents=True, exist_ok=True)
    body = f"#!/bin/sh\necho {MARKER} >&2\n"
    if flag is not None:
        body += f"echo ran > '{flag.as_posix()}'\n"
    hook.write_text(body + "exit 0\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    return hook


def plant_filter(repo: Path) -> None:
    (repo / ".gitattributes").write_text("* filter=issue104\n", encoding="utf-8")
    sh(repo, "config", "filter.issue104.clean", f"sh -c 'echo {MARKER} >&2; cat'")
    sh(repo, "config", "filter.issue104.smudge", "cat")


def plant_textconv(repo: Path) -> None:
    (repo / ".gitattributes").write_text("* diff=issue104\n", encoding="utf-8")
    sh(repo, "config", "diff.issue104.textconv", f"sh -c 'echo {MARKER} >&2; cat'")


def _force_loose_ref(repo: Path, ref: str) -> Path:
    """Leave `ref` as a loose file so copy-back cannot hide behind packed-refs."""
    sha = sh(repo, "rev-parse", ref).stdout.strip()
    sh(repo, "update-ref", "-d", ref)
    sh(repo, "update-ref", ref, sha)
    path = repo / ".git" / ref
    assert path.is_file()
    return path


@needs_host_git
def test_host_wrapper_status_does_not_run_fsmonitor(tmp_path):
    repo = make_repo(tmp_path / "repo")
    plant_fsmonitor(repo)
    result = git(repo, "status", "--porcelain", check=False)
    assert result.code == 0
    assert not has_marker(result.out + result.err)
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    result = git(repo, "status", "--porcelain", check=False)
    assert "app.py" in result.out
    assert not has_marker(result.out + result.err)


@needs_host_git
def test_snapshot_does_not_run_fsmonitor_or_hooks(tmp_path):
    repo = make_repo(tmp_path / "repo")
    flag = tmp_path / "hook.ran"
    plant_fsmonitor(repo)
    plant_hook(repo, "pre-commit", flag)
    plant_hook(repo, "post-commit", flag)
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert snapshot(repo, "Uncommitted work at the end of a run (session test104)") is True
    assert not flag.exists()
    log = sh(repo, "log", "-1", "--format=%s").stdout.strip()
    assert log == "Uncommitted work at the end of a run (session test104)"
    result = git(repo, "log", "-1", "--format=%s", check=False)
    assert result.out.strip() == log
    assert not has_marker(result.out + result.err)


@needs_host_git
def test_wrapper_add_does_not_run_workspace_filter(tmp_path):
    repo = make_repo(tmp_path / "repo")
    plant_filter(repo)
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    raw = sh(repo, "add", "-A", check=False)
    assert has_marker(raw.stderr)
    sh(repo, "reset", "-q")
    result = git(repo, "add", "-A", check=False)
    assert result.code == 0
    assert not has_marker(result.out + result.err)


@needs_host_git
def test_wrapper_and_changes_do_not_run_textconv(tmp_path):
    repo = make_repo(tmp_path / "repo")
    plant_textconv(repo)
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    raw = sh(repo, "diff", check=False)
    assert has_marker(raw.stderr)
    result = git(repo, "diff", "--no-color", check=False)
    assert result.code == 0
    assert not has_marker(result.out + result.err)
    assert "+VALUE = 2" in result.out
    diff = workspace_changes(repo)["repos"][0]
    assert "+VALUE = 2" in diff["diff"]
    assert not has_marker(diff["diff"])


@needs_host_git
def test_include_path_cannot_restore_fsmonitor(tmp_path):
    repo = make_repo(tmp_path / "repo")
    extra = repo / "evil.gitconfig"
    extra.write_text(f"[core]\n\tfsmonitor = sh -c 'echo {MARKER} >&2'\n", encoding="utf-8")
    sh(repo, "config", "include.path", str(extra.resolve()))
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    raw = sh(repo, "status", "--porcelain")
    assert has_marker(raw.stderr)
    result = git(repo, "status", "--porcelain", check=False)
    assert result.code == 0
    assert "app.py" in result.out
    assert not has_marker(result.out + result.err)


@needs_host_git
def test_external_gitdir_pointer_is_refused(tmp_path):
    outside = make_repo(tmp_path / "outside")
    plant_fsmonitor(outside)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").write_text(f"gitdir: {outside / '.git'}\n", encoding="utf-8")
    result = git(workspace, "status", "--porcelain", check=False)
    assert result.code != 0
    assert "outside the workspace" in result.err
    assert not has_marker(result.out + result.err)
    with pytest.raises(GitError, match="outside the workspace"):
        snapshot(workspace, "should not run")


@needs_host_git
def test_commondir_outside_workspace_is_refused(tmp_path):
    outside = make_repo(tmp_path / "outside")
    plant_fsmonitor(outside)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git_dir = workspace / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "commondir").write_text(str(outside / ".git") + "\n", encoding="utf-8")
    result = git(workspace, "status", "--porcelain", check=False)
    assert result.code != 0
    assert "commondir" in result.err
    assert not has_marker(result.out + result.err)


@needs_host_git
def test_remote_helper_url_is_not_copied_into_isolated_config(tmp_path):
    repo = make_repo(tmp_path / "repo")
    sh(repo, "remote", "add", "origin", "https://example.invalid/repo.git")
    sh(repo, "config", "remote.origin.url", f"ext::sh -c 'echo {MARKER} >&2'")
    result = git(repo, "remote", "get-url", "origin", check=False)
    assert not has_marker(result.out + result.err)
    # Dropped rather than executed: origin URL is absent from the isolated view.
    assert result.code != 0 or "ext::" not in result.out


@pytest.mark.parametrize("key", ["url", "pushurl"])
@needs_host_git
def test_workspace_remote_urls_are_not_copied_into_isolated_config(tmp_path, key):
    repo = make_repo(tmp_path / "repo")
    sh(repo, "config", f"remote.origin.{key}", "https://example.invalid/repo.git")
    sh(repo, "config", "remote.origin.promisor", "true")
    result = git(repo, "config", "--get-regexp", r"^remote\.", check=False)
    assert "example.invalid" not in result.out
    assert "promisor" not in result.out


def _second_repo(tmp_path: Path) -> tuple[Path, str]:
    other = make_repo(tmp_path / "other")
    (other / "app.py").write_text("VALUE = other\n", encoding="utf-8")
    sh(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "other")
    return other, sh(other, "rev-parse", "HEAD").stdout.strip()


def _workspace_remote_values(workspace: Path, other: Path) -> list[str]:
    """Remote URLs a workspace config might hold: local path, file URL, relative path, other host."""
    return [str(other), other.resolve().as_uri(), os.path.relpath(other, workspace).replace("\\", "/"),
            "https://example.invalid/other.git"]


@needs_host_git
def test_refresh_origin_fetches_from_the_project_repository(tmp_path):
    src = make_repo(tmp_path / "src")
    other, other_head = _second_repo(tmp_path)
    project = Project(name="proj", repo=str(src))
    workspace = tmp_path / "ws"
    prepare(project, workspace, "refresh01")
    (src / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    sh(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "upstream")
    src_head = sh(src, "rev-parse", "HEAD").stdout.strip()

    for value in _workspace_remote_values(workspace, other):
        sh(workspace, "config", "remote.origin.url", value)
        assert refresh_origin(project, workspace) == ""
        assert sh(workspace, "rev-parse", "origin/main").stdout.strip() == src_head
        assert sh(workspace, "cat-file", "-e", other_head, check=False).returncode != 0


@needs_host_git
def test_push_sends_the_branch_to_the_project_repository(tmp_path):
    src = make_repo(tmp_path / "src")
    sh(src, "config", "receive.denyCurrentBranch", "ignore")
    other, _ = _second_repo(tmp_path)
    workspace = tmp_path / "ws"
    info = prepare(Project(name="proj", repo=str(src)), workspace, "push01")
    (workspace / "app.py").write_text("VALUE = 4\n", encoding="utf-8")
    assert snapshot(workspace, "work") is True
    branch_head = sh(workspace, "rev-parse", "HEAD").stdout.strip()
    # push() is for URL projects; a file URL to the same source keeps the test offline.
    project = Project(name="proj", repo=src.resolve().as_uri())

    for value in _workspace_remote_values(workspace, other):
        sh(workspace, "config", "remote.origin.url", value)
        sh(workspace, "config", "remote.origin.pushurl", value)
        message = push(project, workspace, info["branch"])
        assert message == f"pushed {info['branch']} to {project.repo}"
        assert sh(src, "rev-parse", info["branch"]).stdout.strip() == branch_head
        assert sh(other, "rev-parse", "--verify", "-q", info["branch"], check=False).returncode != 0
        sh(src, "branch", "-D", info["branch"])


@needs_host_git
def test_prepare_refresh_snapshot_and_publish_ignore_workspace_exec_config(tmp_path):
    src = make_repo(tmp_path / "src")
    workspace = tmp_path / "ws"
    info = prepare(Project(name="proj", repo=str(src)), workspace, "deadbeef01")
    plant_fsmonitor(workspace)
    plant_hook(workspace, "pre-commit")
    plant_hook(workspace, "pre-push")
    plant_hook(workspace, "post-commit")
    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

    raw = sh(workspace, "status", "--porcelain")
    assert has_marker(raw.stderr)

    refresh = git(workspace, "fetch", "--quiet", "--prune", "origin", check=False)
    assert not has_marker(refresh.out + refresh.err)

    assert snapshot(workspace, "Uncommitted work at the end of a run (session deadbeef01)") is True
    result = git(workspace, "log", "-1", "--format=%s", check=False)
    assert result.out.strip().startswith("Uncommitted work")
    assert not has_marker(result.out + result.err)

    diff = workspace_changes(workspace, info["base_commit"])["repos"][0]
    assert "+VALUE = 2" in diff["diff"]
    assert not has_marker(diff["diff"])

    assert publish_local(Project(name="proj", repo=str(src)), workspace, info["branch"]) is True
    assert sh(src, "show", f"{info['branch']}:app.py").stdout == "VALUE = 2\n"


@needs_host_git
def test_changes_shows_untracked_files_without_running_fsmonitor(tmp_path):
    repo = tmp_path / "nested"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    plant_fsmonitor(repo)
    (repo / "new.txt").write_text("hello\n", encoding="utf-8")
    raw = sh(repo, "status", "--porcelain")
    assert has_marker(raw.stderr)
    diff = workspace_changes(tmp_path)["repos"][0]
    assert diff["path"] == "nested"
    assert {"path": "new.txt", "status": "??"} in diff["files"]
    assert "+hello" in diff["diff"]
    assert not has_marker(diff["diff"])


@needs_host_git
def test_copy_git_state_drops_refs_removed_from_source(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    for root in (src, dst):
        (root / "refs" / "heads").mkdir(parents=True)
        (root / "refs" / "remotes" / "origin").mkdir(parents=True)
        (root / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (root / "refs" / "heads" / "main").write_text("aaa\n", encoding="utf-8")
    (src / "refs" / "remotes" / "origin" / "main").write_text("aaa\n", encoding="utf-8")
    (dst / "refs" / "remotes" / "origin" / "main").write_text("old\n", encoding="utf-8")
    (dst / "refs" / "remotes" / "origin" / "feature").write_text("bbb\n", encoding="utf-8")
    (dst / "logs" / "refs" / "remotes" / "origin").mkdir(parents=True)
    (dst / "logs" / "refs" / "remotes" / "origin" / "feature").write_text("log\n", encoding="utf-8")
    (src / "logs" / "refs" / "heads").mkdir(parents=True)
    (src / "logs" / "HEAD").write_text("headlog\n", encoding="utf-8")
    (dst / "logs" / "HEAD").write_text("oldhead\n", encoding="utf-8")

    _copy_git_state(src, dst)

    assert (dst / "refs" / "heads" / "main").read_text(encoding="utf-8") == "aaa\n"
    assert (dst / "refs" / "remotes" / "origin" / "main").read_text(encoding="utf-8") == "aaa\n"
    assert not (dst / "refs" / "remotes" / "origin" / "feature").exists()
    assert not (dst / "logs" / "refs" / "remotes" / "origin" / "feature").exists()
    assert (dst / "logs" / "HEAD").read_text(encoding="utf-8") == "headlog\n"


@needs_host_git
def test_fetch_prune_removes_loose_remote_tracking_ref(tmp_path):
    """refresh()/fetch --prune must drop loose origin refs, not only packed-refs."""
    src = make_repo(tmp_path / "src")
    sh(src, "checkout", "-q", "-b", "feature")
    (src / "app.py").write_text("VALUE = feature\n", encoding="utf-8")
    sh(src, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    sh(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "feature")
    sh(src, "checkout", "-q", "main")

    workspace = tmp_path / "ws"
    prepare(Project(name="proj", repo=str(src)), workspace, "prune01")
    plant_fsmonitor(workspace)
    sh(workspace, "fetch", "-q", "origin", "refs/heads/feature:refs/remotes/origin/feature")
    feature_ref = _force_loose_ref(workspace, "refs/remotes/origin/feature")
    listed = sh(workspace, "branch", "-r").stdout
    assert "origin/feature" in listed

    sh(src, "branch", "-D", "feature")
    err = refresh_origin(Project(name="proj", repo=str(src)), workspace)
    assert err == ""
    result = git(workspace, "status", "--porcelain", check=False)
    assert not has_marker(result.out + result.err)
    assert not feature_ref.exists()
    listed = sh(workspace, "branch", "-r").stdout
    assert "origin/feature" not in listed


@needs_host_git
def test_trusted_source_operations_still_see_the_source_repo(tmp_path):
    """Isolation must not break reviewed merge inputs: the source still has its own identity."""
    src = make_repo(tmp_path / "src")
    workspace = tmp_path / "ws"
    info = prepare(Project(name="proj", repo=str(src)), workspace, "cafebabe02")
    (workspace / "app.py").write_text("VALUE = 9\n", encoding="utf-8")
    assert snapshot(workspace, "wip") is True
    assert publish_local(Project(name="proj", repo=str(src)), workspace, info["branch"]) is True
    listed = git(src, "branch", "--list", info["branch"], trusted=True).out.strip()
    assert info["branch"] in listed


@needs_host_git
def test_isolated_call_spawns_at_most_two_git_processes(tmp_path, monkeypatch):
    """The allowlisted config is injected through the environment, not replayed one `git config` per key (#403)."""
    import harness.projects as projects

    repo = make_repo(tmp_path / "repo")
    sh(repo, "config", "branch.main.description", "kept")
    calls = []
    real_run = projects._run
    monkeypatch.setattr(projects, "_run", lambda args, *a, **k: calls.append(args) or real_run(args, *a, **k))
    result = git(repo, "config", "--get", "branch.main.description")
    assert result.out.strip() == "kept"
    assert len(calls) <= 2, calls


@needs_host_git
def test_concurrent_git_calls_serialize_copy_run_copy(tmp_path, monkeypatch):
    """A reader copying back must not overwrite refs created by a concurrent call (#426)."""
    import harness.projects as projects

    repo = make_repo(tmp_path / "repo")
    metadata = repo / ".git"
    copying_back = threading.Event()
    release_copy = threading.Event()
    second_resolved = threading.Event()
    overlapping_copy = threading.Event()
    real_copy = projects._copy_git_state
    real_resolve = projects._resolve_workspace_git

    def resolve(work):
        result = real_resolve(work)
        if copying_back.is_set():
            second_resolved.set()
        return result

    def copy_state(src, dst):
        if dst == metadata and not copying_back.is_set():
            copying_back.set()
            assert release_copy.wait(10), "test did not release copy-back"
        elif src == metadata and copying_back.is_set() and not release_copy.is_set():
            overlapping_copy.set()
        real_copy(src, dst)

    monkeypatch.setattr(projects, "_resolve_workspace_git", resolve)
    monkeypatch.setattr(projects, "_copy_git_state", copy_state)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(git, repo, "status", "--porcelain")
        try:
            assert copying_back.wait(10), "first call did not reach copy-back"
            # An equivalent path must use the same metadata lock.
            second = pool.submit(git, repo / ".", "update-ref", "refs/heads/concurrent", "HEAD")
            assert second_resolved.wait(10), "second call did not resolve metadata"
            overlapped = overlapping_copy.wait(0.5)
        finally:
            release_copy.set()
        assert first.result(timeout=10).code == 0
        assert second.result(timeout=10).code == 0
    assert not overlapped, "copy-in overlapped another call's copy-out"
    assert sh(repo, "rev-parse", "concurrent").stdout == sh(repo, "rev-parse", "HEAD").stdout


def test_git_state_lock_is_per_canonical_metadata_dir(tmp_path):
    import harness.projects as projects

    metadata = tmp_path / "repo" / ".git"
    metadata.mkdir(parents=True)
    lock = projects._git_state_lock(metadata)
    assert projects._git_state_lock(metadata / ".." / ".git") is lock
    other = projects._git_state_lock(tmp_path / "other" / ".git")
    with lock:
        assert other.acquire(blocking=False)
        other.release()


@needs_host_git
def test_git_state_lock_released_after_command_exception(tmp_path, monkeypatch):
    import harness.projects as projects

    repo = make_repo(tmp_path / "repo")
    real_run = projects._run

    def fail_command(args, **kwargs):
        if "--git-dir" in args:
            raise subprocess.TimeoutExpired("git", 1)
        return real_run(args, **kwargs)

    monkeypatch.setattr(projects, "_run", fail_command)
    with pytest.raises(subprocess.TimeoutExpired):
        git(repo, "status")
    monkeypatch.setattr(projects, "_run", real_run)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(git, repo, "status", "--porcelain").result(timeout=10).code == 0


@needs_host_git
def test_push_records_the_pushed_commit_as_the_origin_tracking_ref(tmp_path):
    src = make_repo(tmp_path / "src")
    workspace = tmp_path / "ws"
    info = prepare(Project(name="proj", repo=str(src)), workspace, "push02")
    (workspace / "app.py").write_text("VALUE = 5\n", encoding="utf-8")
    assert snapshot(workspace, "work") is True
    pushed = sh(workspace, "rev-parse", "HEAD").stdout.strip()
    push(Project(name="proj", repo=src.resolve().as_uri()), workspace, info["branch"], source_ref=pushed)
    assert sh(workspace, "rev-parse", f"origin/{info['branch']}").stdout.strip() == pushed
    assert git(workspace, "log", "--oneline", f"origin/{info['branch']}..HEAD").out.strip() == ""


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as e:  # Windows without symlink privilege
        pytest.skip(f"symlinks unavailable: {e}")


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@needs_host_git
def test_refresh_origin_refuses_a_symlinked_state_file(tmp_path):
    """A workspace state file that is a symlink is refused, never read or written through by host git (#966)."""
    src = make_repo(tmp_path / "src")
    workspace = tmp_path / "ws"
    prepare(Project(name="proj", repo=str(src)), workspace, "link01")
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged\n", encoding="utf-8")
    fetch_head = workspace / ".git" / "FETCH_HEAD"
    fetch_head.unlink(missing_ok=True)
    _symlink_or_skip(fetch_head, outside)

    err = refresh_origin(Project(name="proj", repo=str(src)), workspace)

    assert "refusing symlinked or special git state FETCH_HEAD" in err
    assert outside.read_text(encoding="utf-8") == "unchanged\n"


@needs_host_git
def test_refresh_origin_refuses_a_symlinked_state_dir(tmp_path):
    """A workspace `logs` dir that is a symlink is refused, never read or written through by host git (#966)."""
    src = make_repo(tmp_path / "src")
    workspace = tmp_path / "ws"
    prepare(Project(name="proj", repo=str(src)), workspace, "link02")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.txt").write_text("unchanged\n", encoding="utf-8")
    before = _tree_snapshot(outside)
    logs = workspace / ".git" / "logs"
    if logs.exists():
        shutil.rmtree(logs)
    _symlink_or_skip(logs, outside)
    sh(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "next")

    err = refresh_origin(Project(name="proj", repo=str(src)), workspace)

    assert "refusing symlinked or special git state logs" in err
    assert _tree_snapshot(outside) == before


@needs_host_git
def test_copy_git_state_refuses_a_symlinked_source_entry(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "refs" / "heads").mkdir(parents=True)
    (src / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    _symlink_or_skip(src / "refs" / "heads" / "main", outside)

    with pytest.raises(GitError, match="symlink"):
        _copy_git_state(src, dst)

    assert not (dst / "refs" / "heads" / "main").exists()


@needs_host_git
def test_copy_git_state_replaces_symlinked_destination_entries(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "refs" / "heads").mkdir(parents=True)
    (src / "logs").mkdir()
    (src / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (src / "refs" / "heads" / "main").write_text("aaa\n", encoding="utf-8")
    (src / "logs" / "HEAD").write_text("headlog\n", encoding="utf-8")
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside\n", encoding="utf-8")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "keep").write_bytes(b"keep\n")
    (dst / "refs" / "heads").mkdir(parents=True)
    _symlink_or_skip(dst / "HEAD", outside_file)
    _symlink_or_skip(dst / "refs" / "heads" / "main", outside_file)
    _symlink_or_skip(dst / "logs", outside_dir)

    _copy_git_state(src, dst)

    assert outside_file.read_text(encoding="utf-8") == "outside\n"
    assert _tree_snapshot(outside_dir) == {"keep": b"keep\n"}
    for entry in (dst / "HEAD", dst / "refs" / "heads" / "main", dst / "logs"):
        assert not entry.is_symlink()
    assert (dst / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main\n"
    assert (dst / "refs" / "heads" / "main").read_text(encoding="utf-8") == "aaa\n"
    assert (dst / "logs" / "HEAD").read_text(encoding="utf-8") == "headlog\n"


def _state_dirs(tmp_path: Path) -> tuple[Path, Path]:
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "refs" / "heads").mkdir(parents=True)
    (src / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    (src / "refs" / "heads" / "main").write_bytes(b"aaa\n")
    dst.mkdir()
    return src, dst


@needs_host_git
def test_copy_git_state_refuses_state_entries_of_the_wrong_type(tmp_path):
    src, dst = _state_dirs(tmp_path)
    (src / "FETCH_HEAD").mkdir()
    with pytest.raises(GitError, match="FETCH_HEAD"):
        _copy_git_state(src, dst)
    (src / "FETCH_HEAD").rmdir()
    shutil.rmtree(src / "refs")
    (src / "refs").write_bytes(b"not a directory\n")
    with pytest.raises(GitError, match="refs"):
        _copy_git_state(src, dst)


@needs_host_git
def test_copy_git_state_temp_names_stay_short_whatever_the_ref_name(tmp_path, monkeypatch):
    """The copy's temp file sits beside the ref it replaces, so its name must not grow with the ref's: git allows a
    245-byte ref component, and the name limit is 255."""
    src, dst = _state_dirs(tmp_path)
    name = "r" * 120
    (src / "refs" / "heads" / name).write_bytes(b"bbb\n")
    (dst / "refs" / "heads").mkdir(parents=True)
    (dst / "refs" / "heads" / name).write_bytes(b"old\n")
    temps = []
    real_replace = os.replace

    def replace(source, target, **kwargs):
        temps.append(os.path.basename(source))
        return real_replace(source, target, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    _copy_git_state(src, dst)

    assert (dst / "refs" / "heads" / name).read_bytes() == b"bbb\n"
    assert sorted(p.name for p in (dst / "refs" / "heads").iterdir()) == sorted(["main", name])
    assert temps and all(len(t) <= 32 and t.startswith(".") and t.endswith(".lock") for t in temps)


def _swap_for_link(directory: Path, outside: Path) -> bool:
    """Replace `directory` with a link to `outside`, as a concurrent workspace process would. False when the OS refuses
    (Windows: the copy holds the directory open without delete sharing, so it cannot be renamed)."""
    try:
        directory.rename(directory.with_name(directory.name + "-moved"))
    except PermissionError:
        return False
    directory.symlink_to(outside, target_is_directory=True)
    return True


def _require_symlinks(tmp_path: Path, target: Path) -> None:
    probe = tmp_path / "probe"
    _symlink_or_skip(probe, target)
    probe.unlink()


@needs_host_git
def test_copy_git_state_does_not_follow_a_directory_swapped_for_a_link_mid_copy(tmp_path, monkeypatch):
    """A destination directory replaced by a link after the copy opened it: nothing lands where the link points (#966).
    POSIX keeps writing through the directory descriptor; Windows holds the directory so it cannot be replaced."""
    src, dst = _state_dirs(tmp_path)
    heads = dst / "refs" / "heads"
    heads.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _require_symlinks(tmp_path, outside)
    real_write = _projects._StateDir.write
    swapped = []

    def write(self, name, source_fd):
        if self.path == heads and not swapped:
            swapped.append(_swap_for_link(heads, outside))
        return real_write(self, name, source_fd)

    monkeypatch.setattr(_projects._StateDir, "write", write)
    _copy_git_state(src, dst)

    assert swapped == [True] if _projects._FD_OPS else swapped == [False]
    assert list(outside.iterdir()) == []


@needs_host_git
def test_copy_git_state_does_not_follow_an_ancestor_swapped_for_a_link_mid_copy(tmp_path, monkeypatch):
    """A directory above the git dir (a workspace holding a nested git dir) replaced by a link mid-copy (#966)."""
    src, _ = _state_dirs(tmp_path)
    workspace = tmp_path / "ws"
    dst = workspace / "nested" / "git"
    (dst / "refs" / "heads").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "nested" / "git" / "refs" / "heads").mkdir(parents=True)
    before = sorted(p.relative_to(outside).as_posix() for p in outside.rglob("*"))
    _require_symlinks(tmp_path, outside)
    real_write = _projects._StateDir.write
    swapped = []

    def write(self, name, source_fd):
        if not swapped:
            swapped.append(_swap_for_link(workspace, outside))
        return real_write(self, name, source_fd)

    monkeypatch.setattr(_projects._StateDir, "write", write)
    _copy_git_state(src, dst)

    assert swapped == [True] if _projects._FD_OPS else swapped == [False]
    assert sorted(p.relative_to(outside).as_posix() for p in outside.rglob("*")) == before


def _at_file_open(monkeypatch, name: str, action) -> None:
    """Run `action` once, immediately before the filesystem call that opens file `name`: os.open on POSIX, the
    Windows handle open otherwise. This is the boundary between the copy's checks and its use of the path."""
    done = []

    def wrap(real):
        def call(path, *args, **kwargs):
            opening_a_directory = bool(args) and args[0] is True  # _win_open(path, directory=True)
            if not done and not opening_a_directory and os.path.basename(os.fsdecode(path)) == name:
                done.append(action())
            return real(path, *args, **kwargs)
        return call

    monkeypatch.setattr(os, "open", wrap(os.open))
    if hasattr(_projects, "_win_open"):
        monkeypatch.setattr(_projects, "_win_open", wrap(_projects._win_open))


@needs_host_git
def test_copy_git_state_reads_no_source_through_a_directory_swapped_at_open(tmp_path, monkeypatch):
    """The source directory is swapped for a link to outside content right before the file open (#966): the copy
    still reads only the workspace's own file. POSIX opens relative to the held directory descriptor; Windows holds
    the directory open so it cannot be renamed."""
    src, dst = _state_dirs(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "main").write_bytes(b"outside\n")
    _require_symlinks(tmp_path, outside)
    swapped = []
    _at_file_open(monkeypatch, "main", lambda: swapped.append(_swap_for_link(src / "refs" / "heads", outside)))

    _copy_git_state(src, dst)

    assert (dst / "refs" / "heads" / "main").read_bytes() == b"aaa\n"
    assert (outside / "main").read_bytes() == b"outside\n"
    assert swapped == [True] if _projects._FD_OPS else swapped == [False]


def test_copy_git_state_reads_no_source_file_swapped_for_a_link_at_open(tmp_path, monkeypatch):
    """A source file replaced by a link right before the file open is refused, not read through (#966)."""
    src, dst = _state_dirs(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside\n")
    _require_symlinks(tmp_path, outside)
    main = src / "refs" / "heads" / "main"

    def swap():
        main.unlink()
        main.symlink_to(outside)

    _at_file_open(monkeypatch, "main", swap)
    with pytest.raises(GitError):
        _copy_git_state(src, dst)
    assert not (dst / "refs" / "heads" / "main").exists()


@needs_host_git
def test_copy_git_state_fails_closed_without_descriptor_or_handle_access(tmp_path, monkeypatch):
    monkeypatch.setattr(_projects, "_FD_OPS", False)
    monkeypatch.setattr(_projects, "_WIN_HANDLES", False)
    src, dst = _state_dirs(tmp_path)
    with pytest.raises(GitError, match="lacks"):
        _copy_git_state(src, dst)
    assert list(dst.iterdir()) == []


def test_copy_git_state_creates_nothing_through_a_linked_destination_path(tmp_path):
    """A missing destination under a linked ancestor is refused before anything is created where the link points."""
    src, _ = _state_dirs(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "ws"
    _symlink_or_skip(workspace, outside)
    with pytest.raises(GitError):
        _copy_git_state(src, workspace / "git")
    assert list(outside.iterdir()) == []


@needs_host_git
def test_copy_git_state_completes_short_writes(tmp_path, monkeypatch):
    src, dst = _state_dirs(tmp_path)
    content = b"# pack-refs with: peeled fully-peeled sorted\n" + b"a" * 40 + b" refs/heads/main\n"
    (src / "packed-refs").write_bytes(content)
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, bytes(data[:3])))

    _copy_git_state(src, dst)

    assert (dst / "packed-refs").read_bytes() == content
    assert (dst / "refs" / "heads" / "main").read_bytes() == b"aaa\n"


def test_copy_git_state_refuses_a_link_swapped_into_its_path(tmp_path):
    """The state directory itself is refused when a link replaced it after its path was resolved."""
    src, dst = _state_dirs(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    dst.rename(tmp_path / "real")
    _symlink_or_skip(dst, outside)
    with pytest.raises(GitError):
        _copy_git_state(src, dst)
    assert list(outside.iterdir()) == []


@needs_host_git
def test_copy_git_state_replaces_a_directory_link_at_a_file_name(tmp_path, monkeypatch):
    """A directory link (a junction on Windows) at a state file's name is removed itself, then replaced. The rename
    here refuses a directory-link target, as Windows' MoveFileEx does, so the link must be gone first (#966)."""
    src, dst = _state_dirs(tmp_path)
    (src / "FETCH_HEAD").write_bytes(b"fetched\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(dst / "FETCH_HEAD", outside)
    real_replace = os.replace

    def replace(source, target, **kwargs):
        at = dst / os.path.basename(target)  # every replace in this copy lands directly in `dst`
        if at.is_symlink() and at.is_dir():
            raise IsADirectoryError(target)
        return real_replace(source, target, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    _copy_git_state(src, dst)

    assert not (dst / "FETCH_HEAD").is_symlink()
    assert (dst / "FETCH_HEAD").read_bytes() == b"fetched\n"
    assert list(outside.iterdir()) == []


# --- gitdir / commondir pointers are checked as strings before any filesystem call (#966) ------------------------

MARKER_DIR = "POINTER-TARGET-MARKER"


def _spy_filesystem(monkeypatch) -> list[str]:
    """Record every resolve, stat and open whose path names MARKER_DIR."""
    touched: list[str] = []

    def spy(real):
        def call(path, *args, **kwargs):
            if MARKER_DIR in os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike)) else False:
                touched.append(f"{real.__name__}({os.fsdecode(path)})")
            return real(path, *args, **kwargs)
        return call

    for name in ("stat", "lstat", "open", "scandir", "listdir"):
        monkeypatch.setattr(os, name, spy(getattr(os, name)))
    monkeypatch.setattr(os.path, "realpath", spy(os.path.realpath))
    real_resolve = Path.resolve

    def resolve(self, *args, **kwargs):
        if MARKER_DIR in str(self):
            touched.append(f"resolve({self})")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    return touched


@pytest.mark.parametrize("target", [
    "{outside}/" + MARKER_DIR + "/.git",
    "../../" + MARKER_DIR,
    "//server/" + MARKER_DIR + "/repo",
    "\\\\server\\" + MARKER_DIR + "\\repo",
    "\\\\?\\C:\\" + MARKER_DIR,
    "\\\\.\\pipe\\" + MARKER_DIR,
    "C:" + MARKER_DIR,
    "sub/.. /" + MARKER_DIR,
    "sub/" + MARKER_DIR + ".",
    MARKER_DIR + "\0x",
])
@pytest.mark.parametrize("pointer", ["gitdir", "commondir"])
@needs_host_git
def test_rejected_pointer_targets_never_reach_the_filesystem(tmp_path, monkeypatch, target, pointer):
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = make_repo(tmp_path / "ws")
    text = target.format(outside=outside.as_posix())
    if pointer == "gitdir":
        shutil.move(str(workspace / ".git"), str(workspace / ".gitdata"))
        (workspace / ".git").write_text(f"gitdir: {text}\n", encoding="utf-8")
    else:
        (workspace / ".git" / "commondir").write_text(text + "\n", encoding="utf-8")
    touched = _spy_filesystem(monkeypatch)

    with pytest.raises(GitError, match="outside the workspace"):
        _projects._resolve_workspace_git(workspace)

    assert touched == []


@needs_host_git
def test_a_pointer_through_a_link_inside_the_workspace_is_refused_unresolved(tmp_path, monkeypatch):
    workspace = make_repo(tmp_path / "ws")
    shutil.move(str(workspace / ".git"), str(workspace / ".gitdata"))
    _symlink_or_skip(workspace / MARKER_DIR, workspace / ".gitdata")
    (workspace / ".git").write_text(f"gitdir: {MARKER_DIR}\n", encoding="utf-8")
    resolved = []
    real_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: resolved.append(str(self)) or real_resolve(self, *a, **k))

    with pytest.raises(GitError, match="through a link"):
        _projects._resolve_workspace_git(workspace)

    assert not any(MARKER_DIR in path for path in resolved)


@needs_host_git
def test_a_relative_gitdir_pointer_inside_the_workspace_still_works(tmp_path):
    workspace = make_repo(tmp_path / "ws")
    shutil.move(str(workspace / ".git"), str(workspace / ".gitdata"))
    (workspace / ".git").write_text("gitdir: .gitdata\n", encoding="utf-8")
    git_dir, metadata, _ = _projects._resolve_workspace_git(workspace)
    assert git_dir == metadata == workspace.resolve() / ".gitdata"
    assert git(workspace, "status", "--porcelain").code == 0


def _at_filesystem_call(monkeypatch, name: str, action, opens_only: bool = False) -> None:
    """Run `action` once, immediately before the first filesystem call that names `name`: a stat or an open (os.open,
    io.open, or the Windows handle open), or only an open. This is the boundary between a check and the use."""
    import io

    done = []

    def wrap(real):
        def call(path, *args, **kwargs):
            if not done and isinstance(path, (str, bytes, os.PathLike)) and os.path.basename(os.fsdecode(path)) == name:
                done.append(action())
            return real(path, *args, **kwargs)
        return call

    calls = [(os, "open"), (io, "open")] + ([] if opens_only else [(os, "stat"), (os, "lstat")])
    for module, attr in calls:
        monkeypatch.setattr(module, attr, wrap(getattr(module, attr)))
    if hasattr(_projects, "_win_open"):
        monkeypatch.setattr(_projects, "_win_open", wrap(_projects._win_open))


def _swap_file_for_link(path: Path, target: Path) -> None:
    path.unlink()
    path.symlink_to(target)


@pytest.mark.parametrize("pointer", [".git", "commondir"])
@needs_host_git
def test_a_pointer_file_swapped_for_a_link_at_its_read_is_refused(tmp_path, monkeypatch, pointer):
    """The pointer file is replaced by a link to an outside file between its check and its read: the read refuses
    the link instead of returning the outside file's text (#966)."""
    workspace = make_repo(tmp_path / "ws")
    shutil.move(str(workspace / ".git"), str(workspace / ".gitdata"))
    (workspace / ".git").write_text("gitdir: .gitdata\n", encoding="utf-8")
    if pointer == "commondir":
        (workspace / ".gitdata" / "commondir").write_text(".\n", encoding="utf-8")
    outside = tmp_path / "outside-pointer"
    (outside / "gitdata").mkdir(parents=True)
    _require_symlinks(tmp_path, outside)
    # The outside text names a real in-workspace directory, so following the link would succeed quietly.
    outside_file = outside / "pointer"
    outside_file.write_text("gitdir: .gitdata\n" if pointer == ".git" else "../.gitdata\n", encoding="utf-8")
    path = workspace / pointer if pointer == ".git" else workspace / ".gitdata" / pointer
    reads = []
    _at_filesystem_call(monkeypatch, pointer, lambda: (_swap_file_for_link(path, outside_file), reads.append(1)),
                        opens_only=True)

    with pytest.raises(GitError, match="not a regular file"):
        _projects._resolve_workspace_git(workspace)
    assert reads == [1]


@needs_host_git
def test_a_pointer_target_ancestor_swapped_during_the_walk_is_not_followed(tmp_path, monkeypatch):
    """A directory on the pointer's target path is replaced by a link to an outside tree after the walk passed it:
    later steps keep using the held directory, so nothing outside is read (#966)."""
    workspace = make_repo(tmp_path / "ws")
    (workspace / "sub").mkdir()
    shutil.move(str(workspace / ".git"), str(workspace / "sub" / "gitdata"))
    (workspace / ".git").write_text("gitdir: sub/gitdata\n", encoding="utf-8")
    outside = tmp_path / "outside-tree"
    (outside / "gitdata").mkdir(parents=True)
    # Reading this outside commondir would raise: it points out of the workspace.
    (outside / "gitdata" / "commondir").write_text("../../../elsewhere\n", encoding="utf-8")
    _require_symlinks(tmp_path, outside)
    swapped = []
    _at_filesystem_call(monkeypatch, "commondir",
                        lambda: swapped.append(_swap_for_link(workspace / "sub", outside)))

    git_dir, metadata, _ = _projects._resolve_workspace_git(workspace)

    assert swapped == [True] if _projects._FD_OPS else swapped == [False]
    assert git_dir == metadata == workspace.resolve() / "sub" / "gitdata"


@pytest.mark.parametrize("name", ["CON", "aux.lock", "com1", "name.", "name "])
@needs_host_git
def test_copy_git_state_refuses_ambiguous_windows_entry_names(tmp_path, monkeypatch, name):
    """A state entry whose name Windows would rewrite when it is used as a path (a device name, a trailing dot or
    space) is refused before it is touched (#966). On Windows the fixture is a native NTFS name."""
    monkeypatch.setattr(_projects, "_WINDOWS_NAMES", True)
    src, dst = _state_dirs(tmp_path)
    entry = src / "refs" / "heads" / name
    native = "\\\\?\\" + str(entry) if os.name == "nt" else str(entry)  # \\?\ keeps the name exactly as written
    with open(native, "wb") as handle:
        handle.write(b"bbb\n")
    opened = []
    real_open = _projects._StateDir.open_read
    monkeypatch.setattr(_projects._StateDir, "open_read",
                        lambda self, n: opened.append(n) or real_open(self, n))

    with pytest.raises(GitError, match="ambiguous Windows name"):
        _copy_git_state(src, dst)

    assert name not in opened


@pytest.mark.parametrize("name", ["HEAD", "FETCH_HEAD"])
@needs_host_git
def test_copy_git_state_refuses_a_directory_at_a_top_level_state_file(tmp_path, name):
    """Git never turns a top-level state file into a directory, so one found there is refused and kept, not deleted
    with its contents (#972). Directory/file switches happen only in the refs and logs trees."""
    src, dst = _state_dirs(tmp_path)
    (src / "FETCH_HEAD").write_bytes(b"fetched\n")
    (dst / name).mkdir()
    (dst / name / "keep").write_bytes(b"keep\n")

    with pytest.raises(GitError, match=name):
        _copy_git_state(src, dst)

    assert (dst / name / "keep").read_bytes() == b"keep\n"
