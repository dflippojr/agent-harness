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


def clone_public(url: str, dest: Path, root: Path) -> GitResult:
    """Clone `url` into `dest`, which must be contained in `root`. Destination must not exist."""
    canonical = public_https_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        require_contained(dest.parent, root)
    except ContainmentError as e:
        raise CloneRefused(str(e)) from e
    if dest.exists():
        raise CloneRefused(f"clone destination already exists: {dest}")
    # Isolated git: -c overrides plus a clean env. `git()` uses the host environment, so call git here.
    import subprocess
    cmd = [
        "git", "-c", "core.quotepath=off", "-c", "credential.helper=", "-c", "core.askPass=",
        "-c", "http.extraHeader=", "clone", "--config", "core.autocrlf=false", "--", canonical, str(dest),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
        env=isolated_clone_env(), stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    result = GitResult(proc.returncode, proc.stdout, proc.stderr)
    if proc.returncode != 0:
        if dest.exists():
            import shutil
            shutil.rmtree(dest, ignore_errors=True)
        raise GitError(f"could not clone {canonical}: {result.text[-1500:]}")
    try:
        require_contained(dest, root)
    except ContainmentError:
        import shutil
        shutil.rmtree(dest, ignore_errors=True)
        raise CloneRefused("clone resolved outside the account root")
    return result


def isolated_prepare(workspace: Path, source: Path | str, sid: str, root: Path) -> dict:
    """Session checkout from a member-managed source, without owner git helpers."""
    from .projects import branch_name, git as host_git  # noqa: F401 — kept for type parity
    import subprocess
    from .projects import AGENT_EMAIL, AGENT_NAME, GitError as GE

    require_contained(workspace, root, allow_missing=True)
    workspace.mkdir(parents=True, exist_ok=True)
    src = str(source)
    cmd = [
        "git", "-c", "core.quotepath=off", "-c", "credential.helper=", "-c", "core.askPass=",
        "clone", "--no-hardlinks", "--config", "core.autocrlf=false", "--", src, str(workspace),
    ]
    if Path(src).is_dir():
        # Local managed copy: still isolate helpers so origin URLs cannot trigger host credentials.
        pass
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
        env=isolated_clone_env(), stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise GE(f"could not clone {src}: {(proc.stdout + proc.stderr).strip()[-1500:]}")
    require_contained(workspace, root)
    def git_c(*args: str) -> str:
        r = subprocess.run(
            ["git", "-c", f"safe.directory={workspace.as_posix()}", "-C", str(workspace), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            env=isolated_clone_env(), stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode != 0:
            raise GE(f"git {' '.join(args[:3])} failed: {(r.stdout + r.stderr).strip()[-1500:]}")
        return r.stdout
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


def isolated_refresh_origin(workspace: Path, root: Path) -> str:
    """Fetch origin without owner git helpers or credentials. Returns an error or ''."""
    require_contained(workspace, root)
    try:
        origin = _isolated_git(workspace, "remote", "get-url", "origin").strip()
    except GitError as e:
        return str(e)[-500:]
    if not _member_origin_allowed(origin, root):
        return "origin is not a public or account-local repository"
    try:
        _isolated_git(workspace, "fetch", "--quiet", "--prune", "origin", timeout=300)
    except GitError as e:
        return str(e)[-500:]
    return ""


# tempfile imported for future scratch configs; keep the name used in tests via isolated_clone_env.
_ = tempfile
