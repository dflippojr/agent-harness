"""Per-domain state for the hosted CLIs (#371).

A domain is Web (the owner's sessions; members only run the local model) or one App. Each CLI keeps its session
history, settings and caches in a state volume of that domain, so no session can read another domain's history or
leave a file that a session of another domain loads.

- **State:** Web keeps the backend's configured volume (`harness-auth-<backend>`), so the owner's `--resume` keeps
  working. An App gets `harness-cli-<backend>-app-<id>` (the id hashed when it isn't a safe volume name).
- **Login:** Claude Code and Cursor read their credential from a directory of its own
  (`CLAUDE_SECURESTORAGE_CONFIG_DIR`; `$XDG_CONFIG_HOME/cursor`), so one login volume, `harness-login-<backend>`,
  serves every domain. Codex keeps `auth.json` in `CODEX_HOME` with no separate path, so each App's Codex volume
  holds that App's own login (`ops/backends/login.ps1 codex -App <id>`), and Codex is unavailable to an App until
  the owner has logged it in.
- **Read-only config:** the user-level settings, hooks, MCP, rules and instruction files each CLI would load are
  mounted read-only over the writable state (harness-defined files from `cli_home/`, or empty read-only directories),
  so a session can't plant one for the next session in its own domain either.

- **End users (#365):** an App's end user who signs in with their own subscription gets one volume per (App, end
  user, backend), `harness-eu-<backend>-<hash of the ids>`, holding both their login and the CLI's state (Claude's
  login directory moves into it, `.login`). It takes the same read-only config and managed settings; no other
  login or token is mounted with it.

The daemon never mounts these volumes on the host: preparing them, erasing a conversation and removing an App's
volumes all run in throwaway containers. Findings per CLI version are in docs/phase8a-design.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path

from .sandbox import run_cmd

log = logging.getLogger("harness.cli_domains")

CLI_HOME = Path(__file__).resolve().parent / "cli_home"
AGENT_UID = "1000:1000"
_SAFE_SLUG = re.compile(r"[a-z0-9][a-z0-9_.-]{0,47}")
_HASHED_SLUG = re.compile(r"h-[0-9a-f]{24}")


@dataclass(frozen=True)
class Layout:
    """Where one CLI keeps its state and login inside the container, and what is read-only."""
    state_dir: str
    login_dir: str                      # "" when the login lives in the state (Codex)
    env: tuple[str, ...]
    ro_files: tuple[tuple[str, str], ...] = ()  # (path under state_dir, source file under cli_home/<backend>/)
    ro_dirs: tuple[str, ...] = ()       # paths under state_dir, mounted as empty read-only directories
    state_subdirs: tuple[str, ...] = ()  # created and handed to the agent user before the first session
    managed_files: tuple[tuple[str, str], ...] = ()  # (absolute path in the container, source file under cli_home/<backend>/)

    @property
    def per_app_login(self) -> bool:
        return not self.login_dir


LAYOUTS = {
    "claude": Layout(
        state_dir="/home/agent/.claude", login_dir="/home/agent/.claude-login",
        env=("CLAUDE_CONFIG_DIR=/home/agent/.claude", "CLAUDE_SECURESTORAGE_CONFIG_DIR=/home/agent/.claude-login"),
        ro_files=(("settings.json", "settings.json"), ("CLAUDE.md", "CLAUDE.md")),
        ro_dirs=("agents", "commands", "skills", "plugins", "hooks", "output-styles", "rules"),
        # Managed settings outrank project settings: allowManagedHooksOnly ignores `hooks` in the workspace's
        # .claude/settings.json and settings.local.json (#388), and the workspace CLAUDE.md still loads.
        managed_files=(("/etc/claude-code/managed-settings.json", "managed-settings.json"),)),
    "codex": Layout(
        state_dir="/home/agent/.codex", login_dir="",
        env=("CODEX_HOME=/home/agent/.codex",),
        ro_files=(("config.toml", "config.toml"), ("AGENTS.md", "AGENTS.md"), ("AGENTS.override.md", "AGENTS.md"),
                  ("hooks.json", "hooks.json")),
        ro_dirs=("rules", "skills", "prompts"),
        # Managed requirements outrank the workspace's .codex/config.toml: only the harness's MCP server may start,
        # so a project-scope `[mcp_servers.*]` command never runs (#394).
        managed_files=(("/etc/codex/requirements.toml", "requirements.toml"),)),
    # HOME stays the container's own /home/agent, which goes with the container: ~/.cursor (user-level mcp.json,
    # hooks.json, sandbox.json, rules, skills), ~/.claude and the shell's dotfiles never persist. Only the config
    # directory (chats, cli-config.json) and the data directory (transcripts) are kept.
    "cursor": Layout(
        state_dir="/home/agent/.cursor-state", login_dir="/home/agent/.config/cursor",
        env=("CURSOR_CONFIG_DIR=/home/agent/.cursor-state/config", "CURSOR_DATA_DIR=/home/agent/.cursor-state/data"),
        ro_files=(("config/permissions.json", "permissions.json"),),
        state_subdirs=("config", "data")),
}


END_USER_BACKENDS = ("claude", "codex")  # the backends an App's end user can sign in to (#365)
END_USER_LOGIN_SUBDIR = ".login"
_END_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}")


MEMBER_PREFIX = "member:"  # a household member's hosted session carries `member:<user_id>` as its end user (#393)


def valid_end_user(end_user: str) -> bool:
    """An end user's id is the App's own opaque string: short, printable and free of path or shell characters. The
    `member:` prefix is the harness's own, so an App can't name a member."""
    return bool(_END_USER_ID.fullmatch(end_user or "")) and not end_user.startswith(MEMBER_PREFIX)


def end_user_layout(backend: str) -> Layout:
    """`backend`'s layout for an end user's single volume: the login sits in the state volume, not in a shared one."""
    layout = LAYOUTS[backend]
    if not layout.login_dir:
        return layout
    state = layout.state_dir
    env = tuple(f"CLAUDE_SECURESTORAGE_CONFIG_DIR={state}/{END_USER_LOGIN_SUBDIR}"
                if item.startswith("CLAUDE_SECURESTORAGE_CONFIG_DIR=") else item for item in layout.env)
    return replace(layout, login_dir="", env=env, state_subdirs=(*layout.state_subdirs, END_USER_LOGIN_SUBDIR))


def end_user_volume(backend: str, app_id: str, end_user: str) -> str:
    """The one volume of (App, end user, backend). Named from a hash, so neither id (nor a lookalike) can pick another
    person's volume."""
    digest = hashlib.sha256(json.dumps([app_id, end_user], separators=(",", ":")).encode("utf-8")).hexdigest()[:24]
    return f"harness-eu-{backend}-{digest}"


