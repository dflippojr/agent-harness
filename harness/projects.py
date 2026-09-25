"""Git-backed projects: each session works on its own branch of a clone, and the user reviews it from the phone.

All git work that touches the source repository runs host-side in the daemon, never in the sandbox, so
credentials (Git Credential Manager for URL repos) and paths outside the workspace stay out of the agent's reach.

- A local source (a path on the tower) gets the session branch fetched into it after every run, so the work is
  visible there as `agent/<session>` and survives workspace cleanup. Merge squashes it into the base branch.
- A URL source is only read (clone and fetch). Publishing the branch is an explicit push the user asks for.

Stdlib only and Python 3.9 compatible: the MacBook runner runs these same functions on the Mac.

Trust boundary for host-side Git
--------------------------------
Session workspaces are agent-writable, including `.git/config`, hooks, attributes, and `gitdir:` pointers.
Host operations on those repositories (snapshot/save, refresh, Changes, reviewed push, and the workspace side
of publish/merge) must not execute workspace-controlled commands: fsmonitor, hooks, filters, diff/textconv
helpers, credential helpers, sshCommand, remote helpers, or include-file config.

The owner's source repository and global/system Git config are trusted: merge/push into a local source, and
URL-remote credentials, still use them. Isolation is fail-closed for workspace repos: gitdir/commondir
pointers that escape the workspace are refused rather than followed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the MacBook runner imports this module without the daemon's config (or PyYAML)
    from .config import Project

AGENT_NAME = "Agent (agent-harness)"
AGENT_EMAIL = "agent@agent-harness.local"

# Remote-helper URLs (`ext::`, `foo::bar`) execute an arbitrary `git-remote-*` program. IPv6 (`[::1]`) is fine.
_REMOTE_HELPER_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*::")
_SAFE_CONFIG_KEYS = {
    "core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates",
    "core.ignorecase", "core.precomposeunicode", "core.protectntfs", "core.protecthfs",
    "core.autocrlf", "core.eol", "core.symlinks", "core.quotepath",
    "extensions.objectformat", "extensions.preciousobjects", "extensions.partialclone",
    "user.name", "user.email",
}
_SAFE_REMOTE_KEYS = {"url", "pushurl", "fetch", "mirror", "prune", "tagopt", "promisor", "partialclonefilter"}
_SAFE_BRANCH_KEYS = {"remote", "merge", "pushremote", "rebase", "description"}
_STATE_FILES = ("HEAD", "packed-refs", "FETCH_HEAD", "ORIG_HEAD", "shallow")
_STATE_DIRS = ("refs", "logs")


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


def _resolve(path: Path) -> Path:
    r"""Path.resolve() without Windows' `\\?\` prefix, which breaks containment checks."""
    text = str(path.resolve())
    return Path(text[4:]) if text.startswith("\\\\?\\") else Path(text)


def _contained(path: Path, root: Path) -> bool:
    path_s, root_s = os.path.normcase(str(_resolve(path))), os.path.normcase(str(_resolve(root)))
    try:
        return os.path.commonpath([path_s, root_s]) == root_s
    except ValueError:
        return False


def _safe_git_url(url: str) -> bool:
    text = url.strip()
    if not text or any(ch in text for ch in "\r\n\x00") or text.startswith("-"):
        return False
    if _REMOTE_HELPER_URL.match(text) or "proxycommand" in text.lower():
        return False
    return True


def _safe_config_key(key: str, value: str) -> bool:
    k = key.lower()
    if k in _SAFE_CONFIG_KEYS:
        return True
    parts = k.split(".")
    if len(parts) >= 3 and parts[0] == "remote" and parts[-1] in _SAFE_REMOTE_KEYS:
        return parts[-1] not in {"url", "pushurl"} or _safe_git_url(value)
    if len(parts) >= 3 and parts[0] == "branch" and parts[-1] in _SAFE_BRANCH_KEYS:
        return True
    return False


def _run(git_args: list[str], timeout: float = 600, check: bool = True, input_: str | None = None,
         env: dict | None = None) -> GitResult:
    proc = subprocess.run(
        ["git", *git_args], input=input_, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, stdin=None if input_ is not None else subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=env,
    )
    result = GitResult(proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(git_args[:6])} failed: {result.text[-1500:]}")
    return result


