"""Subscription logins for an App's end users (#365).

An App request that names an `end_user` runs under that person's own Claude or Codex subscription. This module is
the credential source for those sessions and the broker for the sign-in itself:

- **Source** (`EndUserLogin`, a `credential_sources.CredentialSource`): the session gets that (App, end user,
  backend)'s own volume, which holds the CLI's login and state, and nothing else: never the owner's login, the
  owner's token (#390) or an App key. Without a login the session is refused (`end_user_login_required`).
- **Sign-in** (`EndUserLogins`): the CLI's own login runs in a throwaway container on that volume. Codex's
  device flow (`codex login --device-auth`) gives a URL and a user code to display; nothing comes back. Claude gives
  a URL, the person signs in on Anthropic's site and pastes the one-time code, which `submit_code` writes to the
  waiting `claude auth login`'s stdin, once, and nowhere else.

The code's rules (#365 decision 4): it goes straight to the process's stdin. It is never written to disk, logs,
events, the database or a transcript, and no response or error repeats it. It works once and the attempt expires after
`ATTEMPT_SECONDS`. Only the App that started the attempt (for that end user) can submit it. The credential stays in the
end user's volume: the daemon never mounts it and never returns it. Output of the login process is parsed for the URL
and the user code only; the rest is dropped, so a CLI that echoes input can't leak it.

Concurrency (decision 5): one end user's Claude sessions would share one refresh token, so `lock_key` allows one
active session per (App, end user, backend) and queues the next (the runner holds it). Codex is serialised the same
way, since its refresh can race alike.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import backend_state, cli_domains, credential_sources, member_keys
from .credential_sources import CredentialRefused
from .sandbox import run_cmd

log = logging.getLogger("harness.end_users")

ATTEMPT_SECONDS = 600           # a login attempt (and a code for it) lives this long
URL_WAIT_SECONDS = 45           # how long start waits for the CLI to print its sign-in URL
MAX_LIVE_ATTEMPTS = 32
REQUIRED = "end_user_login_required"
LOGIN_COMMANDS = {
    "claude": ["claude", "auth", "login"],
    "codex": ["codex", "login", "--device-auth"],
}
LOGOUT_COMMANDS = {
    "claude": ["claude", "auth", "logout"],
    "codex": ["codex", "logout"],
}
NEEDS_CODE = {"claude": True, "codex": False}
_URL = re.compile(r"https://[^\s\"'<>]+")
_USER_CODE = re.compile(r"\b[A-Z0-9]{4,5}-[A-Z0-9]{4,6}\b")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_CODE = re.compile(r"[^\s\x00-\x1f\x7f]{6,1024}")


class LoginError(Exception):
    """A login step the App can be told about: `status` and `code` become the API error."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


@dataclass
class Attempt:
    attempt_id: str
    app_id: str
    end_user: str
    backend: str
    deadline: float
    container: str = ""
    process: subprocess.Popen | None = None
    url: str = ""
    user_code: str = ""
    needs_code: bool = False
    code_submitted: bool = False
    state: str = "starting"          # starting, waiting, submitted, completed, failed, expired, cancelled
    seen: threading.Event = field(default_factory=threading.Event)   # the URL (and a Codex user code) appeared
    lock: threading.Lock = field(default_factory=threading.Lock)
    on_finish: Callable | None = None
    audit_incomplete: str = ""

    @property
    def live(self) -> bool:
        return self.state in ("starting", "waiting", "submitted")


class EndUserLogin(credential_sources.CredentialSource):
    name = "end_user_login"
    label = "end_user_login"

    def selects(self, app_id: str, end_user: str) -> bool:
        return bool(end_user) and not member_keys.member_of(end_user)   # a member runs on their own API key (#393)

    def docker_args(self, backend: str, cfg, app_id: str, end_user: str) -> list[str]:
        return cli_domains.docker_args(backend, cfg, app_id, end_user=end_user)

    async def ready(self, backend: str, cfg, app_id: str, end_user: str) -> None:
        if backend not in cli_domains.END_USER_BACKENDS:
            raise CredentialRefused("end_user_backend_unsupported",
                                    f"{backend} can't run on an end user's own login; use claude or codex")
        if not await asyncio.to_thread(backend_state.end_user_login_ready, backend, cfg, app_id, end_user):
            raise CredentialRefused(REQUIRED, f"this end user has not signed in to {backend.title()}; start "
                                              f"POST /api/v1/end-users/<id>/logins/{backend} and finish the sign-in")
        try:
            await cli_domains.prepare(backend, cfg, app_id, end_user)
        except RuntimeError as e:
            raise CredentialRefused("end_user_unavailable", str(e)) from e

    def lock_key(self, backend: str, app_id: str, end_user: str) -> str:
        return hashlib.sha256(f"{backend}\0{app_id}\0{end_user}".encode()).hexdigest()[:24]