def _layout(backend: str, end_user: str) -> Layout:
    return end_user_layout(backend) if end_user else LAYOUTS[backend]


def domain_slug(app_id: str) -> str:
    """`app_id` as a Docker volume name part: itself when it is short and safe, else a hash of it."""
    if _SAFE_SLUG.fullmatch(app_id) and not _HASHED_SLUG.fullmatch(app_id):
        return app_id
    return "h-" + hashlib.sha256(app_id.encode("utf-8")).hexdigest()[:24]


def _stem(backend: str, cfg) -> tuple[str, bool]:
    """The backend's configured Web volume, and whether it has the standard `harness-auth-<backend>` name."""
    volume = cfg.volume
    return volume, volume == f"harness-auth-{backend}"


def state_volume(backend: str, cfg, app_id: str = "") -> str:
    volume, standard = _stem(backend, cfg)
    if not app_id:
        return volume
    slug = domain_slug(app_id)
    return f"harness-cli-{backend}-app-{slug}" if standard else f"{volume}-app-{slug}"


def login_volume(backend: str, cfg, app_id: str = "") -> str:
    if LAYOUTS[backend].per_app_login:
        return state_volume(backend, cfg, app_id)
    volume, standard = _stem(backend, cfg)
    return f"harness-login-{backend}" if standard else f"{volume}-login"