def _isolate_env() -> dict:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_EDITOR"] = "true"
    env["GIT_PAGER"] = "cat"
    env["GIT_ATTR_NOSYSTEM"] = "1"
    env["GIT_ASKPASS"] = env.get("GIT_ASKPASS") or ""
    return env


def _read_gitdir_pointer(text: str, base: Path) -> Path:
    for raw in text.splitlines():
        line = raw.strip()
        if line.lower().startswith("gitdir:"):
            target = line.split(":", 1)[1].strip().strip('"')
            path = Path(target)
            return path if path.is_absolute() else _resolve(base / path)
    raise GitError("invalid gitdir pointer")


def _resolve_workspace_git(repo: Path) -> tuple[Path, Path, Path]:
    """Return (git dir for index, metadata dir for objects/refs/config, work tree).

    Follows `.git` files and `commondir` only when the target stays inside `repo`. Linked worktrees store the
    index in the worktree git dir and objects/refs/config in the common dir.
    """
    work_tree = _resolve(repo)
    dot_git = repo / ".git"
    if not dot_git.exists():
        raise GitError(f"{repo} is not a git repository")
    if dot_git.is_file():
        git_dir = _read_gitdir_pointer(dot_git.read_text(encoding="utf-8", errors="replace"), repo)
    else:
        git_dir = _resolve(dot_git)
    if not _contained(git_dir, work_tree):
        raise GitError("refusing git directory outside the workspace")
    metadata = git_dir
    common = git_dir / "commondir"
    if common.is_file():
        target = common.read_text(encoding="utf-8", errors="replace").strip()
        metadata = Path(target) if Path(target).is_absolute() else _resolve(git_dir / target)
        if not _contained(metadata, work_tree):
            raise GitError("refusing git commondir outside the workspace")
    return git_dir, metadata, work_tree


def _mirror_tree(src: Path, dst: Path) -> None:
    """Copy `src` onto `dst`, then remove dest entries that `src` no longer has.

    `shutil.copytree(..., dirs_exist_ok=True)` only adds/overwrites, so a fetch
    `--prune` in the isolated git-dir would otherwise leave stale loose refs in
    the real repository.
    """
    dst.mkdir(parents=True, exist_ok=True)
    seen = set()
    for child in src.iterdir():
        seen.add(child.name)
        _mirror_entry(child, dst / child.name)
    for child in sorted(dst.iterdir()):
        if child.name in seen:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _mirror_entry(child: Path, target: Path) -> None:
    if child.is_dir() and not child.is_symlink():
        if target.is_symlink() or target.is_file():
            target.unlink()
        _mirror_tree(child, target)
    else:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        shutil.copy2(child, target)