SOURCE = credential_sources.register(EndUserLogin())


def _clean(line: str) -> str:
    return _ANSI.sub("", line).strip()


class EndUserLogins:
    """Starts, completes and revokes end users' CLI logins. `popen` and `command` exist for tests: a stub login
    process in place of `docker run`."""

    def __init__(self, cfg, *, popen: Callable = subprocess.Popen,
                 command: Callable[[str, str, str, str, str], list[str]] | None = None):
        self.cfg = cfg
        self._popen = popen
        self._docker = command is None   # False when a test supplies a stub login process
        self._command = command or self._docker_command
        self._attempts: dict[str, Attempt] = {}
        self._guard = threading.Lock()

    # --- the throwaway container -------------------------------------------------------------------------------
    def _docker_command(self, backend: str, app_id: str, end_user: str, attempt_id: str, container: str) -> list[str]:
        cfg = self.cfg.backends[backend]
        return ["docker", "run", "--rm", "-i", "--name", container,
                "--label", "agent-harness.end-user-login=1",
                "--network", cfg.network,
                "-e", f"HTTPS_PROXY={cfg.proxy}", "-e", f"HTTP_PROXY={cfg.proxy}",
                "-e", "NO_PROXY=localhost,127.0.0.1", "-e", "NODE_USE_ENV_PROXY=1",
                *cli_domains.docker_args(backend, cfg, app_id, end_user=end_user),
                "--memory", self.cfg.sandbox.memory, "--cpus", str(self.cfg.sandbox.cpus),
                "--pids-limit", str(self.cfg.sandbox.pids),
                "--security-opt", "no-new-privileges",
                "--cap-drop", "NET_RAW", "--cap-drop", "MKNOD", "--cap-drop", "AUDIT_WRITE",
                cfg.image, *LOGIN_COMMANDS[backend]]

    # --- start -------------------------------------------------------------------------------------------------
    async def start(self, app_id: str, end_user: str, backend: str, *, on_finish=None, attempt_id=None) -> dict:
        """Begin the CLI's own login for this end user: returns what the App's popup shows."""
        self.check(backend, end_user)
        cfg = self.cfg.backends[backend]
        if self._docker:
            try:
                await cli_domains.prepare(backend, cfg, app_id, end_user)
            except RuntimeError as e:
                raise LoginError(503, "end_user_unavailable", str(e)) from e
        attempt = await asyncio.to_thread(self._begin, app_id, end_user, backend, on_finish, attempt_id)
        if not await asyncio.to_thread(attempt.seen.wait, URL_WAIT_SECONDS) or not attempt.url:
            await asyncio.to_thread(self._end, attempt, "failed")
            raise LoginError(502, "login_failed", f"{backend.title()} did not offer a sign-in URL")
        return await asyncio.to_thread(self._view, attempt)

    @staticmethod
    def check(backend: str, end_user: str) -> None:
        if backend not in cli_domains.END_USER_BACKENDS:
            raise LoginError(400, "end_user_backend_unsupported",
                             f"end users can sign in to {' or '.join(cli_domains.END_USER_BACKENDS)}, not {backend}")
        if not cli_domains.valid_end_user(end_user):
            raise LoginError(400, "invalid_end_user", "end user ids are 1-128 letters, digits and _.@:-")

    def _begin(self, app_id: str, end_user: str, backend: str, on_finish=None, attempt_id=None) -> Attempt:
        with self._guard:
            self._expire()
            for old in [a for a in self._attempts.values()
                        if a.live and (a.app_id, a.end_user, a.backend) == (app_id, end_user, backend)]:
                self._end(old, "cancelled")  # a new attempt replaces the person's last one
            if sum(a.live for a in self._attempts.values()) >= MAX_LIVE_ATTEMPTS:
                raise LoginError(429, "too_many_logins", "too many sign-ins are in progress; retry shortly")
            attempt_id = attempt_id or secrets.token_urlsafe(16)
            attempt = Attempt(attempt_id, app_id, end_user, backend, time.time() + ATTEMPT_SECONDS,
                              container=f"harness-eulogin-{secrets.token_hex(6)}", needs_code=NEEDS_CODE[backend],
                              on_finish=on_finish)
            command = self._command(backend, app_id, end_user, attempt_id, attempt.container)
            try:
                attempt.process = self._popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                              errors="replace", bufsize=1,
                                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as e:
                raise LoginError(503, "end_user_unavailable", f"could not start the sign-in: {e.strerror}") from e
            self._attempts[attempt_id] = attempt
        threading.Thread(target=self._read, args=(attempt,), daemon=True).start()
        threading.Thread(target=self._watch, args=(attempt,), daemon=True).start()
        return attempt

    def _read(self, attempt: Attempt) -> None:
        """Keep only the sign-in URL (and Codex's user code) from the process's output; drop every other line."""
        proc = attempt.process
        assert proc is not None and proc.stdout is not None
        try:
            for raw in proc.stdout:
                line = _clean(raw)
                with attempt.lock:
                    if not attempt.url:
                        found = _URL.search(line)
                        if found:
                            attempt.url = found.group(0)
                    if attempt.backend == "codex" and not attempt.user_code:
                        code = _USER_CODE.search(line)
                        if code:
                            attempt.user_code = code.group(0)
                    shown = attempt.url and (attempt.user_code or attempt.backend != "codex")
                    if shown and attempt.state == "starting":
                        attempt.state = "waiting"
                        attempt.seen.set()
        except (OSError, ValueError):
            pass
        finally:
            attempt.seen.set()

    def _watch(self, attempt: Attempt) -> None:
        proc = attempt.process
        assert proc is not None
        try:
            code = proc.wait(timeout=max(1.0, attempt.deadline - time.time()))
        except subprocess.TimeoutExpired:
            self._end(attempt, "expired")
            return
        with attempt.lock:
            was_live = attempt.live
            if attempt.live:
                attempt.state = "completed" if code == 0 else "failed"
            if attempt.state == "completed":
                backend_state.forget_end_user_login(attempt.backend, attempt.app_id, attempt.end_user)
            if was_live and attempt.on_finish:
                attempt.on_finish(attempt)

    def _end(self, attempt: Attempt, state: str) -> None:
        with attempt.lock:
            if not attempt.live:
                return
            attempt.state = state
            if attempt.on_finish:
                attempt.on_finish(attempt)
        proc = attempt.process
        try:
            if proc is not None and proc.poll() is None:
                proc.kill()
        except OSError:
            pass
        if self._docker:  # `docker run` was killed; its container may outlive the client
            subprocess.run(["docker", "rm", "-f", attempt.container], capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)

    def _expire(self) -> None:
        now = time.time()
        for attempt in list(self._attempts.values()):
            if attempt.live and attempt.deadline < now:
                self._end(attempt, "expired")
            if not attempt.live and attempt.deadline + ATTEMPT_SECONDS < now:
                self._attempts.pop(attempt.attempt_id, None)

    # --- the code ----------------------------------------------------------------------------------------------
    def owned_attempt(self, app_id: str, end_user: str, backend: str, attempt_id: str) -> Attempt:
        """Resolve a known scoped target before recording intent; guessed caller strings are never audit ids."""
        attempt = self._attempts.get(attempt_id)
        if (attempt is None or (attempt.app_id, attempt.end_user, attempt.backend) != (app_id, end_user, backend)):
            raise LoginError(404, "no_such_attempt", "no sign-in attempt matches that id")  # not another App's either
        return attempt

    async def submit_code(self, app_id: str, end_user: str, backend: str, attempt_id: str, code: str) -> dict:
        """Write the pasted one-time code to the waiting login's stdin, once. It is not kept anywhere else."""
        attempt = self.owned_attempt(app_id, end_user, backend, attempt_id)
        if not attempt.needs_code:
            raise LoginError(409, "code_not_needed", f"{backend.title()}'s sign-in takes no code back")
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            raise LoginError(400, "invalid_code", "the code is missing or malformed")
        with attempt.lock:
            if attempt.live and attempt.deadline < time.time():
                expired = True
            else:
                expired = False
        if expired:
            await asyncio.to_thread(self._end, attempt, "expired")
        with attempt.lock:
            if attempt.code_submitted:
                raise LoginError(409, "code_already_submitted", "a code was already submitted for this attempt")
            if attempt.state != "waiting":
                raise LoginError(409, "attempt_not_waiting", f"this sign-in attempt is {attempt.state}")
            attempt.code_submitted = True        # single use: set before the write, so a retry can't send a second
            attempt.state = "submitted"
        proc = attempt.process
        assert proc is not None and proc.stdin is not None

        def write() -> None:
            proc.stdin.write(code + "\n")
            proc.stdin.flush()
        try:
            await asyncio.to_thread(write)
        except (OSError, ValueError) as e:
            await asyncio.to_thread(self._end, attempt, "failed")
            raise LoginError(502, "login_failed", "the sign-in process is no longer running") from e
        return await asyncio.to_thread(self._view, attempt)

    # --- state -------------------------------------------------------------------------------------------------
    def _view(self, attempt: Attempt) -> dict:
        # A terminal state and its settlement error become visible together, so polling clients cannot miss a gap.
        with attempt.lock:
            return self._locked_view(attempt)

    @staticmethod
    def _locked_view(attempt: Attempt) -> dict:
        view = {"attempt_id": attempt.attempt_id, "backend": attempt.backend, "state": attempt.state,
                "verification_url": attempt.url, "needs_code": attempt.needs_code,
                "expires_in": max(0, int(attempt.deadline - time.time()))}
        if attempt.user_code:
            view["user_code"] = attempt.user_code
        if attempt.audit_incomplete:
            view["error"] = {"code": "audit_record_incomplete", "operation_id": attempt.audit_incomplete,
                             "may_have_completed": True, "retryable": False}
        return view

    def attempt_state(self, app_id: str, end_user: str, backend: str) -> dict | None:
        """The end user's newest attempt on this backend, for the popup to poll; None when there is none."""
        with self._guard:
            self._expire()
            mine = [a for a in self._attempts.values() if (a.app_id, a.end_user, a.backend) == (app_id, end_user, backend)]
        if not mine:
            return None
        newest = max(mine, key=lambda a: a.deadline)
        view = self._view(newest)
        view.pop("verification_url", None)
        view.pop("user_code", None)
        return view

    async def status(self, app_id: str, end_user: str, backend: str) -> dict:
        self.check(backend, end_user)
        linked = await asyncio.to_thread(backend_state.end_user_login_ready, backend, self.cfg.backends[backend],
                                         app_id, end_user)
        attempt = await asyncio.to_thread(self.attempt_state, app_id, end_user, backend)
        return {"backend": backend, "linked": linked, "attempt": attempt}

    # --- revocation --------------------------------------------------------------------------------------------
    async def unlink(self, app_id: str, end_user: str, backend: str) -> None:
        """The CLI's own logout on the end user's volume, then the volume is deleted. Their sessions stop first (the
        manager does that)."""
        self.check(backend, end_user)
        with self._guard:
            for a in self._attempts.values():
                if a.live and (a.app_id, a.end_user, a.backend) == (app_id, end_user, backend):
                    self._end(a, "cancelled")
        cfg = self.cfg.backends[backend]
        volume = cli_domains.end_user_volume(backend, app_id, end_user)
        if await asyncio.to_thread(backend_state._volume_exists, volume):
            code, _out, _err = await run_cmd(
                ["docker", "run", "--rm", "--network", cfg.network, "-e", f"HTTPS_PROXY={cfg.proxy}",
                 "-e", "NODE_USE_ENV_PROXY=1", *cli_domains.probe_args(backend, cfg, app_id, end_user),
                 cfg.image, *LOGOUT_COMMANDS[backend]], timeout=60)
            if code != 0:  # the volume goes either way: the person is unlinked locally even if the CLI could not call out
                log.warning("an end user's CLI logout failed; their volume is deleted regardless")
        backend_state.forget_end_user_login(backend, app_id, end_user)
        try:
            await cli_domains.drop_end_user_volumes(app_id, [end_user], backends=(backend,))
        except RuntimeError as e:
            raise LoginError(503, "end_user_unavailable", f"could not remove the end user's {backend} volume") from e

    def cancel_app(self, app_id: str) -> None:
        """End every sign-in in progress for App `app_id` (App erase)."""
        with self._guard:
            for attempt in list(self._attempts.values()):
                if attempt.app_id == app_id:
                    self._end(attempt, "cancelled")

    def close(self) -> None:
        with self._guard:
            for attempt in list(self._attempts.values()):
                self._end(attempt, "cancelled")
