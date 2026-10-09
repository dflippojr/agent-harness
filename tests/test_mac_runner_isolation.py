"""Mac runner: host-side git scratch space, process cleanup and the sandbox profile.

The runner's host-side git keeps its throwaway git dirs and hooks dirs in a private directory the shell sandbox
can't reach, every command's process group ends with the command, and the profile keeps ~/.agent-harness (apart
from the session's workspace) and launchd/LaunchServices out of reach. sandbox-exec only exists on macOS, so the
profile is checked as text here; docs/phase4-results.md lists the on-Mac checks.
"""

from __future__ import annotations

import io
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from harness import projects
from harness.config import Project

from test_git_isolation import make_repo, sh
from test_phase4 import bash, executor, needs_bash

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "macrunner"))
import harness_runner  # noqa: E402

PROFILE = Path(__file__).resolve().parent.parent / "macrunner" / "sandbox.sb"
SID = "0123456789"


@pytest.fixture
def shared_tmp(tmp_path, monkeypatch):
    """The system temp directory, which the sandbox profile lets commands write to."""
    shared = tmp_path / "shared-tmp"
    shared.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(shared))
    return shared


@pytest.fixture
def private_root(tmp_path, monkeypatch):
    root = harness_runner.git_temp_root(tmp_path / "home")
    monkeypatch.setattr(projects, "TEMP_ROOT", root)
    return root


def write_hooks_into_shared_tmp(shared: Path, flag: Path) -> list[Path]:
    """Write executable post-commit and reference-transaction hooks into every harness-git-*/hooks directory in the
    shared temp directory: what any process limited to the sandbox's writable paths could do."""
    planted = []
    for hooks in shared.glob("harness-git-*/hooks"):
        for name in ("post-commit", "reference-transaction"):
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\necho ran > '{flag.as_posix()}'\n", encoding="utf-8")
            hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
            planted.append(hook)
    return planted


def test_runner_git_temp_root_is_inside_the_runner_directory(tmp_path):
    home = tmp_path / "home"
    root = harness_runner.git_temp_root(home)
    assert root.is_relative_to(home / ".agent-harness" / "runner")


def test_daemon_git_keeps_using_the_system_temp_directory(tmp_path, shared_tmp):
    assert projects.TEMP_ROOT is None
    seen = []
    real = projects.tempfile.TemporaryDirectory

    def spy(*args, **kwargs):
        seen.append(kwargs.get("dir"))
        return real(*args, **kwargs)

    repo = make_repo(tmp_path / "ws")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(projects.tempfile, "TemporaryDirectory", spy)
        projects.git(repo, "status", "--porcelain")
    assert seen == [None]


def test_snapshot_keeps_git_scratch_out_of_the_shared_temp_directory(tmp_path, shared_tmp, private_root,
                                                                     monkeypatch):
    repo = make_repo(tmp_path / "ws")
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    flag = tmp_path / "hook-ran"
    created, planted = [], []
    real_config = projects._write_isolated_config

    def write_config(dest: Path, hooks: Path) -> None:
        created.append(hooks)
        planted.extend(write_hooks_into_shared_tmp(shared_tmp, flag))
        real_config(dest, hooks)

    monkeypatch.setattr(projects, "_write_isolated_config", write_config)
    assert projects.snapshot(repo, "save") is True
    assert sh(repo, "log", "-1", "--format=%s").stdout.strip() == "save"
    assert created and all(h.is_relative_to(private_root) for h in created)
    assert planted == []
    assert not flag.exists()
    assert list(shared_tmp.iterdir()) == []
    assert list(private_root.iterdir()) == []  # each scratch dir is removed after its git call
    if os.name == "posix":
        assert stat.S_IMODE(private_root.stat().st_mode) == 0o700


