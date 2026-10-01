"""Isolated household-member GitHub credentials through Git Credential Manager (issue #63).

Each enabled member can sign in to their own GitHub account with GCM's GitHub OAuth device flow. GCM owns the
token and keeps it in an allowlisted OS secure store under `GCM_NAMESPACE=agent-harness/v1/<user_id>`. The
daemon and Agent Harness Web never receive it:

- connect runs `gcm get` with stdout piped straight into `gcm store` (process to process, never read here),
  and GCM's custom GitHub UI helper (`gcm_ui_helper.py`) relays only the verification URL and user code;
- the noninteractive probe and every other GCM call send stdout to a discard sink;
- clone/fetch/push run host-side through a constrained Git broker: canonical `https://github.com/o/r` URLs
  only, a minimal environment, no inherited config or helpers, the pinned GCM helper at the highest Git
  config precedence, no prompts, hooks, filters, submodules, LFS, redirects, or URL rewrites.

Verified GCM behavior (2.9.0, the minimum tested version):
- `GCM_GITHUB_HELPER=<exe>` is started as `<exe> device --code <code> --url <url>` when GUI prompts are on and
  the session is a desktop session. GCM polls GitHub itself and kills the helper when the token arrives; a
  helper that exits first cancels the flow.
- `get` with `GCM_INTERACTIVE=never` and no stored credential exits non-zero without prompting (the probe).
- `get` echoes protocol, host, username, and password, which is exactly what `store` reads.
- `erase` with only protocol and host removes the namespace's github.com entry.

Trust boundary: this prevents accidental and application-level cross-account credential use. It does not
protect a member's credential from the machine/OS owner, an administrator who can inspect the daemon
account, or a compromised host (the same boundary as #62).
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

NAMESPACE_VERSION = 1
NAMESPACE_PREFIX = f"agent-harness/v{NAMESPACE_VERSION}"
PREFLIGHT_NAMESPACE = f"{NAMESPACE_PREFIX}/preflight"
MIN_GCM_VERSION = (2, 9, 0)
DEVICE_FLOW_SECONDS = 15 * 60  # GitHub's documented device-code lifetime; the harness's hard deadline
VERIFICATION_URLS = ("https://github.com/login/device",)
GITHUB_HOST = "github.com"
PROBE_TIMEOUT = 30
GCM_SCOPES_NOTE = ("Git Credential Manager's GitHub app asks for the broad repo, gist, and workflow scopes: "
                   "access to every repository your GitHub account can reach, not only the ones you add here.")

STORES_BY_PLATFORM = {
    "win32": ("wincredman", "dpapi"),
    "darwin": ("keychain",),
    "linux": ("secretservice", "gpg"),
}
REFUSED_STORES = ("plaintext", "cache", "none", "")

# Public error classes. Messages are generic and never include child-process output.
ERRORS = {
    "not_configured": "GitHub sign-in for household members is not set up on this machine.",
    "feature_disabled": "GitHub sign-in for household members is turned off.",
    "account_disabled": "This household account is disabled.",
    "store_unavailable": "The secure credential store on this machine is unavailable; ask the owner to check it.",
    "unsupported_context": "This machine cannot show GitHub's sign-in prompt in its current service context.",
    "incompatible_gcm": "The installed Git Credential Manager does not support the required sign-in behavior.",
    "not_connected": "Connect your GitHub account first.",
    "reconnect_required": "Your GitHub connection needs to be reconnected.",
    "connect_timeout": "GitHub sign-in timed out; start again when you are ready.",
    "connect_cancelled": "GitHub sign-in was cancelled.",
    "authorization_failed": "GitHub did not authorize the sign-in.",
    "erase_failed": "The stored GitHub credential could not be removed; ask the owner to check the store.",
    "repository_unavailable": "That repository was not found, or your GitHub account cannot access it.",
    "policy_rejected": "That repository address is not allowed.",
    "busy": "Another GitHub operation for this account is still running.",
    "timeout": "The GitHub operation timed out.",
    "cancelled": "The GitHub operation was stopped.",
    "quota": "This clone exceeded the account disk quota.",
    "git_failed": "The Git operation failed.",
}


class GitHubAuthError(Exception):
    """A sanitized failure: `code` is a key of ERRORS; the message never carries child output."""

    def __init__(self, code: str, status: int = 409, message: str = ""):
        self.code = code if code in ERRORS else "git_failed"
        self.status = status
        super().__init__(message or ERRORS[self.code])


# --- URL policy -------------------------------------------------------------------------------------

_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def canonical_github_url(url: str) -> str:
    """Parse and re-render `https://github.com/<owner>/<repo>[.git]`, or raise GitHubAuthError(policy_rejected).

    Refuses userinfo, ports, query/fragment, other schemes and hosts (including `www.` and Unicode
    lookalikes), SSH/SCP, local paths, bundles, remote helpers (`ext::`), and extra path segments.
    """
    text = url if isinstance(url, str) else ""
    text = text.strip()

    def refuse() -> GitHubAuthError:
        return GitHubAuthError("policy_rejected", 400)

    if not text or len(text) > 300 or not text.isascii() or any(c.isspace() or ord(c) < 32 for c in text):
        raise refuse()
    if "::" in text or "\\" in text or "%" in text or "@" in text:
        raise refuse()  # remote helpers, Windows paths, encoded tricks, userinfo/scp
    parts = urlsplit(text)
    if parts.scheme != "https" or parts.netloc.lower() != GITHUB_HOST or parts.query or parts.fragment:
        raise refuse()
    if text.count("?") or text.count("#"):
        raise refuse()
    segments = parts.path.split("/")
    if len(segments) != 3 or segments[0] != "":
        raise refuse()
    owner, repo = segments[1], segments[2]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not _OWNER.match(owner) or not _REPO.match(repo) or repo in (".", "..") or repo.startswith("."):
        raise refuse()
    return f"https://{GITHUB_HOST}/{owner}/{repo}.git"


def display_repo(canonical: str) -> str:
    """`owner/repo` for confirmations shown to the member who owns the project."""
    return canonical.removeprefix(f"https://{GITHUB_HOST}/").removesuffix(".git")


# --- redaction ---------------------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{10,}"),
    re.compile(r"(?i)\b(password|token|oauth_refresh_token|access_token|refresh_token)\s*[=:]\s*\S+"),
    re.compile(r"(?i)\b(?:user[_ ]?code|device[_ ]?code)\b\s*(?:[=:]\s*)?\S+"),
    re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b"),
    re.compile(r"(?i)(authorization:\s*)(?:basic|bearer|token)\s+\S+"),
    re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"),
]


def redact(text: str) -> str:
    """Defensive redaction for anything that might reach logs, events, transcripts, or task records."""
    out = text or ""
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[redacted]", out)
    return out


# --- configuration and preflight ---------------------------------------------------------------------

@dataclass
class Preflight:
    ok: bool
    error: str = ""      # ERRORS key when not ok
    version: str = ""

    def public(self) -> dict:
        return {"ok": self.ok, "error": self.error, "message": ERRORS.get(self.error, "") if self.error else ""}


def platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def configured(cfg) -> bool:
    return bool((cfg.github_member_auth.gcm_path or "").strip())


def broker_root(cfg) -> Path:
    """Daemon-private broker state. Never under a member's storage root or a session workspace."""
    return Path(cfg.data_dir) / "github-broker" / f"v{NAMESPACE_VERSION}"


