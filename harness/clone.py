"""Credential-free public HTTPS clones for household member projects.

The public-host allowlist matches the existing git_clone policy (github.com, gitlab.com, codeberg.org).
Member projects reject local paths, file:/SSH URLs, embedded credentials, non-default ports,
query/fragment credentials, unapproved hosts, and `local:` aliases. Clone runs with prompts and host
credential helpers disabled and without the owner's Git configuration.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .principal import PUBLIC_CLONE_HOSTS
from .projects import GitError, GitResult, git, isolated_git_dir
from .storage import ContainmentError, require_contained

_LOCAL_SCHEME = re.compile(r"^(file|git|ssh|git\+ssh|rsync)$", re.I)
_DRIVE = re.compile(r"^[a-zA-Z]:[\\/]")
# No credential helper and no askpass program: a member clone never finds or prompts for credentials.
_NO_HELPERS = ["-c", "credential.helper=", "-c", "core.askPass="]


class CloneRefused(ValueError):
    """The URL is not a credential-free public HTTPS repository on the allowlist."""


class QuotaExceeded(Exception):
    """A clone wrote past the account disk quota; the destination was removed."""

    def __init__(self, limit: int):
        self.limit = limit
        super().__init__("this clone exceeded the account disk quota")


def _refuse_local(text: str) -> None:
    """Refuse clone aliases and local paths before the text is parsed as a URL."""
    if text.startswith("local:"):
        raise CloneRefused("local: clone aliases are not allowed for household members")
    if _DRIVE.match(text) or text.startswith("\\\\") or text.startswith("//"):
        raise CloneRefused("local paths are not allowed for household members")
    # bare paths and relative paths; scp-style git@ is caught after parsing
    if (os.path.isabs(text) or text.startswith(".") or "/" in text and "@" not in text) and "://" not in text:
        raise CloneRefused("local paths and SSH URLs are not allowed for household members")


def _allowed_host(parsed) -> str:
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host or host not in PUBLIC_CLONE_HOSTS:
        raise CloneRefused(f"host {host or '(missing)'} is not on the public-host allowlist")
    return host


def _repository_path(parsed) -> str:
    path = parsed.path or ""
    if not path or path == "/" or ".." in path.split("/") or ":" in path or "@" in path:
        raise CloneRefused("repository path is invalid")
    return path


def public_https_url(url: str) -> str:
    """Return a canonical https URL or raise CloneRefused."""
    text = (url or "").strip()
    if not text:
        raise CloneRefused("repository url is required")
    if "\x00" in text or len(text) > 2048:
        raise CloneRefused("repository url is invalid")
    _refuse_local(text)
    parsed = urlsplit(text)
    scheme = (parsed.scheme or "").lower()
    if _LOCAL_SCHEME.match(scheme) or text.startswith("git@"):
        raise CloneRefused("file: and SSH URLs are not allowed for household members")
    if scheme != "https":
        raise CloneRefused("only https URLs on the public-host allowlist are allowed")
    if parsed.username or parsed.password:
        raise CloneRefused("embedded credentials are not allowed")
    host = _allowed_host(parsed)
    if parsed.port not in (None, 443):
        raise CloneRefused("non-default ports are not allowed")
    if parsed.query or parsed.fragment:
        # query/fragment can carry tokens; refuse rather than strip-and-continue
        raise CloneRefused("query strings and fragments are not allowed on clone URLs")
    return urlunsplit(("https", host, _repository_path(parsed), "", ""))


def isolated_clone_env() -> dict[str, str]:
    """Environment for member clones: no prompts, no host helpers, no owner gitconfig."""
    # GCM_* and host GitHub tokens are dropped too, so a member GitHub namespace (issue #63) or the owner's
    # token can never become ambient for a credential-free public clone.
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("GIT_", "GCM_")) and k.upper() not in
           ("GIT_ASKPASS", "SSH_ASKPASS", "GH_TOKEN", "GITHUB_TOKEN")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GCM_INTERACTIVE"] = "Never"
    # Empty global config so the owner's ~/.gitconfig (credential helpers, insteadOf) is not read.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env["GIT_CONFIG_COUNT"] = "3"
    env["GIT_CONFIG_KEY_0"] = "credential.helper"
    env["GIT_CONFIG_VALUE_0"] = ""
    env["GIT_CONFIG_KEY_1"] = "core.askPass"
    env["GIT_CONFIG_VALUE_1"] = ""
    env["GIT_CONFIG_KEY_2"] = "http.extraHeader"
    env["GIT_CONFIG_VALUE_2"] = ""
    return env


def clone_public(url: str, dest: Path, root: Path, max_bytes: int | None = None) -> GitResult:
    """Clone `url` into `dest`, which must be contained in `root`. Destination must not exist.

    `max_bytes`, when set, is the most the destination tree may occupy. The clone is killed and
    deleted if it grows past that, so a public repo cannot fill the owner's disk.
    """
    canonical = public_https_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        require_contained(dest.parent, root)
    except ContainmentError as e:
        raise CloneRefused(str(e)) from e
    if dest.exists():
        raise CloneRefused(f"clone destination already exists: {dest}")
    cmd = [
        "git", "-c", "core.quotepath=off", *_NO_HELPERS,
        "-c", "http.extraHeader=", "clone", "--config", "core.autocrlf=false", "--", canonical, str(dest),
    ]
    result = _run_clone(cmd, dest, max_bytes=max_bytes)
    try:
        require_contained(dest, root)
    except ContainmentError:
        import shutil
        shutil.rmtree(dest, ignore_errors=True)
        raise CloneRefused("clone resolved outside the account root")
    return result


def _run_clone(cmd: list[str], dest: Path, *, timeout: int = 600,
               max_bytes: int | None = None, remove_on_fail: bool = True,
               env: dict[str, str] | None = None, on_start=None) -> GitResult:
    """Run an isolated git clone, optionally killing it if `dest` grows past `max_bytes`.

    Fetch into an existing workspace passes `remove_on_fail=False` so a quota kill does not
    delete the session tree. The member GitHub broker (issue #63) passes its own minimal `env` and an
    `on_start(proc)` hook that registers the process so disconnect/disable can stop it.
    """
    from .fileops import dir_size

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", env=env if env is not None else isolated_clone_env(),
        stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if on_start is not None:
        on_start(proc)
    over = threading.Event()
    watcher = threading.Thread(target=_watch_size, args=(proc, dest, max_bytes, over), daemon=True)
    if max_bytes is not None:
        watcher.start()
    stdout, stderr, timed_out = _wait_for_clone(proc, over, timeout)
    if max_bytes is not None:
        watcher.join(timeout=2)
        _stop_clone(proc)
    if timed_out:
        _discard(dest, remove_on_fail)
        raise GitError("clone timed out") from None
    result = GitResult(proc.returncode or 0, stdout or "", stderr or "")
    size = dir_size(dest) if dest.exists() else 0
    if over.is_set() or (max_bytes is not None and size > max_bytes):
        _discard(dest, remove_on_fail)
        raise QuotaExceeded(max_bytes or 0)
    if proc.returncode != 0:
        _discard(dest, remove_on_fail)
        raise GitError(f"could not clone: {result.text[-1500:]}")
    return result


def _stop_clone(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    try:
        proc.kill()
    except OSError:
        pass


_DRAIN_GRACE_SECONDS = 5  # after stopping the process, give its pipes this long to see EOF before giving up (#243)


def _wait_for_clone(proc: subprocess.Popen, over: threading.Event, timeout: int) -> tuple[str, str, bool]:
    """Wait for `proc` to finish, polling in short slices so a quota kill (`over`) ends the wait right away
    instead of only after the full `timeout`.

    Never wait on the pipes without a bound. A killed process is not guaranteed to close them: git can spawn a
    helper (for example `git-upload-pack`, seen for a local clone) that inherits the pipe handle, and stopping
    only the parent leaves that handle open, so `communicate()` never sees EOF and blocks forever (#243) --
    this hit a real CI run, not just this function's own timeout path, because the original code's post-kill
    `communicate()` had no timeout at all.

    Returns (stdout, stderr, timed_out). `timed_out` reflects only a real timeout (the full budget elapsed
    with `over` never set); a quota kill returns `timed_out=False` so the caller's existing `over.is_set()`
    check still reports it as a quota, not a timeout, even when the drain below comes back empty.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _finish_after_stop(proc, timed_out=True)
        if over.is_set():
            return _finish_after_stop(proc, timed_out=False)
        try:
            stdout, stderr = proc.communicate(timeout=min(remaining, 0.2))
            return stdout, stderr, False
        except subprocess.TimeoutExpired:
            continue


def _finish_after_stop(proc: subprocess.Popen, *, timed_out: bool) -> tuple[str, str, bool]:
    _stop_clone(proc)
    try:
        stdout, stderr = proc.communicate(timeout=_DRAIN_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        stdout, stderr = "", ""
    return stdout, stderr, timed_out


def _watch_size(proc: subprocess.Popen, dest: Path, max_bytes: int, over: threading.Event) -> None:
    """Watcher thread: stop the clone once `dest` grows past `max_bytes`."""
    from .fileops import dir_size

    while proc.poll() is None:
        if dest.exists() and dir_size(dest) > max_bytes:
            over.set()
            _stop_clone(proc)
            return
        time.sleep(0.05)


def _discard(dest: Path, remove: bool) -> None:
    if remove:
        _remove_tree(dest)


def _make_writable(p: str) -> None:
    try:
        os.chmod(p, stat.S_IWRITE)
    except OSError:
        pass


def _retry_writable(func, p, _exc) -> None:
    """shutil.rmtree onerror: clear the read-only bit and retry once."""
    _make_writable(p)
    try:
        func(p)
    except OSError:
        pass


def _make_tree_writable(path: Path) -> None:
    try:
        for root, dirs, files in os.walk(path):
            for name in files + dirs:
                _make_writable(os.path.join(root, name))
            _make_writable(root)
    except OSError:
        pass


def _remove_tree(path: Path) -> None:
    for _ in range(15):
        if not path.exists():
            return
        _make_tree_writable(path)
        shutil.rmtree(path, onerror=_retry_writable)
        if not path.exists():
            return
        time.sleep(0.1)
    if os.name == "nt" and path.exists():
        os.system(f'rmdir /s /q "{path}"')


def isolated_prepare(workspace: Path, source: Path | str, sid: str, root: Path,
                     max_bytes: int | None = None) -> dict:
    """Session checkout from a member-managed source, without owner git helpers."""
    from .projects import AGENT_EMAIL, AGENT_NAME, branch_name

    require_contained(workspace, root, allow_missing=True)
    workspace.mkdir(parents=True, exist_ok=True)
    src = str(source)
    cmd = [
        "git", "-c", "core.quotepath=off", *_NO_HELPERS,
        "clone", "--no-hardlinks", "--config", "core.autocrlf=false", "--", src, str(workspace),
    ]
    _run_clone(cmd, workspace, max_bytes=max_bytes)
    require_contained(workspace, root)
    def git_c(*args: str) -> str:
        return _isolated_git(workspace, *args)
    base_branch = git_c("rev-parse", "--abbrev-ref", "HEAD").strip()
    base_commit = git_c("rev-parse", "HEAD").strip()
    branch = branch_name(sid)
    git_c("checkout", "-q", "-b", branch)
    git_c("config", "user.name", AGENT_NAME)
    git_c("config", "user.email", AGENT_EMAIL)
    return {"branch": branch, "base_branch": base_branch, "base_commit": base_commit}


def _isolated_git(workspace: Path, *args: str, timeout: int = 60) -> str:
    r = subprocess.run(
        ["git", "-c", f"safe.directory={workspace.as_posix()}", *_NO_HELPERS, "-C", str(workspace), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        env=isolated_clone_env(), stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if r.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])} failed: {(r.stdout + r.stderr).strip()[-1500:]}")
    return r.stdout


def _member_origin_allowed(origin: str, root: Path) -> bool:
    text = (origin or "").strip()
    if not text:
        return False
    try:
        public_https_url(text)
        return True
    except CloneRefused:
        pass
    try:
        require_contained(Path(text), root)
        return True
    except (OSError, ValueError):  # ContainmentError is a ValueError
        return False


def isolated_refresh_origin(workspace: Path, source: Path | str, root: Path, max_bytes: int | None = None) -> str:
    """Fetch the project's source into a member workspace without owner git helpers or credentials.

    Returns an error or ''. Like the owner's `projects.refresh_origin`, the fetch names `source` from the daemon's
    record, never the workspace's `origin` URL or remote config, and runs through a throwaway GIT_DIR (#529): the
    workspace is agent-writable, so its hooks, fsmonitor, uploadpack and other config must not run on the host.
    """
    require_contained(workspace, root)
    src = str(source)
    if not _member_origin_allowed(src, root):
        return "origin is not a public or account-local repository"
    try:
        with isolated_git_dir(workspace, isolated_clone_env()) as (flags, env):
            cmd = ["git", *_NO_HELPERS, *flags, "fetch", "--quiet", "--prune", "--", src,
                   "+refs/heads/*:refs/remotes/origin/*"]
            _run_clone(cmd, workspace, timeout=300, max_bytes=max_bytes, remove_on_fail=False, env=env)
    except QuotaExceeded as e:
        return str(e)
    except GitError as e:
        return str(e)[-500:]
    return ""


# tempfile imported for future scratch configs; keep the name used in tests via isolated_clone_env.
_ = tempfile
