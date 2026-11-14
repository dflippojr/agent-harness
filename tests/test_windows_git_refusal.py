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
from types import SimpleNamespace

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
    sh(path, "config", "gc.auto", "0")  # no background maintenance writing .git while `tree` reads it
    sh(path, "config", "maintenance.auto", "false")
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


def test_isolated_git_without_check_still_raises_the_refusal(refuse, ws):
    """`check=False` callers read a failed result as an answer (no commits ahead, a missing commit, no changes):
    the refusal is raised to them instead."""
    before = tree(ws)
    with pytest.raises(GitError, match=REFUSAL):
        projects.git(ws, "log", "--oneline", "HEAD~1..HEAD", check=False)
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


def test_prepare_fork_refuses_before_creating_the_workspace(refuse, ws, tmp_path):
    """The fork entry point refuses a git checkpoint before it makes the fork's workspace or touches a store."""
    checkpointer = object.__new__(Checkpointer)
    head = sh(ws, "rev-parse", "HEAD").strip()
    checkpointer.checkpoint = lambda sid, turn: {"turn": turn, "sha": "0" * 40, "head": head, "branch": "agent/s1"}
    checkpointer.store = lambda s: pytest.fail("the fork opened a store before refusing")
    parent = {"id": "s1", "workspace": str(ws), "base_branch": "main", "base_commit": ""}
    fork = tmp_path / "workspaces" / "s2"
    before = tree(ws)
    with pytest.raises(GitError, match=REFUSAL):
        checkpointer.prepare_fork(parent, 1, "s2", fork)
    assert not fork.exists()
    assert tree(ws) == before


def test_rewind_refuses_before_probing_the_workspace(refuse, ws):
    """A rewind probes each file it would replace by renaming it aside and back (`Store.busy`); on a git workspace
    the refusal comes first, so no file is moved."""

    class Store:
        def load_turn(self, turn):
            return {}

        def plan(self, *_a):
            pytest.fail("the rewind planned before refusing")

        busy = plan

    checkpointer = object.__new__(Checkpointer)
    checkpointer.db = SimpleNamespace(get_session=lambda sid: {"id": sid, "workspace": str(ws)})
    checkpointer.checkpoint = lambda sid, turn: {"turn": turn, "sha": "0" * 40, "head": "", "branch": ""}
    checkpointer.store = lambda s: Store()
    before = tree(ws)
    with pytest.raises(GitError, match=REFUSAL):
        checkpointer.restore("s1", 1)
    assert tree(ws) == before


def test_cleanup_keeps_a_workspace_it_cannot_check(refuse, ws, monkeypatch):
    from harness import catalog

    project = Project(name="p", repo="https://example.invalid/r.git")
    monkeypatch.setattr(catalog, "get_project", lambda *_a: project)
    maintenance = object.__new__(Maintenance)
    maintenance.cfg = maintenance.db = None
    base = sh(ws, "rev-parse", "HEAD").strip()
    s = {"id": "s1", "workspace": str(ws), "project": "p", "base_commit": base, "branch": "agent/s1"}
    before = tree(ws)
    assert REFUSAL in maintenance.unsaved_work({**s, "review": ""})         # never pushed: commits_ahead
    assert tree(ws) == before
    assert REFUSAL in maintenance.unsaved_work({**s, "review": "pushed"})   # pushed: the unpushed check
    assert tree(ws) == before


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


# Entries that ran host git without the state copy: the repo map and the git_clone tool -------------------------
def _no_processes(monkeypatch):
    def fail(*_a, **_k):
        pytest.fail("a process ran before the refusal")
    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)


def test_repo_map_refuses_host_git_and_leaves_the_map_out(refuse, ws, monkeypatch):
    from harness import repomap

    (ws / "m.py").write_bytes(b"def f():\n    return 1\n")
    _no_processes(monkeypatch)
    with pytest.raises(GitError, match=REFUSAL):
        repomap.list_source_files(ws)
    assert repomap.apply_to_prompt("base", ws) == "base"