def namespace_for(user_id: str) -> str:
    _check_user_id(user_id)
    return f"{NAMESPACE_PREFIX}/{user_id}"


def _check_user_id(user_id: str) -> None:
    if not user_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user_id) or user_id in ("owner", "preflight"):
        raise GitHubAuthError("policy_rejected", 400)


def member_dir(cfg, user_id: str) -> Path:
    _check_user_id(user_id)
    return broker_root(cfg) / "members" / user_id


def _member_writable(path: Path, cfg) -> bool:
    """True when a household member (or anyone but the daemon account and administrators) could change it."""
    try:
        if path.is_symlink():
            return True  # not canonical: the link target can be swapped
        resolved = path.resolve(strict=True)
    except OSError:
        return True
    data = Path(cfg.data_dir).resolve()
    if resolved == data or data in resolved.parents:
        return True  # everything under data_dir holds member storage and workspaces
    if os.name == "nt":
        from .storage import is_reparse_point
        return is_reparse_point(path)
    for p in (resolved, *resolved.parents):
        try:
            st = p.stat()
        except OSError:
            return True
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not (p != resolved and st.st_mode & stat.S_ISVTX):
            return True
        if st.st_uid not in (0, os.getuid()):
            return True
    return False


_SHELL_UNSAFE = re.compile(r"[\"'`$\n\r;&|<>*?!]")