def _copy_git_state(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for name in _STATE_FILES:
        s, d = src / name, dst / name
        if s.is_file():
            shutil.copy2(s, d)
    for name in _STATE_DIRS:
        s = src / name
        if s.is_dir():
            _mirror_tree(s, dst / name)


def _write_isolated_config(real_config: Path, dest: Path, hooks: Path) -> None:
    dest.write_text(
        "[core]\n\trepositoryformatversion = 0\n\tfilemode = false\n\tbare = false\n"
        "\tlogallrefupdates = true\n\tautocrlf = false\n\tfsmonitor =\n"
        f"\thooksPath = {hooks.as_posix()}\n",
        encoding="utf-8",
    )
    if not real_config.is_file():
        return
    listed = _run(["config", "--file", str(real_config), "--null", "--list"], check=False, env=_isolate_env())
    if listed.code != 0 or not listed.out:
        return
    for item in listed.out.split("\0"):
        if not item or "\n" not in item:
            continue
        key, value = item.split("\n", 1)
        if _safe_config_key(key, value):
            _run(["config", "--file", str(dest), key, value], check=False, env=_isolate_env())


def _isolated_flags(work_tree: Path, tmp: Path, hooks: Path) -> list[str]:
    # -c overrides beat any copied key and any trusted global config for these executable settings.
    return [
        "-c", "core.quotepath=off",
        "-c", f"safe.directory={work_tree.as_posix()}",
        "-c", f"safe.directory={tmp.as_posix()}",
        "-c", "core.fsmonitor=",
        "-c", "core.useBuiltinFSMonitor=false",
        "-c", f"core.hooksPath={hooks.as_posix()}",
        "-c", "core.pager=cat",
        "-c", "core.editor=true",
        "-c", "commit.gpgSign=false",
        "--git-dir", str(tmp),
        "--work-tree", str(work_tree),
        "-C", str(work_tree),
    ]


def git(repo: Path | str | None, *args: str, timeout: float = 600, check: bool = True,
        input_: str | None = None, trusted: bool = False) -> GitResult:
    """Run git. Workspace repositories are isolated unless `trusted=True` (owner source repos only).

    Isolation uses a throwaway GIT_DIR with an allowlisted config, shared objects/index, and copied refs, so
    workspace hooks, filters, fsmonitor, credential helpers, and gitdir escapes cannot run on the host.
    Global/system config is kept so reviewed fetch/push can still use the owner's credential helper.
    """
    # No core.autocrlf override on trusted repos: source repos keep their own checkout settings (Git for Windows
    # defaults to true), or every CRLF file there would look modified. Workspaces get autocrlf=false at clone.
    if repo is None or trusted:
        cmd = ["-c", "core.quotepath=off"]
        if repo is not None:
            cmd += ["-c", f"safe.directory={Path(repo).as_posix()}", "-C", str(repo)]
        return _run(cmd + list(args), timeout=timeout, check=check, input_=input_)

    work = Path(repo)
    try:
        index_dir, metadata, work_tree = _resolve_workspace_git(work)
    except GitError as e:
        if check:
            raise
        return GitResult(1, "", str(e))

    with tempfile.TemporaryDirectory(prefix="harness-git-") as raw_tmp:
        tmp, hooks = Path(raw_tmp) / "git", Path(raw_tmp) / "hooks"
        hooks.mkdir(parents=True)
        tmp.mkdir()
        _copy_git_state(metadata, tmp)
        _write_isolated_config(metadata / "config", tmp / "config", hooks)
        env = _isolate_env()
        env["GIT_INDEX_FILE"] = str(index_dir / "index")
        env["GIT_OBJECT_DIRECTORY"] = str(metadata / "objects")
        (metadata / "objects").mkdir(parents=True, exist_ok=True)
        try:
            result = _run(_isolated_flags(work_tree, tmp, hooks) + list(args),
                          timeout=timeout, check=False, input_=input_, env=env)
        finally:
            _copy_git_state(tmp, metadata)
        if check and result.code != 0:
            raise GitError(f"git {' '.join(args[:3])} failed: {result.text[-1500:]}")
        return result


def is_url(repo: str) -> bool:
    return "://" in repo or repo.startswith("git@")


def branch_name(sid: str) -> str:
    return f"agent/{sid}"


def source_path(project: Project) -> Path | None:
    return None if is_url(project.repo) else Path(project.repo)


def _is_bare(repo: Path) -> bool:
    return git(repo, "rev-parse", "--is-bare-repository", trusted=True).out.strip() == "true"


def prepare(project: Project, workspace: Path, sid: str, shared: bool = False) -> dict:
    """Clone the project into the (empty) workspace and create the session branch. `shared` (local sources only)
    borrows the source's objects through git alternates instead of copying them."""
    src = project.repo
    if not is_url(src) and not Path(src).is_dir():
        raise GitError(f"project {project.name}: repository {src} doesn't exist", 400)
    workspace.mkdir(parents=True, exist_ok=True)
    if any(workspace.iterdir()):
        raise GitError(f"workspace {workspace} is not empty")
    args = ["clone", "--shared" if shared else "--no-hardlinks", "--config", "core.autocrlf=false"]  # LF checkout
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
    cfg = workspace / ".git" / "config"  # real file: isolation would otherwise throw the identity away
    for key, value in (("user.name", AGENT_NAME), ("user.email", AGENT_EMAIL)):
        git(None, "config", "--file", str(cfg), key, value)
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
        "commit", "-q", "--no-verify", "--no-gpg-sign", "-m", message)
    return True


def head(workspace: Path, trusted: bool = False) -> str:
    return git(workspace, "rev-parse", "HEAD", trusted=trusted).out.strip()