def test_repo_map_lists_git_files_through_the_isolated_wrapper(tmp_path, monkeypatch):
    """No git process of the map's own: the listing goes through `projects.git` (a throwaway GIT_DIR)."""
    from harness import repomap

    root = tmp_path / "ws"
    (root / ".git").mkdir(parents=True)
    (root / "a.py").write_bytes(b"a = 1\n")
    (root / "b.py").write_bytes(b"b = 1\n")  # on disk but untracked: git leaves it out
    calls = []

    def fake_git(repo, *args, **_k):
        calls.append((Path(repo), args))
        return projects.GitResult(0, "a.py\0", "")
    monkeypatch.setattr(projects, "git", fake_git)
    _no_processes(monkeypatch)
    assert repomap.list_source_files(root) == ["a.py"]
    assert calls == [(root, ("ls-files", "-z"))]


@pytest.mark.parametrize("mode", ["local", "member"])
def test_git_clone_tool_refuses_host_clones(refuse, tmp_path, monkeypatch, mode):
    import asyncio

    from harness import tools

    root, repos = tmp_path / "ws", tmp_path / "repos"
    root.mkdir()
    (repos / "demo").mkdir(parents=True)
    workspace = tools.Workspace(root, None, repos, 8000, public_clone_only=mode == "member")
    url = "local:demo" if mode == "local" else "https://github.com/o/r"
    _no_processes(monkeypatch)

    async def fail(*_a, **_k):
        pytest.fail("the clone ran before the refusal")
    monkeypatch.setattr(tools, "run_cmd", fail)
    monkeypatch.setattr(clone, "_run_clone", lambda *_a, **_k: pytest.fail("the clone ran before the refusal"))
    with pytest.raises(tools.ToolError, match=REFUSAL):
        asyncio.run(workspace.git_clone(url))
    assert tree(root) == {}


def test_git_clone_tool_still_clones_in_the_sandbox(refuse, tmp_path):
    """The sandbox clone runs git inside the sandbox, not on the host: it is not refused."""
    import asyncio

    from harness import tools

    root = tmp_path / "ws"
    root.mkdir()
    ran = []

    class Sandbox:
        async def exec(self, command, timeout, network):
            ran.append(command)
            return 0, ""
    workspace = tools.Workspace(root, Sandbox(), tmp_path / "repos", 8000)
    assert asyncio.run(workspace.git_clone("https://example.invalid/o/r.git")) == \
        "cloned https://example.invalid/o/r.git into r"
    assert ran == ["git clone -- https://example.invalid/o/r.git r"]


# Rewind and fork decide from the session and checkpoint records, which the agent can't change ------------------
class _RewindStore:
    """A checkpoint store whose planning and probing fail the test, unless `allow` lets them run."""

    def __init__(self, allow=False, during_plan=None):
        self.allow, self.during_plan = allow, during_plan

    def load_turn(self, turn):
        return [], {}

    def plan(self, *_a):
        if not self.allow:
            pytest.fail("the rewind planned before refusing")
        if self.during_plan:
            self.during_plan()
        return SimpleNamespace(current="tree")

    def busy(self, *_a):
        if not self.allow:
            pytest.fail("the rewind probed files before refusing")
        return []

    def hold(self, *_a):
        pass

    release = hold

    def restore(self, *_a):
        return []


def _checkpointer(session, ckpt, store):
    checkpointer = object.__new__(Checkpointer)
    checkpointer.db = SimpleNamespace(get_session=lambda sid: session)
    checkpointer.checkpoint = lambda sid, turn: ckpt
    checkpointer.store = lambda s: store
    return checkpointer