def test_bare_source_merge_worktree_uses_the_private_root(tmp_path, shared_tmp, private_root, monkeypatch):
    seed = make_repo(tmp_path / "seed")
    source = tmp_path / "source.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(source)], check=True)
    ws = tmp_path / "ws"
    project = Project(name="p", repo=str(source))
    projects.prepare(project, ws, SID, shared=True)
    (ws / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    made = []
    real = projects.tempfile.mkdtemp

    def spy(*args, **kwargs):
        made.append(Path(real(*args, **kwargs)))
        return str(made[-1])

    monkeypatch.setattr(projects.tempfile, "mkdtemp", spy)
    result = projects.merge(project, ws, SID, projects.branch_name(SID), "main", "Change value")
    assert result["merged"] is True
    assert made and all(p.is_relative_to(private_root) for p in made)
    assert list(shared_tmp.iterdir()) == []


def test_temp_root_must_be_a_plain_directory(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "root-link"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs developer mode on Windows")
    monkeypatch.setattr(projects, "TEMP_ROOT", link)
    with pytest.raises(projects.GitError, match="plain directory"):
        projects.git(make_repo(tmp_path / "ws"), "status")


# process cleanup
def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # an exited child not yet reaped by its new parent counts as gone
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except (OSError, IndexError):
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                             check=False).stdout
        return bool(out.strip()) and not out.strip().startswith("Z")


def _gone(pid: int, seconds: float = 5) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


@needs_bash
@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
@pytest.mark.parametrize("launch", ["sleep 30 >/dev/null 2>&1 &", "nohup sleep 30 >/dev/null 2>&1 &"])
def test_background_children_end_with_their_command(tmp_path, shared_tmp, launch):
    ex = executor(tmp_path, [tmp_path])
    out = ex.handle("r1", "shell", {"session": SID, "command": f"{launch} echo $!", "timeout": 20})
    assert out["code"] == 0
    pid = int(out["output"].strip())
    assert _gone(pid)


@needs_bash
@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_background_children_end_when_the_command_times_out(tmp_path, shared_tmp):
    ex = executor(tmp_path, [tmp_path])
    pidfile = tmp_path / "pid"
    out = ex.handle("r1", "shell", {"session": SID, "timeout": 1,
                                    "command": f"sleep 30 >/dev/null 2>&1 & echo $! > '{pidfile}'; sleep 10"})
    assert out["code"] == 124
    assert _gone(int(pidfile.read_text().strip()))


@pytest.mark.parametrize("gone", [False, True])
def test_finished_command_group_is_signalled_once(tmp_path, monkeypatch, gone):
    killed = []

    def killpg(pid, sig):
        killed.append((pid, sig))
        if gone:
            raise ProcessLookupError

    class FakeProc:
        pid, returncode = 4242, 0

        def __init__(self, argv, **_kw):
            self.stdout = io.BytesIO(b"")

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(harness_runner.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(harness_runner.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(harness_runner.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(harness_runner.Executor, "tmpdir", lambda self, sid: tmp_path)
    ex = executor(tmp_path, [tmp_path])
    assert ex.handle("r", "shell", {"session": SID, "command": "true"})["code"] == 0
    assert killed == [(4242, 9)]


# sandbox profile
def test_runner_passes_the_workspace_directories_to_the_profile(tmp_path, monkeypatch):
    argvs = []

    class FakeProc:
        pid, returncode = 1, 0

        def __init__(self, argv, **_kw):
            argvs.append(argv)
            self.stdout = io.BytesIO(b"")

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(harness_runner.subprocess, "Popen", FakeProc)
    session_tmp = tmp_path / "tmp-base" / SID
    monkeypatch.setattr(harness_runner.Executor, "tmpdir", lambda self, sid: session_tmp)
    ex = harness_runner.Executor(workspaces=tmp_path / "home" / ".agent-harness" / "workspaces", repo_roots=[],
                                 profile=PROFILE, home=tmp_path / "home", shell=bash() or "bash", min_free_gb=0)
    ex.handle("r", "shell", {"session": SID, "command": "true"})
    argv = argvs[0]
    params = {argv[i + 1].split("=", 1)[0]: argv[i + 1].split("=", 1)[1] for i, a in enumerate(argv) if a == "-D"}
    assert params == {"WORKSPACE": str(ex.workspace(SID)), "WORKSPACES": str(ex.workspaces),
                      "SESSION_TMP": str(session_tmp.resolve()), "TMP_BASE": str((tmp_path / "tmp-base").resolve()),
                      "HOME": str(tmp_path / "home")}
    used = set(re.findall(r'\(param "([A-Z_]+)"\)', PROFILE.read_text(encoding="utf-8")))
    assert used == set(params)


def _rules(text: str) -> list[str]:
    """Top-level SBPL forms in order, comments dropped, whitespace collapsed."""
    text = "\n".join(line.split(";;", 1)[0] for line in text.splitlines())
    forms, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth, start = depth + 1, i if depth == 0 else start
        elif ch == ")":
            depth -= 1
            if depth == 0:
                forms.append(" ".join(text[start:i + 1].split()))
    return forms


def _home(path: str) -> str:
    return f'(string-append (param "HOME") "{path}")'


def _last_rule(rules: list[str], op: str, path_form: str) -> str:
    """The last rule for `op` that names `path_form` (SBPL: the last matching rule wins)."""
    matching = [r for r in rules if r.startswith(("(allow ", "(deny ")) and op in r.split("(", 2)[1].split()
                and path_form in r]
    assert matching, f"no {op} rule names {path_form}"
    return matching[-1]


def test_profile_keeps_agent_harness_files_and_other_sessions_out_of_reach():
    rules = _rules(PROFILE.read_text(encoding="utf-8"))
    deny_home = _last_rule(rules, "file-read*", f'(subpath {_home("/.agent-harness")})')
    assert deny_home.startswith("(deny file-read* file-write*")
    deny_tmp = _last_rule(rules, "file-read*", '(subpath (param "TMP_BASE"))')
    assert deny_tmp.startswith("(deny file-read* file-write*")
    allow_ws = _last_rule(rules, "file-read*", '(subpath (param "WORKSPACE"))')
    assert allow_ws.startswith("(allow file-read* file-write*") and '(subpath (param "SESSION_TMP"))' in allow_ws
    assert rules.index(allow_ws) > max(rules.index(deny_home), rules.index(deny_tmp))
    metadata = _last_rule(rules, "file-read-metadata", f'(literal {_home("/.agent-harness")})')
    assert metadata.startswith("(allow file-read-metadata")
    assert '(literal (param "WORKSPACES"))' in metadata and '(literal (param "TMP_BASE"))' in metadata
    assert "subpath" not in metadata
    for git_path in ("/.git", "/.git/config", "/.git/objects/info/alternates"):
        rule = _last_rule(rules, "file-write*", f'(literal (string-append (param "WORKSPACE") "{git_path}"))')
        assert rule.startswith("(deny file-write*") and rules.index(rule) > rules.index(allow_ws)
    for git_dir in ("/.git/hooks", "/.git/info"):
        rule = _last_rule(rules, "file-write*", f'(subpath (string-append (param "WORKSPACE") "{git_dir}"))')
        assert rule.startswith("(deny file-write*") and rules.index(rule) > rules.index(allow_ws)


def test_profile_blocks_launching_programs_outside_the_sandbox():
    rules = _rules(PROFILE.read_text(encoding="utf-8"))
    for program in ("/bin/launchctl", "/usr/bin/open", "/usr/bin/osascript"):
        assert _last_rule(rules, "process-exec", f'(literal "{program}")').startswith("(deny process-exec")
    lookups = _last_rule(rules, "mach-lookup", '(global-name "com.apple.coreservices.launchservicesd")')
    assert lookups.startswith("(deny mach-lookup") and "com\\.apple\\.launchd" in lookups
    assert "(deny appleevent-send)" in rules
    assert "(allow appleevent-send" not in " ".join(rules)


@pytest.mark.parametrize("build_dir", ["/.gradle", "/.m2", "/.cache"])
def test_profile_keeps_build_tool_homes_read_only(build_dir):
    rules = _rules(PROFILE.read_text(encoding="utf-8"))
    writable = [r for r in rules if r.startswith("(allow") and "file-write*" in r.split("(", 2)[1]]
    assert writable and not [r for r in writable if _home(build_dir) in r]
    assert _last_rule(rules, "file-write*", f'(subpath {_home("/.npm")})').startswith("(allow file-write*")


def test_sandboxed_builds_use_session_directories(tmp_path, monkeypatch):
    envs = []

    class FakeProc:
        pid, returncode = 1, 0

        def __init__(self, argv, env, **_kw):
            envs.append(env)
            self.stdout = io.BytesIO(b"")

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "shared"))
    (tmp_path / "shared").mkdir()
    monkeypatch.setattr(harness_runner.subprocess, "Popen", FakeProc)
    ex = executor(tmp_path, [tmp_path])
    ex.handle("r", "shell", {"session": SID, "command": "true"})
    env = envs[0]
    assert Path(env["GRADLE_USER_HOME"]) == Path(env["TMPDIR"]) / "gradle"
    assert Path(env["GRADLE_RO_DEP_CACHE"]) == tmp_path / ".gradle" / "caches"
    assert env["MAVEN_OPTS"] == (f"-Dmaven.repo.local={Path(env['TMPDIR']) / 'm2'} "
                                 f"-Dmaven.repo.local.tail={tmp_path / '.m2' / 'repository'}")
    assert Path(env["XDG_CACHE_HOME"]) == Path(env["TMPDIR"]) / "cache"


def test_session_gradle_home_gets_its_own_copy_of_wrapper_distributions(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "shared"))
    (tmp_path / "shared").mkdir()
    dist = tmp_path / ".gradle" / "wrapper" / "dists" / "gradle-9.0-bin" / "abc123"
    dist.mkdir(parents=True)
    (dist / "gradle-9.0-bin.zip.ok").write_text("")
    (dist / "gradle-9.0" / "lib").mkdir(parents=True)
    (dist / "gradle-9.0" / "lib" / "gradle.jar").write_text("original")
    (tmp_path / ".gradle" / "jdks" / "jdk-21").mkdir(parents=True)
    (tmp_path / ".gradle" / "gradle.properties").write_text("org.gradle.jvmargs=-Xmx2g\n")
    ex = executor(tmp_path, [tmp_path])
    gradle_home = ex.tmpdir(SID) / "gradle"
    ex.seed_gradle_home(gradle_home)
    copy = gradle_home / "wrapper" / "dists" / "gradle-9.0-bin" / "abc123"
    assert (copy / "gradle-9.0-bin.zip.ok").exists()
    assert (gradle_home / "jdks" / "jdk-21").is_dir()
    assert (gradle_home / "gradle.properties").read_text() == "org.gradle.jvmargs=-Xmx2g\n"
    (copy / "gradle-9.0" / "lib" / "gradle.jar").write_text("session")
    assert (dist / "gradle-9.0" / "lib" / "gradle.jar").read_text() == "original"
    (dist / "gradle-9.1-bin").mkdir()
    ex.seed_gradle_home(gradle_home)  # once per session
    assert not (gradle_home / "wrapper" / "dists" / "gradle-9.1-bin").exists()


def test_missing_or_uncopyable_wrapper_distributions_do_not_fail_the_command(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "shared"))
    (tmp_path / "shared").mkdir()
    ex = executor(tmp_path, [tmp_path])
    gradle_home = ex.tmpdir(SID) / "gradle"
    ex.seed_gradle_home(gradle_home)
    assert not gradle_home.exists()

    def fail(src, dst):
        raise OSError("no space")

    (tmp_path / ".gradle" / "wrapper" / "dists").mkdir(parents=True)
    monkeypatch.setattr(harness_runner, "clone_tree", fail)
    ex.seed_gradle_home(gradle_home)
    assert "~/.gradle/wrapper/dists" in caplog.text


def test_clone_tree_uses_copy_on_write_cp_on_macos(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(harness_runner.sys, "platform", "darwin")
    monkeypatch.setattr(harness_runner.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)))
    harness_runner.clone_tree(tmp_path / "a", tmp_path / "b")
    assert calls == [(["/bin/cp", "-cR", str(tmp_path / "a"), str(tmp_path / "b")],
                      {"check": True, "capture_output": True, "timeout": 300})]