def commits_ahead(workspace: Path, base_commit: str) -> list[str]:
    out = git(workspace, "log", "--format=%h %s", "--no-color", f"{base_commit}..HEAD", check=False).out
    return out.splitlines()


def publish_local(project: Project, workspace: Path, branch: str) -> bool:
    """Copy the session branch into a local source repository. No-op for URL projects.

    Push from the isolated workspace into the trusted source so receive-pack (and its hooks) run in the source,
    not upload-pack inside the agent-writable clone.
    """
    src = source_path(project)
    if src is None or not (workspace / ".git").exists():
        return False
    git(workspace, "push", "--quiet", "--no-verify", str(src), f"+{branch}:refs/heads/{branch}")
    return True


def push(project: Project, workspace: Path, branch: str) -> str:
    """Push the session branch to the URL source, with the daemon's (the user's) git credentials."""
    if not is_url(project.repo):
        raise GitError("push is for URL projects; local projects already have the branch", 400)
    result = git(workspace, "push", "--quiet", "--no-verify", "origin", f"{branch}:refs/heads/{branch}",
                 timeout=300, check=False)
    if result.code != 0:
        raise GitError(f"push failed: {result.text[-1500:]}")
    return f"pushed {branch} to {project.repo}"


def _delete_branch(src: Path, branch: str) -> None:
    if git(src, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False, trusted=True).code == 0:
        git(src, "branch", "-q", "-D", branch, trusted=True)


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
    subjects = git(src, "log", "--reverse", "--format=- %s", f"{base_branch}..{branch}",
                   check=False, trusted=True).out.strip()
    if not subjects:
        _delete_branch(src, branch)
        return {"merged": False, "message": "nothing to merge: the branch has no commits beyond the base"}
    message = f"{title}\n\nSquash-merged from {branch} (agent-harness session {sid}).\n\n{subjects}\n"

    if _is_bare(src):
        tmp = Path(tempfile.mkdtemp(prefix="harness-merge-"))
        worktree = tmp / "wt"
        try:
            git(src, "worktree", "add", "-q", str(worktree), base_branch, trusted=True)
            _squash_commit(worktree, branch, message)
            commit = head(worktree, trusted=True)
        finally:
            git(src, "worktree", "remove", "--force", str(worktree), check=False, trusted=True)
            git(src, "worktree", "prune", check=False, trusted=True)
    else:
        current = git(src, "rev-parse", "--abbrev-ref", "HEAD", trusted=True).out.strip()
        if current != base_branch:
            raise GitError(f"{src} is on branch {current}, not {base_branch}; switch it back to merge")
        if git(src, "diff", "--cached", "--quiet", check=False, trusted=True).code != 0:
            raise GitError(f"{src} has staged changes; commit or unstage them first")
        _squash_commit(src, branch, message)
        commit = head(src, trusted=True)
    _delete_branch(src, branch)
    return {"merged": True, "commit": commit, "message": f"squash-merged into {base_branch} as {commit[:10]}"}


def _squash_commit(repo: Path, branch: str, message: str) -> None:
    result = git(repo, "merge", "--squash", branch, check=False, trusted=True)
    if result.code != 0:
        conflicted = git(repo, "diff", "--name-only", "--diff-filter=U", check=False, trusted=True).out.split()
        if conflicted:
            git(repo, "reset", "--merge", check=False, trusted=True)
            raise GitError("merge conflicts in " + ", ".join(conflicted) +
                           ". Ask the agent to merge origin/<base> into its branch and resolve them, then retry.")
        raise GitError(f"merge refused: {result.text[-1000:]}")
    if git(repo, "diff", "--cached", "--quiet", check=False, trusted=True).code == 0:
        return  # the changes are already on the base branch
    result = git(repo, "commit", "-q", "--no-verify", "-F", "-", check=False, input_=message, trusted=True)
    if result.code != 0:
        git(repo, "reset", "--merge", check=False, trusted=True)
        raise GitError(f"commit failed: {result.text[-1000:]}")


def discard(project: Project, branch: str) -> None:
    """Delete the session branch from a local source. The workspace is removed by the caller."""
    src = source_path(project)
    if src is not None and src.is_dir():
        _delete_branch(src, branch)