def test_rewind_refuses_from_the_records_before_planning(refuse, tmp_path, monkeypatch):
    """A git session's rewind refuses even when the workspace has no `.git` at that moment (the agent can remove
    it): nothing is planned and no file is renamed."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"a\n")
    session = {"id": "s1", "workspace": str(workspace), "branch": "agent/s1"}
    ckpt = {"turn": 1, "sha": "0" * 40, "head": "1" * 40, "branch": "agent/s1"}
    monkeypatch.setattr("os.rename", lambda *_a: pytest.fail("a file was renamed before the refusal"))
    monkeypatch.setattr("os.replace", lambda *_a: pytest.fail("a file was replaced before the refusal"))
    before = tree(workspace)
    with pytest.raises(GitError, match=REFUSAL):
        _checkpointer(session, ckpt, _RewindStore()).restore("s1", 1)
    assert tree(workspace) == before


def test_rewind_of_a_scratch_session_runs_no_host_git(refuse, tmp_path, monkeypatch):
    """A scratch session's rewind stays without host git even if a `.git` appears while it runs."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = {"id": "s1", "workspace": str(workspace), "branch": ""}
    ckpt = {"turn": 1, "sha": "0" * 40, "head": "", "branch": ""}
    store = _RewindStore(allow=True, during_plan=lambda: (workspace / ".git").mkdir())
    done = _checkpointer(session, ckpt, store).restore("s1", 1)
    assert done.before is None


def test_fork_refuses_from_the_records(refuse, tmp_path):
    """A git checkpoint's fork refuses even when the parent workspace has no `.git` at that moment."""
    parent_ws = tmp_path / "parent"
    parent_ws.mkdir()
    parent = {"id": "s1", "workspace": str(parent_ws), "branch": "agent/s1", "base_branch": "main"}
    ckpt = {"turn": 1, "sha": "0" * 40, "head": "1" * 40, "branch": "agent/s1"}
    checkpointer = _checkpointer(parent, ckpt, None)
    checkpointer.store = lambda s: pytest.fail("the fork opened a store before refusing")
    fork = tmp_path / "workspaces" / "s2"
    with pytest.raises(GitError, match=REFUSAL):
        checkpointer.prepare_fork(parent, 1, "s2", fork)
    assert not fork.exists()


def test_manager_fork_refuses_before_allocating_the_workspace(refuse, ws):
    import asyncio

    from harness.manager import HarnessError, Manager

    parent = {"id": "s1", "workspace": str(ws), "branch": "agent/s1", "base_branch": "main", "backend": "local"}
    ckpt = {"turn": 1, "sha": "0" * 40, "head": sh(ws, "rev-parse", "HEAD").strip(), "branch": "agent/s1"}
    manager = object.__new__(Manager)
    manager.db = SimpleNamespace(get_session=lambda sid: parent)
    manager.runner = SimpleNamespace(checkpointer=_checkpointer(parent, ckpt, None))
    manager._enforce_app_caps = lambda *_a: None
    manager._check_free_space = lambda *_a: None
    manager._new_workspace = lambda *_a, **_k: pytest.fail("the fork allocated a workspace before refusing")
    with pytest.raises(HarnessError, match=REFUSAL) as raised:
        asyncio.run(manager._fork("s1", 1, "go on"))
    assert raised.value.status == 409


# The Changes view answers a refusal with an error, never an empty diff or a server error -----------------------
def test_changes_and_review_comments_report_the_refusal(tmp_path, monkeypatch):
    from harness.llm import Completion
    from test_api import make_client, wait_for

    client, m, _ = make_client(tmp_path, [Completion(content="done")])
    with client:
        sid = client.post("/sessions", json={"prompt": "look around"}).json()["id"]
        wait_for(lambda: m.db.get_session(sid)["status"] == "done")
        make_repo(Path(m.db.get_session(sid)["workspace"]) / "repo")
        comment = {"side": "new", "path": "repo/a.txt", "start_line": 1, "end_line": 1, "quoted": ["a"],
                   "comment": "rename this"}
        assert client.post(f"/sessions/{sid}/review-comments", json=comment).status_code == 201
        monkeypatch.setattr(projects, "_REFUSE_HOST_GIT", True, raising=False)
        for response in (client.get(f"/sessions/{sid}/changes"),
                         client.post(f"/sessions/{sid}/review-comments/send")):
            assert response.status_code == 409, response.text
            assert REFUSAL in response.json()["error"]["message"]
        assert len(m.db.list_review_comments(sid)) == 1  # nothing was sent
