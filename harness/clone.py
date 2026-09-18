"""Credential-free public HTTPS clones for household member projects.

The public-host allowlist matches the existing git_clone policy (github.com, gitlab.com, codeberg.org).
Member projects reject local paths, file:/SSH URLs, embedded credentials, non-default ports,
query/fragment credentials, unapproved hosts, and `local:` aliases. Clone runs with prompts and host
credential helpers disabled and without the owner's Git configuration.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .principal import PUBLIC_CLONE_HOSTS
from .projects import GitError, GitResult, git
from .storage import ContainmentError, require_contained

_LOCAL_SCHEME = re.compile(r"^(file|git|ssh|git\+ssh|rsync)$", re.I)
_DRIVE = re.compile(r"^[a-zA-Z]:[\\/]")


class CloneRefused(ValueError):
    """The URL is not a credential-free public HTTPS repository on the allowlist."""


class QuotaExceeded(Exception):
    """A clone wrote past the account disk quota; the destination was removed."""

    def __init__(self, limit: int):
        self.limit = limit
        super().__init__("this clone exceeded the account disk quota")


def public_https_url(url: str) -> str:
    """Return a canonical https URL or raise CloneRefused."""
    text = (url or "").strip()
    if not text:
        raise CloneRefused("repository url is required")
    if "\x00" in text or len(text) > 2048:
        raise CloneRefused("repository url is invalid")
    if text.startswith("local:"):
        raise CloneRefused("local: clone aliases are not allowed for household members")
    if _DRIVE.match(text) or text.startswith("\\\\") or text.startswith("//"):
        raise CloneRefused("local paths are not allowed for household members")
    if os.path.isabs(text) or text.startswith(".") or "/" in text and "://" not in text and "@" not in text:
        # bare paths and relative paths; scp-style git@ still caught below
        if "://" not in text:
            raise CloneRefused("local paths and SSH URLs are not allowed for household members")
    parsed = urlsplit(text)
    scheme = (parsed.scheme or "").lower()
    if _LOCAL_SCHEME.match(scheme) or text.startswith("git@"):
        raise CloneRefused("file: and SSH URLs are not allowed for household members")
    if scheme != "https":
        raise CloneRefused("only https URLs on the public-host allowlist are allowed")
    if parsed.username or parsed.password:
        raise CloneRefused("embedded credentials are not allowed")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host or host not in PUBLIC_CLONE_HOSTS:
        raise CloneRefused(f"host {host or '(missing)'} is not on the public-host allowlist")
    if parsed.port not in (None, 443):
        raise CloneRefused("non-default ports are not allowed")
    if parsed.query or parsed.fragment:
        # query/fragment can carry tokens; refuse rather than strip-and-continue
        raise CloneRefused("query strings and fragments are not allowed on clone URLs")
    path = parsed.path or ""
    if not path or path == "/" or ".." in path.split("/"):
        raise CloneRefused("repository path is invalid")
    if ":" in path or "@" in path:
        raise CloneRefused("repository path is invalid")
    return urlunsplit(("https", host, path, "", ""))


def isolated_clone_env() -> dict[str, str]:
    """Environment for member clones: no prompts, no host helpers, no owner gitconfig."""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("GIT_") and k.upper() not in
           ("GCM_INTERACTIVE", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")}
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
        "git", "-c", "core.quotepath=off", "-c", "credential.helper=", "-c", "core.askPass=",
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
               max_bytes: int | None = None, remove_on_fail: bool = True) -> GitResult:
    """Run an isolated git clone, optionally killing it if `dest` grows past `max_bytes`.

    Fetch into an existing workspace passes `remove_on_fail=False` so a quota kill does not
    delete the session tree.
    """
    import subprocess
    import threading
    import time

    from .fileops import dir_size

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", env=isolated_clone_env(), stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    over = False

    def stop() -> None:
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

    def watch() -> None:
        nonlocal over
        while proc.poll() is None:
            if max_bytes is not None and dest.exists() and dir_size(dest) > max_bytes:
                over = True
                stop()
                return
            time.sleep(0.05)

    watcher = threading.Thread(target=watch, daemon=True)
    if max_bytes is not None:
        watcher.start()
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop()
        stdout, stderr = proc.communicate()
        if remove_on_fail:
            _remove_tree(dest)
        raise GitError("clone timed out") from None
    if max_bytes is not None:
        watcher.join(timeout=2)
        stop()
    result = GitResult(proc.returncode or 0, stdout or "", stderr or "")
    size = dir_size(dest) if dest.exists() else 0
    if over or (max_bytes is not None and size > max_bytes):
        if remove_on_fail:
            _remove_tree(dest)
        raise QuotaExceeded(max_bytes or 0)
    if proc.returncode != 0:
        if remove_on_fail:
            _remove_tree(dest)
        raise GitError(f"could not clone: {result.text[-1500:]}")
    return result


def _remove_tree(path: Path) -> None:
    import os
    import shutil
    import stat
    import time

    def writable(p: str) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
        except OSError:
            pass

    def onerror(func, p, _exc) -> None:
        writable(p)
        try:
            func(p)
        except OSError:
            pass

    for _ in range(15):
        if not path.exists():
            return
        try:
            for root, dirs, files in os.walk(path):
                for name in files + dirs:
                    writable(os.path.join(root, name))
                writable(root)
        except OSError:
            pass
        shutil.rmtree(path, onerror=onerror)
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
        "git", "-c", "core.quotepath=off", "-c", "credential.helper=", "-c", "core.askPass=",
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
    import subprocess
    r = subprocess.run(
        ["git", "-c", f"safe.directory={workspace.as_posix()}", "-c", "credential.helper=",
         "-c", "core.askPass=", "-C", str(workspace), *args],
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
    except (ContainmentError, OSError, ValueError):
        return False


def isolated_refresh_origin(workspace: Path, root: Path, max_bytes: int | None = None) -> str:
    """Fetch origin without owner git helpers or credentials. Returns an error or ''."""
    require_contained(workspace, root)
    try:
        origin = _isolated_git(workspace, "remote", "get-url", "origin").strip()
    except GitError as e:
        return str(e)[-500:]
    if not _member_origin_allowed(origin, root):
        return "origin is not a public or account-local repository"
    cmd = [
        "git", "-c", f"safe.directory={workspace.as_posix()}", "-c", "credential.helper=",
        "-c", "core.askPass=", "-C", str(workspace), "fetch", "--quiet", "--prune", "origin",
    ]
    try:
        _run_clone(cmd, workspace, timeout=300, max_bytes=max_bytes, remove_on_fail=False)
    except QuotaExceeded as e:
        return str(e)
    except GitError as e:
        return str(e)[-500:]
    return ""


# tempfile imported for future scratch configs; keep the name used in tests via isolated_clone_env.
_ = tempfile