def needs_app_login(backend: str) -> bool:
    return backend in LAYOUTS and LAYOUTS[backend].per_app_login


def docker_args(backend: str, cfg, app_id: str = "", *, token: bool = False, end_user: str = "") -> list[str]:
    """The env and mount arguments that give a session its domain's state, the login and the read-only config.
    With `token` (Claude's CLAUDE_CODE_OAUTH_TOKEN, #390) the session has no login volume: nothing can refresh, race
    over or overwrite a shared credential. With `end_user` (#365) the session has that person's own volume and never the
    token."""
    layout = _layout(backend, end_user)
    token = token and not end_user
    args: list[str] = []
    for item in layout.env:
        if token and item.startswith("CLAUDE_SECURESTORAGE_CONFIG_DIR="):
            continue
        args += ["-e", item]
    state = end_user_volume(backend, app_id, end_user) if end_user else state_volume(backend, cfg, app_id)
    args += ["-v", f"{state}:{layout.state_dir}"]
    if layout.login_dir and not token:
        args += ["-v", f"{login_volume(backend, cfg, app_id)}:{layout.login_dir}"]
    for target, source in layout.ro_files:
        args += ["--mount", f"type=bind,source={CLI_HOME / backend / source},"
                            f"target={layout.state_dir}/{target},readonly"]
    for target, source in layout.managed_files:
        args += ["--mount", f"type=bind,source={CLI_HOME / backend / source},target={target},readonly"]
    for target in layout.ro_dirs:
        args += ["--mount", f"type=tmpfs,target={layout.state_dir}/{target},tmpfs-mode=0555"]
    return args


def probe_args(backend: str, cfg, app_id: str = "", end_user: str = "") -> list[str]:
    """Mounts for a login-status probe: the login only. The CLI's state is a throwaway directory in the container,
    except for an end user's single volume (#365), which holds both."""
    layout = _layout(backend, end_user)
    if end_user:
        return [*[a for item in layout.env for a in ("-e", item)],
                "-v", f"{end_user_volume(backend, app_id, end_user)}:{layout.state_dir}"]
    login_dir = layout.login_dir or layout.state_dir
    return [*[a for item in layout.env for a in ("-e", item)],
            "-v", f"{login_volume(backend, cfg, app_id)}:{login_dir}"]


def prepare_command(backend: str, cfg, app_id: str = "", end_user: str = "") -> list[str]:
    """A throwaway root container that creates the domain's volumes and hands them to the agent user. A volume
    mounted where the image has no directory, and any directory Docker makes for a nested mount, would be root's."""
    layout = _layout(backend, end_user)
    state = end_user_volume(backend, app_id, end_user) if end_user else state_volume(backend, cfg, app_id)
    args = ["docker", "run", "--rm", "--network", "none", "--user", "0:0", "-v", f"{state}:/state"]
    owned = ["/state", *(f"/state/{d}" for d in layout.state_subdirs)]
    owned += [f"/state/{Path(t).parent.as_posix()}" for t, _ in layout.ro_files if Path(t).parent.as_posix() != "."]
    if layout.login_dir:
        args += ["-v", f"{login_volume(backend, cfg, app_id)}:/login"]
        owned.append("/login")
    owned = list(dict.fromkeys(owned))
    script = f"mkdir -p {' '.join(owned)} && chown {AGENT_UID} {' '.join(owned)}"
    return [*args, cfg.image, "sh", "-c", script]


_prepared: set[tuple[str, ...]] = set()


