"""Git-backed projects: each session works on its own branch of a clone, and the user reviews it from the phone.

All git work that touches the source repository runs host-side in the daemon, never in the sandbox, so
credentials (Git Credential Manager for URL repos) and paths outside the workspace stay out of the agent's reach.

- A local source (a path on the tower) gets the session branch fetched into it after every run, so the work is
  visible there as `agent/<session>` and survives workspace cleanup. Merge squashes it into the base branch.
- A URL source is only read (clone and fetch). Publishing the branch is an explicit push the user asks for.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Project

AGENT_NAME = "Agent (agent-harness)"
AGENT_EMAIL = "agent@agent-harness.local"


class GitError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


@dataclass
class GitResult:
    code: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return (self.out + self.err).strip()


def git(repo: Path | str | None, *args: str, timeout: float = 600, check: bool = True,
        input_: str | None = None) -> GitResult:
    # No core.autocrlf override: source repos keep their own checkout settings (Git for Windows defaults to
    # true), or every CRLF file there would look modified. Workspaces get autocrlf=false at clone time.
    cmd = ["git", "-c", "core.quotepath=off"]
    if repo is not None:
        cmd += ["-c", f"safe.directory={Path(repo).as_posix()}", "-C", str(repo)]
    proc = subprocess.run(
        cmd + list(args), input=input_, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, stdin=None if input_ is not None else subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    result = GitResult(proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])} failed: {result.text[-1500:]}")
    return result


def is_url(repo: str) -> bool:
    return "://" in repo or repo.startswith("git@")


def branch_name(sid: str) -> str:
    return f"agent/{sid}"


def source_path(project: Project) -> Path | None:
    return None if is_url(project.repo) else Path(project.repo)


def _is_bare(repo: Path) -> bool:
    return git(repo, "rev-parse", "--is-bare-repository").out.strip() == "true"


def prepare(project: Project, workspace: Path, sid: str) -> dict:
    """Clone the project into the (empty) workspace and create the session branch."""
    src = project.repo
    if not is_url(src) and not Path(src).is_dir():
        raise GitError(f"project {project.name}: repository {src} doesn't exist", 400)
    workspace.mkdir(parents=True, exist_ok=True)
    if any(workspace.iterdir()):
        raise GitError(f"workspace {workspace} is not empty")
    args = ["clone", "--no-hardlinks", "--config", "core.autocrlf=false"]  # LF checkout for the Linux sandbox
    if project.base_branch:
        args += ["--branch", project.base_branch]
    result = git(None, *args, "--", src, str(workspace), check=False)
    if result.code != 0:
        for child in workspace.iterdir():  # so a retry starts from an empty workspace
            shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
        raise GitError(f"could not clone {src}: {result.text[-1500:]}")
    base_branch = git(workspace, "rev-parse", "--abbrev-ref", "HEAD").out.strip()
    base_commit = git(workspace, "rev-parse", "HEAD").out.strip()
    branch = branch_name(sid)
    git(workspace, "checkout", "-q", "-b", branch)
    for key, value in (("user.name", AGENT_NAME), ("user.email", AGENT_EMAIL)):
        git(workspace, "config", key, value)
    return {"branch": branch, "base_branch": base_branch, "base_commit": base_commit}


def refresh_origin(workspace: Path) -> str:
    """Fetch the source so `origin/<base>` is current when the agent starts a run. Returns an error or ''."""
    result = git(workspace, "fetch", "--quiet", "--prune", "origin", timeout=300, check=False)
    return "" if result.code == 0 else result.text[-500:]


def snapshot(workspace: Path, message: str) -> bool:
    """Commit anything the agent left uncommitted, so the branch holds all of its work. Returns True if it did."""
    if not (workspace / ".git").exists():
        return False
    if not git(workspace, "status", "--porcelain").out.strip():
        return False
    git(workspace, "add", "-A")
    git(workspace, "-c", f"user.name={AGENT_NAME}", "-c", f"user.email={AGENT_EMAIL}",
        "commit", "-q", "--no-verify", "-m", message)
    return True


def head(workspace: Path) -> str:
    return git(workspace, "rev-parse", "HEAD").out.strip()


def commits_ahead(workspace: Path, base_commit: str) -> list[str]:
    out = git(workspace, "log", "--format=%h %s", "--no-color", f"{base_commit}..HEAD", check=False).out
    return out.splitlines()


def publish_local(project: Project, workspace: Path, branch: str) -> bool:
    """Copy the session branch into a local source repository. No-op for URL projects."""
    src = source_path(project)
    if src is None or not (workspace / ".git").exists():
        return False
    git(src, "fetch", "--quiet", "--no-tags", str(workspace), f"+{branch}:refs/heads/{branch}")
    return True


def push(project: Project, workspace: Path, branch: str) -> str:
    """Push the session branch to the URL source, with the daemon's (the user's) git credentials."""
    if not is_url(project.repo):
        raise GitError("push is for URL projects; local projects already have the branch", 400)
    result = git(workspace, "push", "--quiet", "origin", f"{branch}:refs/heads/{branch}", timeout=300, check=False)
    if result.code != 0:
        raise GitError(f"push failed: {result.text[-1500:]}")
    return f"pushed {branch} to {project.repo}"


def _delete_branch(src: Path, branch: str) -> None:
    if git(src, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).code == 0:
        git(src, "branch", "-q", "-D", branch)


def merge(project: Project, workspace: Path, sid: str, branch: str, base_branch: str, title: str) -> dict:
    """Squash-merge the session branch into the base branch of a local source repository.

    A checked-out source is merged in place, which needs it to be on the base branch with nothing staged
    (unrelated unstaged edits are fine; git refuses if the merge would touch them). A bare source is merged in a
    temporary worktree.
    """
    src = source_path(project)
    if src is None:
        raise GitError("merging is for local projects; push the branch and open a pull request instead", 400)
    snapshot(workspace, f"Work in progress from session {sid}")
    publish_local(project, workspace, branch)
    subjects = git(src, "log", "--reverse", "--format=- %s", f"{base_branch}..{branch}", check=False).out.strip()
    if not subjects:
        _delete_branch(src, branch)
        return {"merged": False, "message": "nothing to merge: the branch has no commits beyond the base"}
    message = f"{title}\n\nSquash-merged from {branch} (agent-harness session {sid}).\n\n{subjects}\n"

    if _is_bare(src):
        tmp = Path(tempfile.mkdtemp(prefix="harness-merge-"))
        worktree = tmp / "wt"
        try:
            git(src, "worktree", "add", "-q", str(worktree), base_branch)
            _squash_commit(worktree, branch, message)
            commit = head(worktree)
        finally:
            git(src, "worktree", "remove", "--force", str(worktree), check=False)
            git(src, "worktree", "prune", check=False)
    else:
        current = git(src, "rev-parse", "--abbrev-ref", "HEAD").out.strip()
        if current != base_branch:
            raise GitError(f"{src} is on branch {current}, not {base_branch}; switch it back to merge")
        if git(src, "diff", "--cached", "--quiet", check=False).code != 0:
            raise GitError(f"{src} has staged changes; commit or unstage them first")
        _squash_commit(src, branch, message)
        commit = head(src)
    _delete_branch(src, branch)
    return {"merged": True, "commit": commit, "message": f"squash-merged into {base_branch} as {commit[:10]}"}


def _squash_commit(repo: Path, branch: str, message: str) -> None:
    result = git(repo, "merge", "--squash", branch, check=False)
    if result.code != 0:
        conflicted = git(repo, "diff", "--name-only", "--diff-filter=U", check=False).out.split()
        if conflicted:
            git(repo, "reset", "--merge", check=False)
            raise GitError("merge conflicts in " + ", ".join(conflicted) +
                           ". Ask the agent to merge origin/<base> into its branch and resolve them, then retry.")
        raise GitError(f"merge refused: {result.text[-1000:]}")
    if git(repo, "diff", "--cached", "--quiet", check=False).code == 0:
        return  # the changes are already on the base branch
    result = git(repo, "commit", "-q", "--no-verify", "-F", "-", check=False, input_=message)
    if result.code != 0:
        git(repo, "reset", "--merge", check=False)
        raise GitError(f"commit failed: {result.text[-1000:]}")


def discard(project: Project, branch: str) -> None:
    """Delete the session branch from a local source. The workspace is removed by the caller."""
    src = source_path(project)
    if src is not None and src.is_dir():
        _delete_branch(src, branch)