def _check_executable(path_text: str, cfg) -> Path | None:
    text = (path_text or "").strip()
    if not text or _SHELL_UNSAFE.search(text):
        return None
    p = Path(text)
    if not p.is_absolute() or not p.is_file() or _member_writable(p, cfg):
        return None
    return p


def gcm_executable(cfg) -> Path:
    exe = _check_executable(cfg.github_member_auth.gcm_path, cfg)
    if exe is None:
        raise GitHubAuthError("not_configured", 503)
    return exe


def git_executable(cfg) -> str:
    text = (cfg.github_member_auth.git_path or "").strip()
    if text:
        exe = _check_executable(text, cfg)
        if exe is None:
            raise GitHubAuthError("not_configured", 503)
        return str(exe)
    found = shutil.which("git")
    if not found:
        raise GitHubAuthError("not_configured", 503)
    return found


def _parse_version(text: str) -> tuple[int, ...] | None:
    tokens = re.findall(r"\d+|\.|[^\d.]+", text or "")  # digit runs, dots, other text: one linear pass
    for i in range(len(tokens) - 4):
        a, d1, b, d2, c = tokens[i:i + 5]
        if a.isdigit() and d1 == "." and b.isdigit() and d2 == "." and c.isdigit():
            return int(a), int(b), int(c)
    return None


def store_settings(cfg, user_id: str | None) -> dict[str, str]:
    """GCM store variables for one namespace. Raises GitHubAuthError when the store is not allowlisted."""
    conf = cfg.github_member_auth
    store = (conf.credential_store or "").strip().lower()
    if store in REFUSED_STORES or store not in STORES_BY_PLATFORM[platform_key()]:
        raise GitHubAuthError("store_unavailable", 503)
    env = {"GCM_CREDENTIAL_STORE": store}
    if store == "dpapi":
        owner = user_id or "preflight"
        env["GCM_DPAPI_STORE_PATH"] = str(broker_root(cfg) / "dpapi" / owner)
    elif store == "gpg":
        pass_store = (conf.gpg_pass_store_path or "").strip()
        if not pass_store or not (Path(pass_store) / ".gpg-id").is_file():
            raise GitHubAuthError("store_unavailable", 503)
        env["GCM_GPG_PATH"] = shutil.which("gpg") or ""
        if not env["GCM_GPG_PATH"]:
            raise GitHubAuthError("store_unavailable", 503)
        env["PASSWORD_STORE_DIR"] = pass_store
        if conf.gnupg_home:
            env["GNUPGHOME"] = conf.gnupg_home
    elif store == "secretservice":
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            raise GitHubAuthError("store_unavailable", 503)
    return env


# --- process environment -----------------------------------------------------------------------------

# Only these are inherited. Everything else (GH_TOKEN, GITHUB_TOKEN, GIT_*, GCM_*, SSH_*, *_PROXY, tracing,
# HOME/XDG config, PYTHON*) is dropped.
_ENV_ALLOW = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "PROGRAMDATA", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "OS", "USERNAME", "USERDOMAIN", "LOGNAME", "USER", "LANG", "LC_ALL", "LC_CTYPE",
    "TZ", "TEMP", "TMP", "TMPDIR", "XDG_RUNTIME_DIR",
)
_STORE_ENV = {"secretservice": ("DBUS_SESSION_BUS_ADDRESS",)}
_DESKTOP_ENV = ("DISPLAY", "WAYLAND_DISPLAY")  # GCM only uses a UI helper in a desktop session


def _member_home(cfg, user_id: str | None) -> Path:
    base = member_dir(cfg, user_id) if user_id else broker_root(cfg) / "preflight"
    return base / "home"