async def prepare(backend: str, cfg, app_id: str = "", end_user: str = "") -> None:
    """Run `prepare_command` once per daemon process for each domain's volumes."""
    key = ((backend, cfg.image, end_user_volume(backend, app_id, end_user)) if end_user else
           (backend, cfg.image, state_volume(backend, cfg, app_id), login_volume(backend, cfg, app_id)))
    if key in _prepared:
        return
    code, out, err = await run_cmd(prepare_command(backend, cfg, app_id, end_user), timeout=120)
    if code != 0:
        raise RuntimeError(f"could not prepare the {backend} volumes: {(err or out).strip()[:300]}")
    _prepared.add(key)


def erase_command(backend: str, cfg, app_id: str, conversation_id: str) -> list[str]:
    from . import cli_erase
    if not cli_erase.SAFE_ID.fullmatch(conversation_id):
        raise ValueError(f"unsafe {backend} conversation id {conversation_id!r}")
    return ["docker", "run", "--rm", "-i", "--network", "none", "--user", AGENT_UID,
            "-v", f"{state_volume(backend, cfg, app_id)}:{cli_erase.STATE_DIR}", cfg.image,
            "python3", "-", conversation_id]


async def erase_history(backend: str, cfg, app_id: str, conversation_id: str) -> None:
    """Delete one conversation's CLI history from its domain's state volume (session erase)."""
    from . import cli_erase
    source = Path(cli_erase.__file__).read_text(encoding="utf-8")  # the script goes in on stdin
    code, out, err = await run_cmd(erase_command(backend, cfg, app_id, conversation_id), timeout=120,
                                   input_=source)
    if code != 0:
        raise RuntimeError(f"could not erase {backend} history {conversation_id}: {(err or out).strip()[:300]}")
    log.info("erased %s history %s (%s items)", backend, conversation_id, out.strip())


def app_volumes(backends: dict, app_id: str) -> list[str]:
    """Every CLI volume that belongs to App `app_id` alone (never the shared login or Web's state)."""
    if not app_id:
        raise ValueError("Web's CLI volumes are never removed")
    names = []
    for backend, cfg in backends.items():
        if backend in LAYOUTS:
            names += [state_volume(backend, cfg, app_id), login_volume(backend, cfg, app_id)]
    return list(dict.fromkeys(n for n in names if n not in {login_volume(b, c) for b, c in backends.items()
                                                           if b in LAYOUTS}))


async def drop_app_volumes(backends: dict, app_id: str) -> None:
    """Remove App `app_id`'s CLI state and login volumes (App erase). Its sessions must be stopped first."""
    names = app_volumes(backends, app_id)
    if not names:
        return
    code, out, err = await run_cmd(["docker", "volume", "rm", "-f", *names], timeout=120)
    if code != 0:
        raise RuntimeError(f"could not remove the CLI volumes of App {app_id}: {(err or out).strip()[:300]}")
    log.info("removed the CLI volumes of App %s", app_id)


def end_user_volumes(app_id: str, end_user: str, backends=END_USER_BACKENDS) -> list[str]:
    return [end_user_volume(b, app_id, end_user) for b in backends]


async def drop_end_user_volumes(app_id: str, end_users: list[str], backends=END_USER_BACKENDS) -> None:
    """Remove every volume of these end users of App `app_id` (unlink, App erase). Their sessions must be stopped."""
    names = [n for e in end_users for n in end_user_volumes(app_id, e, backends)]
    if not names:
        return
    for key in [k for k in _prepared if k[-1] in names]:  # a re-link must prepare (and chown) the new volume
        _prepared.discard(key)
    code, out, err = await run_cmd(["docker", "volume", "rm", "-f", *names], timeout=120)
    if code != 0:
        raise RuntimeError(f"could not remove end-user volumes of App {app_id}: {(err or out).strip()[:300]}")
    log.info("removed %s end-user volumes", len(names))
