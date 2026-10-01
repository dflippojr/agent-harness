"""Connection lifecycle and credentialed Git operations for household-member GitHub sign-in (issue #63).

State machine per opaque `user_id` (persisted rows hold only non-secret state):

    disconnected --connect--> connecting --GCM stored a credential--> connected
    connecting --cancel/timeout/failure--> disconnected (or reconnect_required after a reconnect)
    connected --401 / missing credential--> reconnect_required --connect--> connecting
    any --disconnect--> disconnected (GCM erase in that namespace only)
    feature off / account disabled / store unusable --> reported as `disabled`; nothing is erased

Decisions recorded for the audit's open points:
- The probe (`gcm get`, noninteractive, output discarded) runs after connect, at daemon startup for rows that
  say `connected` (a restored backup may have no credential), and inside erase. Status reads never probe.
- Erase is a namespace-wide purge of github.com: `gcm erase` repeated until the probe reports nothing stored.
- Disconnect, owner reset, member disable, and feature disable stop in-flight credentialed clone/fetch/push
  for that member (the processes are killed) instead of letting them finish.
- Connect erases the member's previous credential first, so reauthorization replaces only that member's
  credential and GCM never silently returns the old one.
"""

from __future__ import annotations

import logging
import re
import secrets
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import github_auth as ga
from .github_auth import ERRORS, GitHubAuthError

log = logging.getLogger(__name__)

FEATURE_FLAG = "github_member_auth.enabled"
PREFLIGHT_TTL = 600
ATTEMPT_RETAIN = 120
STOP_GRACE = 3
_CODE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")
_BRANCH = re.compile(r"^agent/[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
PERSISTED = ("disconnected", "connected", "reconnect_required")
# last_error marking a credential erase that failed and is still owed (retried by reconcile()).
ERASE_FAILED = "erase_failed"


@dataclass
class Attempt:
    user_id: str
    started: float
    deadline: float
    nonce: str
    server: socket.socket
    prior: str
    url: str = ""
    code: str = ""
    outcome: str = ""            # "" while live, then "connected" or an ERRORS key
    finished_at: float = 0.0
    stop_reason: str = ""        # why the daemon stopped it: connect_cancelled, feature_disabled, ...
    conn: socket.socket | None = None
    procs: list = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)

    @property
    def live(self) -> bool:
        return not self.outcome


