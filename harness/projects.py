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
pointers that escape the workspace are refused rather than followed, and so are links in the ref state the host
copies in and out (HEAD, refs, logs and the other state files).
"""

from __future__ import annotations

import os
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

if TYPE_CHECKING:  # the MacBook runner imports this module without the daemon's config (or PyYAML)
    from .config import Project

AGENT_NAME = "Agent (agent-harness)"
AGENT_EMAIL = "agent@agent-harness.local"

_SAFE_CONFIG_KEYS = {
    "core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates",
    "core.ignorecase", "core.precomposeunicode", "core.protectntfs", "core.protecthfs",
    "core.autocrlf", "core.eol", "core.symlinks", "core.quotepath",
    "extensions.objectformat", "extensions.preciousobjects", "extensions.partialclone",
    "user.name", "user.email",
}
# No url/pushurl/promisor: host fetch and push name the project's repository from the daemon's record, so a remote
# URL written into the workspace config never decides where host-side git connects.
_SAFE_REMOTE_KEYS = {"fetch", "mirror", "prune", "tagopt", "partialclonefilter"}
_SAFE_BRANCH_KEYS = {"remote", "merge", "pushremote", "rebase", "description"}
_STATE_FILES = ("HEAD", "packed-refs", "FETCH_HEAD", "ORIG_HEAD", "shallow")
_STATE_DIRS = ("refs", "logs")
# threading.Lock supports weak references since Python 3.2; idle workspace locks can be reclaimed.
# https://docs.python.org/3.12/library/weakref.html
_GIT_STATE_LOCKS: WeakValueDictionary = WeakValueDictionary()
_GIT_STATE_LOCKS_GUARD = threading.Lock()
# Where host-side git keeps its throwaway git dirs, hooks dirs and merge worktrees. None means the system temp
# directory (the daemon). The Mac runner sets a private directory that its shell sandbox can't read or write.
TEMP_ROOT: Path | None = None


def _temp_dir() -> str | None:
    if TEMP_ROOT is None:
        return None
    TEMP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if TEMP_ROOT.is_symlink() or not TEMP_ROOT.is_dir():
        raise GitError(f"{TEMP_ROOT} isn't a plain directory")
    return str(TEMP_ROOT)


def _git_state_lock(metadata: Path):
    """Share a lock for aliases of a metadata directory, without retaining idle workspaces."""
    key = os.path.normcase(str(_resolve(metadata)))
    with _GIT_STATE_LOCKS_GUARD:
        lock = _GIT_STATE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _GIT_STATE_LOCKS[key] = lock
        return lock


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


def _safe_config_key(key: str) -> bool:
    k = key.lower()
    if k in _SAFE_CONFIG_KEYS:
        return True
    parts = k.split(".")
    if len(parts) >= 3 and parts[0] == "remote" and parts[-1] in _SAFE_REMOTE_KEYS:
        return True
    if len(parts) >= 3 and parts[0] == "branch" and parts[-1] in _SAFE_BRANCH_KEYS:
        return True
    return False


def _run(git_args: list[str], timeout: float = 600, check: bool = True, input_: str | None = None,
         env: dict | None = None, cwd: str | None = None) -> GitResult:
    proc = subprocess.run(
        ["git", *git_args], input=input_, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, stdin=None if input_ is not None else subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=env, cwd=cwd,
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
            return _resolve(path if path.is_absolute() else base / path)
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
        metadata = _resolve(Path(target) if Path(target).is_absolute() else git_dir / target)
        if not _contained(metadata, work_tree):
            raise GitError("refusing git commondir outside the workspace")
    return git_dir, metadata, work_tree


_isjunction = getattr(os.path, "isjunction", lambda _path: False)  # Python 3.12+; Windows junctions are links too
_O_BINARY = getattr(os, "O_BINARY", 0)
# Read state files without following a final-component link and without blocking on a FIFO (POSIX only).
_NOFOLLOW_READ = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | _O_BINARY
# POSIX: walk and copy git state through directory descriptors opened without following links, so a path component
# the workspace swaps for a link mid-copy cannot redirect a read or a write. Windows has no *at calls; there the
# directory chain is checked for links right before each operation instead (a narrower window, not a closed one).
_FD_OPS = (hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd
           and os.stat in os.supports_dir_fd and os.mkdir in os.supports_dir_fd and os.rename in os.supports_dir_fd
           and os.unlink in os.supports_dir_fd and os.rmdir in os.supports_dir_fd and os.scandir in os.supports_fd
           and os.chmod in os.supports_fd and os.utime in os.supports_fd)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_MISSING, _FILE, _DIR, _OTHER = "missing", "file", "dir", "other"


class _StateDir:
    """A directory of git state, addressed by entry name, that never follows a link (see `_FD_OPS`).

    Every call names an entry through `_at`: the bare name with `fd` as `dir_fd` on POSIX, or the full path (with
    `dir_fd=None`) after re-checking the directory chain elsewhere.
    """

    def __init__(self, path: Path, fd: int | None = None):
        self.path, self.fd = path, fd

    @classmethod
    def open(cls, path: Path) -> _StateDir:
        """Open an existing canonical directory, refusing a link anywhere on its path (one swapped in after the path
        was resolved, say). On POSIX every component is opened without following links."""
        if not _FD_OPS:
            state = cls(path)
            state._check_chain()
            if not path.is_dir():
                raise GitError(f"refusing git state directory {path.name}: not a directory")
            return state
        fd = os.open(path.anchor, _DIR_FLAGS)
        try:
            for part in path.parts[1:]:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
        except BaseException:
            os.close(fd)
            raise
        return cls(path, fd)

    def __enter__(self) -> _StateDir:
        return self

    def __exit__(self, *_exc) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _check_chain(self) -> None:
        """No component of the canonical path, up to the filesystem root, may have become a link."""
        current = self.path
        while True:
            if _is_link(current):
                raise GitError(f"refusing symlinked git state {current.name}")
            if current == current.parent:
                return
            current = current.parent

    def _at(self, name: str) -> str:
        if self.fd is not None:
            return name
        self._check_chain()
        return str(self.path / name)

    def kind(self, name: str) -> str:
        where = self._at(name)
        if self.fd is None and _isjunction(where):
            return _OTHER
        try:
            mode = os.stat(where, dir_fd=self.fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return _MISSING
        return _FILE if stat.S_ISREG(mode) else _DIR if stat.S_ISDIR(mode) else _OTHER

    def names(self) -> list[str]:
        with os.scandir(self.fd if self.fd is not None else self._at("")) as entries:
            return [entry.name for entry in entries]

    def subdir(self, name: str) -> _StateDir:
        if self.fd is not None:
            return _StateDir(self.path / name, os.open(name, _DIR_FLAGS, dir_fd=self.fd))
        if self.kind(name) != _DIR:
            raise GitError(f"refusing git state {name}: not a plain directory")
        return _StateDir(self.path / name)

    def open_or_create_subdir(self, name: str) -> _StateDir:
        """The subdirectory `name`, created if missing; anything else in its place is refused, not replaced."""
        kind = self.kind(name)
        if kind == _MISSING:
            os.mkdir(self._at(name), dir_fd=self.fd)
        elif kind != _DIR:
            raise GitError(f"refusing git state directory {name}: not a plain directory")
        return self.subdir(name)

    def ensure_subdir(self, name: str) -> _StateDir:
        """The subdirectory `name`, created first; anything else in its place (a link, a file) is removed."""
        kind = self.kind(name)
        if kind not in (_DIR, _MISSING):
            self.remove(name)
        if kind != _DIR:
            os.mkdir(self._at(name), dir_fd=self.fd)
        return self.subdir(name)

    def remove(self, name: str) -> None:
        """Remove an entry: a directory with its contents, or a file or link itself (never what a link points to)."""
        kind = self.kind(name)
        if kind == _DIR:
            with self.subdir(name) as sub:
                for child in sub.names():
                    sub.remove(child)
            os.rmdir(self._at(name), dir_fd=self.fd)
        elif kind != _MISSING:
            where = self._at(name)
            if self.fd is None and _isjunction(where):
                os.rmdir(where)  # removes the junction, not its target
            else:
                os.unlink(where, dir_fd=self.fd)

    def open_read(self, name: str) -> int:
        """A descriptor on regular file `name`, never through a link."""
        if self.fd is None and self.kind(name) != _FILE:
            raise GitError(f"refusing git state {name}: not a regular file")
        fd = os.open(self._at(name), _NOFOLLOW_READ, dir_fd=self.fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise GitError(f"refusing git state {name}: not a regular file")
        return fd

    def write(self, name: str, source_fd: int) -> None:
        """Copy `source_fd` to `name` through a new temp file renamed over it, so a link at `name` is replaced rather
        than written through. The short temp name starts with a dot and ends in `.lock`: git never reads it as a ref,
        and its length doesn't depend on `name`."""
        st = os.fstat(source_fd)
        temp = _temp_name()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | _O_BINARY
        out = os.open(self._at(temp), flags, 0o600, dir_fd=self.fd)
        try:
            try:
                while chunk := os.read(source_fd, 1 << 16):
                    view = memoryview(chunk)
                    while view:  # os.write may write less than asked
                        view = view[os.write(out, view):]
                # Mode and times through the descriptor where the OS allows it, else by the just-checked path.
                target = out if self.fd is not None else self._at(temp)
                os.chmod(target, stat.S_IMODE(st.st_mode))
                os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
            finally:
                os.close(out)
            os.replace(self._at(temp), self._at(name), src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except BaseException:
            self.remove(temp)
            raise


def _temp_name() -> str:
    return f".tmp-{secrets.token_hex(8)}.lock"


def _is_link(path: Path) -> bool:
    return path.is_symlink() or _isjunction(path)


def _copy_state_file(src: _StateDir, dst: _StateDir, name: str) -> None:
    source_fd = src.open_read(name)
    try:
        if dst.kind(name) == _DIR:
            dst.remove(name)
        dst.write(name, source_fd)
    finally:
        os.close(source_fd)


def _mirror_tree(src: _StateDir, dst: _StateDir) -> None:
    """Copy `src` onto `dst`, then remove dest entries that `src` no longer has.

    `shutil.copytree(..., dirs_exist_ok=True)` only adds/overwrites, so a fetch
    `--prune` in the isolated git-dir would otherwise leave stale loose refs in
    the real repository. Links and special files are refused in `src`; in `dst` they are replaced, never followed.
    """
    seen = set()
    for name in src.names():
        seen.add(name)
        kind = src.kind(name)
        if kind == _DIR:
            with src.subdir(name) as sub_src, dst.ensure_subdir(name) as sub_dst:
                _mirror_tree(sub_src, sub_dst)
        elif kind == _FILE:
            _copy_state_file(src, dst, name)
        else:
            raise GitError(f"refusing symlinked or special git state {name}")
    for name in sorted(dst.names()):
        if name not in seen:
            dst.remove(name)


def _copy_git_state(src: Path, dst: Path) -> None:
    """Copy the ref state (`_STATE_FILES`, `_STATE_DIRS`) from git dir `src` to git dir `dst`, both canonical paths.

    Fails closed with GitError on a link or special file in `src`, or a state entry of the wrong type.
    """
    try:
        # `dst` is created, if missing, under its checked parent, so a link swapped onto its path creates nothing.
        with _StateDir.open(src) as s, _StateDir.open(dst.parent) as parent, \
                parent.open_or_create_subdir(dst.name) as d:
            for name in _STATE_FILES:
                kind = s.kind(name)
                if kind == _FILE:
                    _copy_state_file(s, d, name)
                elif kind != _MISSING:
                    raise GitError(f"refusing symlinked or special git state {name}")
            for name in _STATE_DIRS:
                kind = s.kind(name)
                if kind == _DIR:
                    with s.subdir(name) as sub_src, d.ensure_subdir(name) as sub_dst:
                        _mirror_tree(sub_src, sub_dst)
                elif kind != _MISSING:
                    raise GitError(f"refusing symlinked or special git state {name}")
    except GitError:
        raise
    except OSError as e:  # a link or a type change raced in after a check (ELOOP, ENOTDIR, ...): fail closed
        raise GitError(f"could not copy git state: {e.strerror or e}") from e


def _write_isolated_config(dest: Path, hooks: Path) -> None:
    dest.write_text(
        "[core]\n\trepositoryformatversion = 0\n\tfilemode = false\n\tbare = false\n"
        "\tlogallrefupdates = true\n\tautocrlf = false\n\tfsmonitor =\n"
        f"\thooksPath = {hooks.as_posix()}\n",
        encoding="utf-8",
    )


def _allowlisted_config(real_config: Path) -> list[tuple[str, str]]:
    """The real repo config's allowlisted entries: one `git config --list` process, no per-key writes."""
    if not real_config.is_file():
        return []
    listed = _run(["config", "--file", str(real_config), "--null", "--list"], check=False, env=_isolate_env())
    if listed.code != 0 or not listed.out:
        return []
    entries = []
    for item in listed.out.split("\0"):
        if not item or "\n" not in item:
            continue
        key, value = item.split("\n", 1)
        if _safe_config_key(key):
            entries.append((key, value))
    return entries


def _inject_config(env: dict, entries: list[tuple[str, str]]) -> None:
    """Hand the allowlisted entries to git through GIT_CONFIG_COUNT/KEY_n/VALUE_n (appended after any existing)."""
    try:
        start = int(env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        start = 0
    for i, (key, value) in enumerate(entries, start):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    env["GIT_CONFIG_COUNT"] = str(start + len(entries))


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

    try:
        with isolated_git_dir(Path(repo), _isolate_env()) as (flags, env, _tmp):
            result = _run(flags + list(args), timeout=timeout, check=False, input_=input_, env=env)
    except GitError as e:
        if check:
            raise
        return GitResult(1, "", str(e))
    if check and result.code != 0:
        raise GitError(f"git {' '.join(args[:3])} failed: {result.text[-1500:]}")
    return result


@contextmanager
def isolated_git_dir(repo: Path, base_env: dict, keep_on_error: bool = True):
    """Yield (git flags, env, temp git dir) that run git on workspace `repo` through a throwaway GIT_DIR.

    The temp git dir gets an allowlisted copy of the workspace config and an empty hooks dir; objects and the index
    stay shared, and refs are copied back on exit (on an exception too, unless `keep_on_error` is False). `base_env`
    is the caller's environment: the owner's (global config kept for reviewed push credentials) or a member's (no
    owner config at all). Raises GitError for a workspace whose git dir escapes it.
    """
    index_dir, metadata, work_tree = _resolve_workspace_git(repo)
    # Copy-in and copy-out are one transaction: concurrent readers also copy back
    # stale refs and can collide with open files on Windows. Keep other repos independent.
    with _git_state_lock(metadata), tempfile.TemporaryDirectory(prefix="harness-git-", dir=_temp_dir()) as raw_tmp:
        base = _resolve(Path(raw_tmp))  # canonical: the state copy refuses links anywhere on its paths
        tmp, hooks = base / "git", base / "hooks"
        hooks.mkdir(parents=True)
        tmp.mkdir()
        _copy_git_state(metadata, tmp)
        _write_isolated_config(tmp / "config", hooks)
        env = dict(base_env)
        _inject_config(env, _allowlisted_config(metadata / "config"))
        env["GIT_INDEX_FILE"] = str(index_dir / "index")
        env["GIT_OBJECT_DIRECTORY"] = str(metadata / "objects")
        (metadata / "objects").mkdir(parents=True, exist_ok=True)
        try:
            yield _isolated_flags(work_tree, tmp, hooks), env, tmp
        except BaseException:
            if keep_on_error:
                _copy_git_state(tmp, metadata)
            raise
        _copy_git_state(tmp, metadata)


def is_url(repo: str) -> bool:
    return "://" in repo or repo.startswith("git@")


def branch_name(sid: str) -> str:
    return f"agent/{sid}"


def source_path(project: Project) -> Path | None:
    return None if is_url(project.repo) else Path(project.repo)


def _is_bare(repo: Path) -> bool:
    return git(repo, "rev-parse", "--is-bare-repository", trusted=True).out.strip() == "true"


def prepare(project: Project, workspace: Path, sid: str, shared: bool = False, base_only: bool = False) -> dict:
    """Clone the project into the (empty) workspace and create the session branch. `shared` (local sources only)
    borrows the source's objects through git alternates instead of copying them. `base_only` clones just the base
    branch through the git transport, so the workspace holds neither other branches nor their objects."""
    src = project.repo
    if not is_url(src) and not Path(src).is_dir():
        raise GitError(f"project {project.name}: repository {src} doesn't exist", 400)
    workspace.mkdir(parents=True, exist_ok=True)
    if any(workspace.iterdir()):
        raise GitError(f"workspace {workspace} is not empty")
    if base_only:
        args = ["clone", "--no-local", "--single-branch", "--config", "core.autocrlf=false"]
    else:
        args = ["clone", "--shared" if shared else "--no-hardlinks", "--config", "core.autocrlf=false"]  # LF checkout
    if project.base_branch:
        args += ["--branch", project.base_branch]
    result = git(None, *args, "--", src, str(workspace), check=False)
    if result.code != 0:
        for child in workspace.iterdir():  # so a retry starts from an empty workspace
            shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
        if project.base_branch and "Remote branch" in result.text and "not found" in result.text:
            raise GitError(f"base branch {project.base_branch} was deleted or is inaccessible")
        raise GitError(f"could not clone {src}: {result.text[-1500:]}")
    base_branch = git(workspace, "rev-parse", "--abbrev-ref", "HEAD").out.strip()
    base_commit = git(workspace, "rev-parse", "HEAD").out.strip()
    branch = branch_name(sid)
    git(workspace, "checkout", "-q", "-b", branch)
    cfg = workspace / ".git" / "config"  # real file: isolation would otherwise throw the identity away
    for key, value in (("user.name", AGENT_NAME), ("user.email", AGENT_EMAIL)):
        git(None, "config", "--file", str(cfg), key, value)
    return {"branch": branch, "base_branch": base_branch, "base_commit": base_commit}


def refresh_origin(project: Project, workspace: Path, base_branch: str = "") -> str:
    """Fetch the source so `origin/<base>` is current when the agent starts a run. Returns an error or ''.

    Fetches from `project.repo`, not the workspace's `origin` URL, which the agent can rewrite. With `base_branch`,
    only that branch is fetched.
    """
    refspec = (f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}" if base_branch
               else "+refs/heads/*:refs/remotes/origin/*")
    result = git(workspace, "fetch", "--quiet", "--prune", "--", project.repo, refspec, timeout=300, check=False)
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


def publish_local(project: Project, workspace: Path, branch: str, source_ref: str | None = None) -> bool:
    """Copy the session branch into a local source repository. No-op for URL projects.

    Push from the isolated workspace into the trusted source so receive-pack (and its hooks) run in the source,
    not upload-pack inside the agent-writable clone.
    """
    src = source_path(project)
    if src is None or not (workspace / ".git").exists():
        return False
    git(workspace, "push", "--quiet", "--no-verify", str(src), f"+{source_ref or branch}:refs/heads/{branch}")
    return True


def push(project: Project, workspace: Path, branch: str, source_ref: str | None = None) -> str:
    """Push the session branch to the URL source, with the daemon's (the user's) git credentials.

    Pushes to `project.repo`, not the workspace's `origin` URL, which the agent can rewrite.
    """
    if not is_url(project.repo):
        raise GitError("push is for URL projects; local projects already have the branch", 400)
    commit = git(workspace, "rev-parse", "--verify", f"{source_ref or branch}^{{commit}}").out.strip()
    result = git(workspace, "push", "--quiet", "--no-verify", "--", project.repo,
                 f"{commit}:refs/heads/{branch}", timeout=300, check=False)
    if result.code != 0:
        raise GitError(f"push failed: {result.text[-1500:]}")
    # A push by URL updates no tracking ref; record it so cleanup and the secret scan see what was published.
    git(workspace, "update-ref", f"refs/remotes/origin/{branch}", commit)
    return f"pushed {branch} to {project.repo}"


def _delete_branch(src: Path, branch: str) -> None:
    if git(src, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False, trusted=True).code == 0:
        git(src, "branch", "-q", "-D", branch, trusted=True)


def merge(project: Project, workspace: Path, sid: str, branch: str, base_branch: str, title: str,
          source_ref: str | None = None) -> dict:
    """Squash-merge the session branch into the base branch of a local source repository.

    A checked-out source is merged in place, which needs it to be on the base branch with nothing staged
    (unrelated unstaged edits are fine; git refuses if the merge would touch them). A bare source is merged in a
    temporary worktree.
    """
    src = source_path(project)
    if src is None:
        raise GitError("merging is for local projects; push the branch and open a pull request instead", 400)
    # A runner has already snapshotted and checked its scanned head; publish that immutable commit.
    if source_ref is None:
        snapshot(workspace, f"Work in progress from session {sid}")
    publish_local(project, workspace, branch, source_ref)
    subjects = git(src, "log", "--reverse", "--format=- %s", f"{base_branch}..{branch}",
                   check=False, trusted=True).out.strip()
    if not subjects:
        _delete_branch(src, branch)
        return {"merged": False, "message": "nothing to merge: the branch has no commits beyond the base"}
    message = f"{title}\n\nSquash-merged from {branch} (agent-harness session {sid}).\n\n{subjects}\n"

    if _is_bare(src):
        tmp = Path(tempfile.mkdtemp(prefix="harness-merge-", dir=_temp_dir()))
        worktree = tmp / "wt"
        try:
            git(src, "worktree", "add", "-q", str(worktree), base_branch, trusted=True)
            _squash_commit(worktree, branch, message)
            commit = head(worktree, trusted=True)
        finally:
            git(src, "worktree", "remove", "--force", str(worktree), check=False, trusted=True)
            git(src, "worktree", "prune", check=False, trusted=True)
            shutil.rmtree(tmp, ignore_errors=True)
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
