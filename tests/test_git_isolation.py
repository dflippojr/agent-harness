"""Host Git must not execute agent-writable repository configuration (issue #104)."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from harness.changes import workspace_changes
from harness.config import Project
from harness.projects import GitError, _copy_git_state, git, prepare, publish_local, refresh_origin, snapshot

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


def test_raw_git_status_runs_workspace_fsmonitor(tmp_path):
    """Sanity: the attack still works when Git is invoked without the host wrapper."""
    repo = make_repo(tmp_path / "repo")
    plant_fsmonitor(repo)
    raw = sh(repo, "status", "--porcelain")
    assert has_marker(raw.stderr)


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


def test_wrapper_commit_does_not_run_pre_commit_hook(tmp_path):
    repo = make_repo(tmp_path / "repo")
    plant_hook(repo, "pre-commit")
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(repo, "add", "-A")
    result = git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "no hook", check=False)
    assert result.code == 0
    assert not has_marker(result.out + result.err)
    assert git(repo, "log", "-1", "--format=%s").out.strip() == "no hook"


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


def test_remote_helper_url_is_not_copied_into_isolated_config(tmp_path):
    repo = make_repo(tmp_path / "repo")
    sh(repo, "remote", "add", "origin", "https://example.invalid/repo.git")
    sh(repo, "config", "remote.origin.url", f"ext::sh -c 'echo {MARKER} >&2'")
    result = git(repo, "remote", "get-url", "origin", check=False)
    assert not has_marker(result.out + result.err)
    # Dropped rather than executed: origin URL is absent from the isolated view.
    assert result.code != 0 or "ext::" not in result.out


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
    err = refresh_origin(workspace)
    assert err == ""
    result = git(workspace, "status", "--porcelain", check=False)
    assert not has_marker(result.out + result.err)
    assert not feature_ref.exists()
    listed = sh(workspace, "branch", "-r").stdout
    assert "origin/feature" not in listed


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