def _prepare_member_dirs(cfg, user_id: str | None) -> dict[str, Path]:
    home = _member_home(cfg, user_id)
    base = home.parent
    hooks = base / "empty-hooks"
    template = base / "empty-template"
    for d in (home, hooks, template, home / "AppData" / "Roaming", home / "AppData" / "Local"):
        d.mkdir(parents=True, exist_ok=True)
    for d in (hooks, template):  # fixed and empty: anything that appeared is removed
        for child in d.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    gitconfig = base / "gitconfig"
    gitconfig.write_text("# agent-harness member broker: intentionally empty\n", encoding="utf-8")
    if os.name != "nt":
        for d in (base, home):
            os.chmod(d, 0o700)
    return {"home": home, "hooks": hooks, "template": template, "gitconfig": gitconfig}


def broker_env(cfg, user_id: str | None, *, connect: bool = False, namespace: str | None = None) -> dict[str, str]:
    """Minimal environment for GCM and Git on behalf of one member namespace (or the preflight namespace)."""
    dirs = _prepare_member_dirs(cfg, user_id)
    store = store_settings(cfg, user_id)
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_ALLOW}
    for key in _STORE_ENV.get(store["GCM_CREDENTIAL_STORE"], ()):
        if key in os.environ:
            env[key] = os.environ[key]
    if connect:
        for key in _DESKTOP_ENV:
            if key in os.environ:
                env[key] = os.environ[key]
    home = str(dirs["home"])
    env.update({
        "HOME": home, "USERPROFILE": home,
        "APPDATA": str(dirs["home"] / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(dirs["home"] / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(dirs["home"] / ".config"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": str(dirs["gitconfig"]),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_ALLOW_PROTOCOL": "https",
        "GCM_NAMESPACE": namespace or namespace_for(user_id),
        "GCM_PROVIDER": "github",
        "GCM_GITHUB_AUTHMODES": "device",
        "GCM_INTERACTIVE": "always" if connect else "never",
        "GCM_GUI_PROMPT": "true" if connect else "false",
        **store,
    })
    if store["GCM_CREDENTIAL_STORE"] == "dpapi":
        Path(store["GCM_DPAPI_STORE_PATH"]).mkdir(parents=True, exist_ok=True)
    return env


def git_config_args(cfg, user_id: str, *, hooks_dir: Path | None = None) -> list[str]:
    """`-c` overrides (highest precedence) that pin the credential helper and forbid every escape hatch."""
    gcm = gcm_executable(cfg).as_posix()
    hooks = (hooks_dir or (member_dir(cfg, user_id) / "empty-hooks")).as_posix()
    pairs = [
        ("credential.helper", ""),                 # clear any inherited list
        # then the pinned GCM. `!` runs it as a shell command: git only treats an unquoted value as an absolute
        # path, and the quotes keep a path with spaces (Program Files) intact.
        ("credential.helper", f'!"{gcm}"'),
        ("credential.useHttpPath", "false"),
        ("credential.interactive", "never"),
        ("credential.namespace", namespace_for(user_id)),
        ("credential.guiPrompt", "false"),
        ("credential.gitHubAuthModes", "device"),
        ("core.askPass", ""),
        ("core.sshCommand", ""),
        ("core.gitProxy", ""),
        ("core.hooksPath", hooks),
        ("core.fsmonitor", "false"),
        ("core.autocrlf", "false"),
        ("protocol.allow", "never"),
        ("protocol.https.allow", "always"),
        ("http.followRedirects", "false"),
        ("http.extraHeader", ""),
        ("http.proxy", ""),
        ("http.sslVerify", "true"),
        ("http.emptyAuth", "false"),
        ("submodule.recurse", "false"),
        ("fetch.recurseSubmodules", "false"),
        ("push.recurseSubmodules", "no"),
        ("filter.lfs.required", "false"),
        ("filter.lfs.smudge", ""),
        ("filter.lfs.process", ""),
        ("gc.auto", "0"),
        ("maintenance.auto", "false"),
        ("advice.detachedHead", "false"),
    ]
    out: list[str] = []
    for key, value in pairs:
        out += ["-c", f"{key}={value}"]
    return out


# Repository config keys a credentialed operation accepts in the daemon-owned managed repository. Anything
# else (url.*.insteadOf, http.*, credential.*, remote.*.uploadpack/receivepack/proxy/vcs, filter.*, include*,
# core.hooksPath/fsmonitor/sshCommand/gitProxy, submodule.*, lfs.*) refuses the operation.
_REPO_CONFIG_ALLOW = re.compile(
    r"^(core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode|symlinks|"
    r"autocrlf|hookspath)|remote\.origin\.(url|fetch)|branch\.[^.]+(\.[^.]+)*\.(remote|merge)|"
    r"user\.(name|email)|extensions\.objectformat|gc\.auto|maintenance\.auto)$", re.I)


def check_repo_config(git: str, repo: Path, env: dict[str, str], expected_origin: str, hooks_dir: Path) -> None:
    """Refuse a managed repository whose local config could rewrite URLs, inject helpers, or run programs."""
    r = subprocess.run(
        [git, "-c", "core.fsmonitor=false", "-C", str(repo), "config", "--local", "--null", "--list"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=30,
        stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if r.returncode != 0:
        raise GitHubAuthError("policy_rejected", 409)
    for entry in filter(None, r.stdout.split("\0")):
        key, _, value = entry.partition("\n")
        if not _REPO_CONFIG_ALLOW.match(key):
            raise GitHubAuthError("policy_rejected", 409)
        lk = key.lower()
        if lk == "remote.origin.url" and value != expected_origin:
            raise GitHubAuthError("policy_rejected", 409)
        if lk == "core.hookspath" and Path(value).resolve() != hooks_dir.resolve():
            raise GitHubAuthError("policy_rejected", 409)
    if (repo / ".git" / "modules").exists():
        raise GitHubAuthError("policy_rejected", 409)  # submodules are never initialized in v1


# --- GCM calls ---------------------------------------------------------------------------------------

QUERY = b"protocol=https\nhost=github.com\n\n"


def _popen_kwargs() -> dict:
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _gcm_run(cfg, user_id: str | None, action: str, data: bytes = QUERY, *, namespace: str | None = None,
             timeout: int = PROBE_TIMEOUT) -> int:
    """Run one GCM credential action. stdout goes to a discard sink; stderr is discarded too."""
    exe = gcm_executable(cfg)
    env = broker_env(cfg, user_id, namespace=namespace)
    try:
        r = subprocess.run([str(exe), action], input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env=env, timeout=timeout, **_popen_kwargs())
    except subprocess.TimeoutExpired:
        return -2
    except OSError:
        return -3
    return r.returncode


def probe(cfg, user_id: str) -> bool | None:
    """True when the member namespace holds a github.com credential, False when not, None when unknown.

    Noninteractive and local: GCM looks the credential up in the store and the output is discarded.
    """
    code = _gcm_run(cfg, user_id, "get")
    if code == 0:
        return True
    if code in (-2, -3):
        return None
    return False


def erase(cfg, user_id: str, attempts: int = 5) -> None:
    """Erase every github.com credential in this member's namespace only; raises erase_failed if one stays."""
    for _ in range(attempts):
        _gcm_run(cfg, user_id, "erase")
        if probe(cfg, user_id) is False:
            return
    raise GitHubAuthError("erase_failed", 500)


def gcm_version(cfg) -> str:
    exe = gcm_executable(cfg)
    try:
        r = subprocess.run([str(exe), "--version"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL,
                           env=broker_env(cfg, None, namespace=PREFLIGHT_NAMESPACE), **_popen_kwargs())
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (r.stdout or "").strip().splitlines()[0][:80] if r.returncode == 0 and r.stdout.strip() else ""


def run_preflight(cfg) -> Preflight:
    """Validate configuration, GCM, and a store round trip in a dedicated preflight namespace.

    Never touches a member namespace or GCM's default `git` namespace. Fails closed: no silent downgrade.
    """
    if not configured(cfg):
        return Preflight(False, "not_configured")
    try:
        gcm_executable(cfg)
        git_executable(cfg)
        store_settings(cfg, None)
    except GitHubAuthError as e:
        return Preflight(False, e.code)
    if platform_key() == "linux" and not any(os.environ.get(k) for k in _DESKTOP_ENV):
        return Preflight(False, "unsupported_context")
    version = gcm_version(cfg)
    parsed = _parse_version(version)
    if not parsed or parsed < MIN_GCM_VERSION:
        return Preflight(False, "incompatible_gcm", version)
    host = f"preflight-{secrets.token_hex(4)}.agent-harness.invalid"
    marker = secrets.token_hex(16)
    query = f"protocol=https\nhost={host}\n\n".encode()
    entry = f"protocol=https\nhost={host}\nusername=agent-harness-preflight\npassword={marker}\n\n".encode()
    try:
        stored = _gcm_run(cfg, None, "store", entry, namespace=PREFLIGHT_NAMESPACE)
        found = _gcm_run(cfg, None, "get", query, namespace=PREFLIGHT_NAMESPACE) if stored == 0 else -1
    finally:
        _gcm_run(cfg, None, "erase", query, namespace=PREFLIGHT_NAMESPACE)
    if stored != 0 or found != 0:
        return Preflight(False, "store_unavailable", version)
    gone = _gcm_run(cfg, None, "get", query, namespace=PREFLIGHT_NAMESPACE)
    if gone == 0:
        return Preflight(False, "incompatible_gcm", version)
    return Preflight(True, "", version)


# --- UI helper wiring --------------------------------------------------------------------------------

HELPER_SOURCE = Path(__file__).with_name("gcm_ui_helper.py")


def helper_command(cfg) -> Path:
    """Write (or rewrite) the pinned launcher GCM starts as its GitHub UI helper; returns its path."""
    bindir = broker_root(cfg) / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    py = Path(sys.executable).resolve()
    script = HELPER_SOURCE.resolve()
    if os.name == "nt":
        path = bindir / "agent-harness-gcm-helper.cmd"
        content = f'@echo off\r\n"{py}" -I "{script}" %*\r\n'
    else:
        path = bindir / "agent-harness-gcm-helper"
        content = f'#!/bin/sh\nexec "{py}" -I "{script}" "$@"\n'
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


# --- error classification ----------------------------------------------------------------------------

_AUTH_FAILURES = re.compile(
    r"(?i)authentication failed|could not read (username|password)|terminal prompts disabled|"
    r"unable to get password|"
    r"invalid username or password|returned error: 401|cannot prompt because|bad credentials")
_NOT_FOUND = re.compile(
    r"(?i)repository not found|returned error: 40[34]|not found|permission to .* denied|access denied|"
    r"write access to repository not granted")
_REDIRECT = re.compile(r"(?i)redirect|returned error: 30[1278]")


def classify_git_failure(text: str) -> str:
    """Map raw Git stderr (kept in memory only) to a public error class."""
    if _AUTH_FAILURES.search(text or ""):
        return "reconnect_required"
    if _NOT_FOUND.search(text or "") or _REDIRECT.search(text or ""):
        return "repository_unavailable"
    return "git_failed"


# --- per-member serialization and in-flight processes -------------------------------------------------

class MemberOps:
    """One credentialed Git/GCM operation per member at a time, killable on disconnect/disable/reset."""

    def __init__(self):
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._procs: dict[str, set] = {}
        self._generation: dict[str, int] = {}

    def lock(self, user_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(user_id, threading.Lock())

    def generation(self, user_id: str) -> int:
        with self._guard:
            return self._generation.get(user_id, 0)

    def register(self, user_id: str, proc) -> None:
        with self._guard:
            self._procs.setdefault(user_id, set()).add(proc)

    def unregister(self, user_id: str, proc) -> None:
        with self._guard:
            self._procs.get(user_id, set()).discard(proc)

    def kill(self, user_id: str) -> int:
        """Stop every in-flight credentialed process for this member; bumps the generation."""
        from .clone import _stop_clone
        with self._guard:
            procs = list(self._procs.get(user_id, ()))
            self._generation[user_id] = self._generation.get(user_id, 0) + 1
        for proc in procs:
            _stop_clone(proc)
        return len(procs)

    def kill_all(self) -> None:
        with self._guard:
            users = list(self._procs)
        for uid in users:
            self.kill(uid)