class MemberGitHub:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.ops = ga.MemberOps()
        self._attempts: dict[str, Attempt] = {}
        self._guard = threading.Lock()
        self._preflight: ga.Preflight | None = None
        self._preflight_at = 0.0
        self._closed = False

    # --- feature switch and availability ---------------------------------------------------------------

    def configured(self) -> bool:
        return ga.configured(self.cfg)

    def enabled(self) -> bool:
        return self.configured() and self.db.get_meta(FEATURE_FLAG, "0") == "1"

    def set_enabled(self, actor_id: str, enabled: bool) -> dict:
        if enabled and not self.configured():
            raise GitHubAuthError("not_configured", 409)
        self.db.set_meta(FEATURE_FLAG, "1" if enabled else "0")
        self.db.insert_audit(actor_id, "github_member_auth", "enable" if enabled else "disable", "ok")
        if enabled:
            self.preflight(refresh=True)
        else:
            self._stop_everyone("feature_disabled")
        return self.owner_view()

    def preflight(self, refresh: bool = False) -> ga.Preflight:
        now = time.monotonic()
        if refresh or self._preflight is None or now - self._preflight_at > PREFLIGHT_TTL:
            try:
                result = ga.run_preflight(self.cfg)
            except Exception:  # never let a broken store take the daemon down
                log.warning("GitHub member sign-in preflight failed unexpectedly")
                result = ga.Preflight(False, "store_unavailable")
            self._preflight, self._preflight_at = result, now
            if not result.ok:
                log.warning("GitHub member sign-in unavailable: %s", result.error)
        return self._preflight

    def cached_preflight(self) -> ga.Preflight | None:
        return self._preflight

    def availability(self, user_id: str, *, run_preflight: bool = True) -> str:
        """'' when this member may use GitHub sign-in now, else the ERRORS key that blocks it."""
        if not self.configured():
            return "not_configured"
        if not self.enabled():
            return "feature_disabled"
        account = self.db.account_by_id(user_id)
        if account is None or account.get("role") != "member" or not account.get("enabled", 1):
            return "account_disabled"
        pf = self.preflight() if run_preflight else self._preflight
        if pf is not None and not pf.ok:
            return pf.error or "store_unavailable"
        return ""

    # --- status ----------------------------------------------------------------------------------------

    def _row(self, user_id: str) -> dict:
        return self.db.get_github_connection(user_id) or {"status": "disconnected", "last_used_at": None,
                                                         "last_error": "", "connected_at": None}

    def _attempt(self, user_id: str) -> Attempt | None:
        with self._guard:
            att = self._attempts.get(user_id)
            if att and not att.live and time.time() - att.finished_at > ATTEMPT_RETAIN:
                self._attempts.pop(user_id, None)
                return None
            return att

    def status(self, user_id: str, *, include_prompt: bool = False) -> dict:
        """Public status. The device prompt is included only for the member's own request."""
        row = self._row(user_id)
        blocked = self.availability(user_id)
        att = self._attempt(user_id)
        status = row.get("status") or "disconnected"
        if status not in PERSISTED:
            status = "disconnected"
        out = {
            "status": status,
            "deadline": None,
            "seconds_left": None,
            "last_used_at": row.get("last_used_at"),
            "error": row.get("last_error") or "",
            "reason": "",
            "scopes_note": ga.GCM_SCOPES_NOTE,
        }
        if att is not None and att.live:
            out["status"] = "connecting"
            out["deadline"] = att.deadline
            out["seconds_left"] = max(0, int(att.deadline - time.time()))
            out["error"] = ""
            if include_prompt and att.code:
                out["prompt"] = {"verification_uri": att.url, "user_code": att.code}
        elif att is not None and att.outcome and att.outcome != "connected":
            out["error"] = att.outcome
        if blocked:
            out["status"] = "disabled"
            out["reason"] = blocked
        out["message"] = ERRORS.get(out["reason"] or out["error"], "") if (out["reason"] or out["error"]) else ""
        return out

    def owner_view(self) -> dict:
        """Coarse per-member state for Owner Settings: no URLs, usernames, scopes, or repositories."""
        pf = self._preflight
        members = []
        rows = {r["user_id"]: r for r in self.db.list_github_connections()}
        for account in self.db.list_accounts():
            if account.get("role") != "member":
                continue
            uid = account["user_id"]
            row = rows.get(uid) or {}
            att = self._attempt(uid)
            status = "connecting" if att is not None and att.live else (row.get("status") or "disconnected")
            members.append({"user_id": uid, "display_name": account.get("display_name") or "",
                            "status": status, "last_used_at": row.get("last_used_at")})
        return {
            "configured": self.configured(),
            "enabled": self.enabled(),
            "preflight": pf.public() if pf is not None else None,
            "scopes_note": ga.GCM_SCOPES_NOTE,
            "members": members,
        }

    # --- connect ---------------------------------------------------------------------------------------

    def connect(self, user_id: str) -> dict:
        """Start a device sign-in, or resume this member's live attempt. One attempt per member."""
        if self._closed:
            raise GitHubAuthError("feature_disabled", 503)
        blocked = self.availability(user_id)
        if blocked:
            raise GitHubAuthError(blocked, 403 if blocked == "account_disabled" else 409)
        with self._guard:
            current = self._attempts.get(user_id)
            resume = current is not None and current.live
            if not resume:
                att = self._new_attempt(user_id)
        if resume:
            return self.status(user_id, include_prompt=True)
        threading.Thread(target=self._run_attempt, args=(att,), daemon=True,
                         name=f"github-connect-{user_id[:8]}").start()
        self.db.insert_audit(user_id, user_id, "github_connect", "started")
        # give the helper a moment so the first response usually carries the code
        for _ in range(40):
            if att.code or not att.live:
                break
            time.sleep(0.1)
        return self.status(user_id, include_prompt=True)

    def _new_attempt(self, user_id: str) -> Attempt:
        """Register a new attempt with its one-time loopback relay; the caller holds `_guard`."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        now = time.time()
        att = Attempt(user_id=user_id, started=now, deadline=now + ga.DEVICE_FLOW_SECONDS,
                      nonce=secrets.token_urlsafe(24), server=server,
                      prior=self._row(user_id).get("status") or "disconnected")
        self._attempts[user_id] = att
        return att

    def cancel(self, user_id: str, reason: str = "connect_cancelled") -> dict:
        att = self._attempt(user_id)
        if att is not None and att.live:
            self._stop_attempt(att, reason)
            for _ in range(50):
                if not att.live:
                    break
                time.sleep(0.1)
        return self.status(user_id, include_prompt=True)

    def _stop_attempt(self, att: Attempt, reason: str) -> None:
        if not att.stop_reason:
            att.stop_reason = reason
        att.stop.set()
        self._close_relay(att)

    @staticmethod
    def _close_relay(att: Attempt) -> None:
        for sock in (att.conn, att.server):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _relay(self, att: Attempt) -> None:
        """Accept the helper's single connection; keep only a valid URL and code, in memory."""
        att.server.settimeout(0.5)
        while att.live and not att.stop.is_set() and time.time() < att.deadline:
            try:
                conn, _ = att.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(10)
            try:
                data = b""
                while b"\n" not in data and len(data) < 512:
                    chunk = conn.recv(512)
                    if not chunk:
                        break
                    data += chunk
                nonce, url, code = (data.split(b"\n", 1)[0].decode("ascii", "replace").split("\t") + ["", "", ""])[:3]
            except OSError:
                conn.close()
                continue
            if (not secrets.compare_digest(nonce.encode(), att.nonce.encode())
                    or url.rstrip("/") not in ga.VERIFICATION_URLS or not _CODE.match(code)):
                conn.close()
                continue
            att.url, att.code, att.conn = url.rstrip("/"), code, conn
            try:
                att.server.close()
            except OSError:
                pass
            return

    def _spawn(self, att: Attempt) -> tuple[subprocess.Popen, subprocess.Popen]:
        exe = str(ga.gcm_executable(self.cfg))
        env = ga.broker_env(self.cfg, att.user_id, connect=True)
        host, port = att.server.getsockname()[:2]
        env["GCM_GITHUB_HELPER"] = str(ga.helper_command(self.cfg))
        env["AGENT_HARNESS_GCM_RELAY"] = f"{host}:{port}"
        env["AGENT_HARNESS_GCM_NONCE"] = att.nonce
        store_env = ga.broker_env(self.cfg, att.user_id)
        kw = ga._popen_kwargs()
        getter = subprocess.Popen([exe, "get"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env, **kw)
        # The credential flows from GCM `get` straight into GCM `store`; this process never reads it.
        storer = subprocess.Popen([exe, "store"], stdin=getter.stdout, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, env=store_env, **kw)
        getter.stdout.close()
        for proc in (getter, storer):
            self.ops.register(att.user_id, proc)
            att.procs.append(proc)
        try:
            getter.stdin.write(ga.QUERY)
            getter.stdin.close()
        except OSError:
            pass
        return getter, storer

    def _run_attempt(self, att: Attempt) -> None:
        uid = att.user_id
        lock = self.ops.lock(uid)
        outcome = "authorization_failed"
        if not lock.acquire(timeout=60):
            self._finish(att, "busy")
            return
        try:
            try:
                ga.erase(self.cfg, uid)
            except GitHubAuthError as e:
                outcome = e.code
                return
            if att.prior == "connected":
                self.db.set_github_connection(uid, status="reconnect_required", last_error="")
            if att.stop.is_set():
                outcome = att.stop_reason or "connect_cancelled"
                return
            relay = threading.Thread(target=self._relay, args=(att,), daemon=True)
            relay.start()
            try:
                getter, storer = self._spawn(att)
            except (OSError, GitHubAuthError):
                outcome = "incompatible_gcm"
                return
            tail = _Tail(getter.stderr)
            outcome = self._wait(att, getter, storer, tail)
        finally:
            lock.release()
            self._finish(att, outcome)

    def _wait(self, att: Attempt, getter, storer, tail: "_Tail") -> str:
        from .clone import _stop_clone
        while getter.poll() is None:
            if att.stop.is_set() or time.time() >= att.deadline:
                reason = att.stop_reason or "connect_timeout"
                self._close_relay(att)  # the helper exits and GCM cancels on its own
                try:
                    getter.wait(STOP_GRACE)
                except subprocess.TimeoutExpired:
                    pass
                for proc in (getter, storer):
                    _stop_clone(proc)
                return reason
            time.sleep(0.2)
        try:
            storer.wait(30)
        except subprocess.TimeoutExpired:
            _stop_clone(storer)
            return "store_unavailable"
        text = tail.text()
        if getter.returncode != 0:
            if att.stop.is_set():
                return att.stop_reason or "connect_cancelled"
            if not att.code:
                if re.search(r"(?i)prompt|terminal|interactiv|tty|desktop", text):
                    return "unsupported_context"
                return "authorization_failed"
            if re.search(r"(?i)expired", text):
                return "connect_timeout"
            return "authorization_failed"
        if storer.returncode != 0:
            return "store_unavailable"
        # probe() is True/False when it can tell, None when it cannot (timeout, busy keyring).
        # GCM reported a successful store, so an unknown result may still mean a stored
        # credential: retry briefly, then treat it as possibly stored so _finish erases it.
        for delay in (0, 0.5, 1.5):
            time.sleep(delay)
            found = ga.probe(self.cfg, att.user_id)
            if found is not None:
                return "connected" if found else "store_unavailable"
        return "store_unverified"

    def _finish(self, att: Attempt, outcome: str) -> None:
        uid = att.user_id
        self._close_relay(att)
        for proc in att.procs:
            self.ops.unregister(uid, proc)
        att.code = ""
        att.url = ""
        att.finished_at = time.time()
        if outcome == "connected" and not att.stop.is_set():
            now = time.time()
            self.db.set_github_connection(uid, status="connected", connected_at=now, last_error="",
                                          namespace_version=ga.NAMESPACE_VERSION)
            self.db.insert_audit(uid, uid, "github_connect", "ok")
        else:
            # A credential may be in the store if GCM stored it before a cancel/disable, or if the
            # post-store check could not verify it. Never leave one behind a failed attempt.
            may_hold = outcome in ("connected", "store_unverified")
            if outcome == "connected":
                outcome = att.stop_reason or "connect_cancelled"
            if may_hold:
                try:
                    ga.erase(self.cfg, uid)
                except GitHubAuthError:
                    outcome = ERASE_FAILED  # reconcile() retries the erase at startup
            # connect erased the previous credential first, so a failed attempt never leaves `connected`
            status = "reconnect_required" if att.prior in ("connected", "reconnect_required") else "disconnected"
            self.db.set_github_connection(uid, status=status, last_error=outcome)
            self.db.insert_audit(uid, uid, "github_connect", outcome)
        att.outcome = outcome
        att.stop.set()

    # --- disconnect / reset / disable ------------------------------------------------------------------

    def disconnect(self, user_id: str, *, actor_id: str | None = None) -> dict:
        """Member disconnect or owner erase-only reset: stop work, erase in this namespace, fail closed."""
        att = self._attempt(user_id)
        if att is not None and att.live:
            self._stop_attempt(att, "connect_cancelled")
        self.ops.kill(user_id)
        if not self.configured():
            raise GitHubAuthError("not_configured", 409)
        lock = self.ops.lock(user_id)
        if not lock.acquire(timeout=60):
            raise GitHubAuthError("busy", 409)
        try:
            self.db.set_github_connection(user_id, status="disconnected", last_error="")
            ga.erase(self.cfg, user_id)
        except GitHubAuthError as e:
            # Fail closed (credentialed Git needs `connected`) and mark the erase as still owed;
            # reconcile() retries it at startup. The caller sees the error.
            self.db.set_github_connection(user_id, status="disconnected", last_error=ERASE_FAILED)
            self.db.insert_audit(actor_id or user_id, user_id, "github_erase", e.code)
            raise
        finally:
            lock.release()
        self.db.insert_audit(actor_id or user_id, user_id, "github_erase", "ok")
        return self.status(user_id)

    def member_disabled(self, user_id: str) -> None:
        """Account disabled: cancel attempts and stop credentialed Git now. The credential is not erased."""
        att = self._attempt(user_id)
        if att is not None and att.live:
            self._stop_attempt(att, "account_disabled")
        self.ops.kill(user_id)

    def _stop_everyone(self, reason: str, wait: float = 0) -> None:
        with self._guard:
            attempts = [a for a in self._attempts.values() if a.live]
        for att in attempts:
            self._stop_attempt(att, reason)
        self.ops.kill_all()
        end = time.monotonic() + wait
        while wait and time.monotonic() < end and any(a.live for a in attempts):
            time.sleep(0.05)

    def shutdown(self) -> None:
        """Daemon stop: invalidate every device prompt and credentialed Git process before returning."""
        self._closed = True
        self._stop_everyone("connect_cancelled", wait=STOP_GRACE + 3)

    def owes_erase(self) -> bool:
        """Whether some member's credential erase failed earlier and still has to be retried."""
        return self.configured() and any(row.get("last_error") == ERASE_FAILED
                                         for row in self.db.list_github_connections())

    def reconcile(self) -> None:
        """Startup: retry erases that failed earlier, and move a row that says connected but whose
        credential is gone (restored backup) to reconnect_required."""
        if not self.configured() or not self.preflight().ok:
            return
        for row in self.db.list_github_connections():
            if row.get("last_error") == ERASE_FAILED and row.get("status") != "connected":
                try:
                    ga.erase(self.cfg, row["user_id"])
                except GitHubAuthError:
                    continue  # still owed; try again next start
                self.db.set_github_connection(row["user_id"], status=row["status"], last_error="")
                self.db.insert_audit(row["user_id"], row["user_id"], "github_erase", "ok_retry")
                continue
            if not self.enabled() or row.get("status") != "connected":
                continue
            found = ga.probe(self.cfg, row["user_id"])
            if found is False:
                self.db.set_github_connection(row["user_id"], status="reconnect_required",
                                              last_error="reconnect_required")

    # --- credentialed Git ------------------------------------------------------------------------------

    def _begin(self, user_id: str) -> tuple[threading.Lock, int]:
        self._require_ready(user_id)
        lock = self.ops.lock(user_id)
        if not lock.acquire(timeout=30):
            raise GitHubAuthError("busy", 409)
        try:
            self._require_ready(user_id)  # revalidate after waiting: a disconnect or disable may have won
        except GitHubAuthError:
            lock.release()
            raise
        return lock, self.ops.generation(user_id)

    def _require_ready(self, user_id: str) -> None:
        blocked = self.availability(user_id)
        if blocked:
            raise GitHubAuthError(blocked, 403 if blocked == "account_disabled" else 409)
        status = self._row(user_id).get("status")
        if status == "reconnect_required":
            raise GitHubAuthError("reconnect_required", 409)
        if status != "connected":
            raise GitHubAuthError("not_connected", 409)

    def require_connected(self, user_id: str) -> None:
        self._require_ready(user_id)

    def _failed(self, user_id: str, raw: str) -> GitHubAuthError:
        code = ga.classify_git_failure(raw)
        if code == "reconnect_required":
            self.db.set_github_connection(user_id, status="reconnect_required", last_error=code)
            try:
                ga.erase(self.cfg, user_id)  # the rejected credential, in this namespace only
            except GitHubAuthError:
                pass
        return GitHubAuthError(code, 409)

    def _used(self, user_id: str) -> None:
        self.db.set_github_connection(user_id, last_used_at=time.time(), last_error="")

    def _git(self, user_id: str, args: list[str], *, timeout: int, generation: int) -> None:
        git = ga.git_executable(self.cfg)
        env = ga.broker_env(self.cfg, user_id)
        cmd = [git, *ga.git_config_args(self.cfg, user_id), *args]
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", env=env, **ga._popen_kwargs())
        self.ops.register(user_id, proc)
        try:
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                from .clone import _stop_clone
                _stop_clone(proc)
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                raise GitHubAuthError("timeout", 504) from None
        finally:
            self.ops.unregister(user_id, proc)
        if self.ops.generation(user_id) != generation:
            raise GitHubAuthError("cancelled", 409)
        if proc.returncode != 0:
            raise self._failed(user_id, (out or "") + (err or ""))

    def clone(self, user_id: str, url: str, dest: Path, root: Path, max_bytes: int | None = None) -> str:
        """Credentialed clone of a canonical GitHub URL into the member's repository root. Returns the origin."""
        from . import clone as clone_mod
        from .projects import GitError
        from .storage import ContainmentError, require_contained
        canonical = ga.canonical_github_url(url)
        lock, generation = self._begin(user_id)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                require_contained(dest.parent, root)
            except ContainmentError:
                raise GitHubAuthError("policy_rejected", 400) from None
            if dest.exists():
                raise GitHubAuthError("policy_rejected", 400, "a project with that name already exists")
            git = ga.git_executable(self.cfg)
            env = ga.broker_env(self.cfg, user_id)
            hooks = ga.member_dir(self.cfg, user_id) / "empty-hooks"
            template = ga.member_dir(self.cfg, user_id) / "empty-template"
            cmd = [git, *ga.git_config_args(self.cfg, user_id), "clone", "--quiet", f"--template={template}",
                   "--no-recurse-submodules", "--config", "core.autocrlf=false",
                   "--config", f"core.hooksPath={hooks.as_posix()}", "--", canonical, str(dest)]
            started: list = []

            def track(proc) -> None:
                started.append(proc)
                self.ops.register(user_id, proc)

            try:
                clone_mod._run_clone(cmd, dest, max_bytes=max_bytes, env=env, on_start=track)
            except clone_mod.QuotaExceeded:
                raise GitHubAuthError("quota", 507) from None
            except GitError as e:
                clone_mod._remove_tree(dest)
                if self.ops.generation(user_id) != generation:
                    raise GitHubAuthError("cancelled", 409) from None
                if "timed out" in str(e):
                    raise GitHubAuthError("timeout", 504) from None
                raise self._failed(user_id, str(e)) from None
            finally:
                for proc in started:
                    self.ops.unregister(user_id, proc)
            if self.ops.generation(user_id) != generation:
                clone_mod._remove_tree(dest)
                raise GitHubAuthError("cancelled", 409)
            try:
                require_contained(dest, root)
            except ContainmentError:
                clone_mod._remove_tree(dest)
                raise GitHubAuthError("policy_rejected", 400) from None
            self._used(user_id)
            return canonical
        finally:
            lock.release()

    def _managed(self, user_id: str, canonical: str, repo: Path, root: Path) -> tuple[str, Path]:
        from .storage import ContainmentError, require_contained
        if ga.canonical_github_url(canonical) != canonical:
            raise GitHubAuthError("policy_rejected", 409)
        try:
            require_contained(repo, root, allow_missing=False)
        except ContainmentError:
            raise GitHubAuthError("policy_rejected", 409) from None
        hooks = ga.member_dir(self.cfg, user_id) / "empty-hooks"
        ga.check_repo_config(ga.git_executable(self.cfg), repo, ga.broker_env(self.cfg, user_id), canonical,
                             hooks)
        return canonical, hooks

    def fetch(self, user_id: str, canonical: str, repo: Path, root: Path) -> str:
        """Fetch the stored origin into the managed repository, then fast-forward its branch when possible.

        Returns '' or 'diverged' (the managed branch has local merges, so it was left as is).
        """
        lock, generation = self._begin(user_id)
        try:
            canonical, _ = self._managed(user_id, canonical, repo, root)
            self._git(user_id, ["-C", str(repo), "fetch", "--quiet", "--prune", "--recurse-submodules=no",
                                "--", canonical, "+refs/heads/*:refs/remotes/origin/*"],
                      timeout=300, generation=generation)
            self._used(user_id)
            return _fast_forward(self.cfg, user_id, repo)
        finally:
            lock.release()

    def push(self, user_id: str, canonical: str, repo: Path, root: Path, branch: str) -> str:
        """Push exactly `refs/heads/<branch>` (a session branch) to the stored origin. No force, tags, deletes."""
        if not _BRANCH.match(branch or "") or ".." in branch or branch.endswith((".lock", ".")):
            raise GitHubAuthError("policy_rejected", 400)
        lock, generation = self._begin(user_id)
        try:
            canonical, _ = self._managed(user_id, canonical, repo, root)
            ref = f"refs/heads/{branch}"
            self._git(user_id, ["-C", str(repo), "push", "--quiet", "--no-verify", "--recurse-submodules=no",
                                "--", canonical, f"{ref}:{ref}"], timeout=300, generation=generation)
            self._used(user_id)
            return f"pushed {branch} to {ga.display_repo(canonical)}"
        finally:
            lock.release()


def _fast_forward(cfg, user_id: str, repo: Path) -> str:
    """Fast-forward the managed checkout to origin/<branch>; credential-free (no network)."""
    git = ga.git_executable(cfg)
    env = ga.broker_env(cfg, user_id)
    base = [git, "-c", "credential.helper=", "-c", "core.fsmonitor=false",
            "-c", f"core.hooksPath={(ga.member_dir(cfg, user_id) / 'empty-hooks').as_posix()}", "-C", str(repo)]

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([*base, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=120, stdin=subprocess.DEVNULL, **ga._popen_kwargs())

    head = run("symbolic-ref", "--quiet", "--short", "HEAD")
    branch = head.stdout.strip()
    if head.returncode != 0 or not branch:
        return ""
    remote = f"refs/remotes/origin/{branch}"
    if run("rev-parse", "--verify", "--quiet", remote).returncode != 0:
        return ""
    if run("merge-base", "--is-ancestor", "HEAD", remote).returncode != 0:
        return "diverged"
    return "" if run("merge", "--ff-only", "--quiet", remote).returncode == 0 else "diverged"


class _Tail:
    """Drain a child's stderr into a small in-memory buffer (never logged) so the pipe cannot fill."""

    def __init__(self, stream, limit: int = 4096):
        self._buf = b""
        self._limit = limit
        self._thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self._thread.start()

    def _read(self, stream) -> None:
        try:
            for chunk in iter(lambda: stream.read(512), b""):
                self._buf = (self._buf + chunk)[-self._limit:]
        except (OSError, ValueError):
            pass

    def text(self) -> str:
        self._thread.join(timeout=2)
        return self._buf.decode("utf-8", "replace")
