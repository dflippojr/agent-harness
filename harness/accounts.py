"""Owner-provisioned household member accounts.

Creation immediately allocates an opaque `user_id`. First login does not create identity. Display-name
edits do not affect identity. An owner-only login rebind keeps the same `user_id`, invalidates the old
login immediately, and is audited. SQLite stores non-secret metadata only.
"""

from __future__ import annotations

import logging
import re
import secrets
import time

from . import audit_context
from .manager import HarnessError
from .principal import (AUDIT_RETENTION_DAYS, DEFAULT_DISK_QUOTA_BYTES, DEFAULT_MAX_QUEUED,
                        DEFAULT_MAX_RUNNING, OWNER_USER_ID)
from .storage import account_usage_bytes, ensure_user_dirs

LOGIN_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")
NAME_MAX = 80
HINT_LEN = 8
log = logging.getLogger(__name__)
ONE_ROLE_ONLY = "a login cannot occupy more than one role"


def account_hint(user_id: str) -> str:
    text = (user_id or "").replace("u-", "", 1)
    return text[:HINT_LEN]


def validate_login(login: str) -> str:
    text = (login or "").strip()
    if not LOGIN_RE.fullmatch(text):
        raise HarnessError(400, "login must be an exact Tailscale login (user@host)")
    return text


def validate_display_name(name: str) -> str:
    text = " ".join((name or "").split())
    if not text:
        raise HarnessError(400, "display name is required")
    if len(text) > NAME_MAX:
        raise HarnessError(400, "display name is too long")
    return text


class AccountService:
    def __init__(self, manager):
        self.m = manager

    def _cfg(self):
        return self.m.cfg

    def _db(self):
        return self.m.db

    def _audit(self, actor, target_id: str, action: str, outcome: str, detail: str = "",
               metadata: dict | None = None) -> None:
        """One audit row with the actor's context. Inside a `db.write` it commits or rolls back with the change."""
        ctx = audit_context.coerce(actor)
        try:
            self._db().insert_audit(ctx.actor_id, target_id, action, outcome, detail, context=ctx,
                                    target_kind="account", metadata=metadata)
        except Exception:
            log.exception("audit write failed for %s", action)
            raise HarnessError(503, "audit record could not be written; the change was not applied",
                               code="audit_unavailable") from None

    def _deny(self, actor, target_id: str, action: str, detail: str, reason: str) -> None:
        self._audit(actor, target_id, action, "denied", detail, {"reason": reason})

    def _apply(self, fn):
        """Run a mutation and its audit row as one main-store transaction; an audit failure undoes the mutation."""
        return self._db().write(fn)

    def _occupied_logins(self) -> set[str]:
        cfg = self._cfg()
        occupied = set(cfg.allowed_logins)
        occupied.update(g.login for g in cfg.guests if g.login)
        occupied.update(a["login"] for a in self._db().list_accounts())
        return occupied

    def require_open_owner_safe(self) -> None:
        if not self._cfg().allowed_logins:
            raise HarnessError(
                400, "creating a household member requires an explicit allowed_logins owner allowlist"
            )

    def create(self, actor_id: str, login: str, display_name: str,
               disk_quota_bytes: int | None = None, max_running: int | None = None,
               max_queued: int | None = None) -> dict:
        self.require_open_owner_safe()
        login = validate_login(login)
        display_name = validate_display_name(display_name)
        if login in self._cfg().allowed_logins:
            self._deny(actor_id, "", "create", "login is an owner", "login_is_owner")
            raise HarnessError(400, ONE_ROLE_ONLY)
        if any(g.login == login for g in self._cfg().guests):
            self._deny(actor_id, "", "create", "login is a guest", "login_is_guest")
            raise HarnessError(400, ONE_ROLE_ONLY)
        if login in self._cfg().google_signin.admitted_logins:
            self._deny(actor_id, "", "create", "login is a Google-admitted device", "login_is_google_device")
            raise HarnessError(400, ONE_ROLE_ONLY)
        if self._db().account_by_login(login) is not None:
            self._deny(actor_id, "", "create", "login already a member", "login_already_member")
            raise HarnessError(409, "a household account already uses that login")
        quota = DEFAULT_DISK_QUOTA_BYTES if disk_quota_bytes is None else int(disk_quota_bytes)
        running = DEFAULT_MAX_RUNNING if max_running is None else int(max_running)
        queued = DEFAULT_MAX_QUEUED if max_queued is None else int(max_queued)
        if quota < 1 or running < 0 or queued < 0:
            raise HarnessError(400, "quota and concurrency limits must be non-negative")
        if running < 1:
            raise HarnessError(400, "max_running must be at least 1")
        if queued < 1:
            raise HarnessError(400, "max_queued must be at least 1")
        user_id = "u-" + secrets.token_hex(16)
        now = time.time()
        row = {
            "user_id": user_id, "role": "member", "login": login, "display_name": display_name,
            "enabled": 1, "disk_quota_bytes": quota, "max_running": running, "max_queued": queued,
            "created_at": now, "updated_at": now, "last_activity_at": None,
        }
        ensure_user_dirs(self._cfg(), user_id)

        def commit() -> None:
            self._db().insert_account(row)
            self._db().insert_member_project({
                "user_id": user_id, "slug": "scratch", "description": "Empty workspace for this account",
                "repo": "", "source_url": "",
            })
            self._audit(actor_id, user_id, "create", "ok", metadata={
                "fields": ["disk_quota_bytes", "max_running", "max_queued"], "new_disk_quota_bytes": quota,
                "new_max_running": running, "new_max_queued": queued})
        self._apply(commit)
        return self.public_account(row)

    def rename(self, actor_id: str, user_id: str, display_name: str) -> dict:
        account = self._require(user_id)
        display_name = validate_display_name(display_name)
        def commit() -> None:
            self._db().update_account(user_id, display_name=display_name)
            self._audit(actor_id, user_id, "rename", "ok", metadata={"fields": ["display_name"]})
        self._apply(commit)
        return self.public_account(self._db().account_by_id(user_id) or account)

    def rebind_login(self, actor_id: str, user_id: str, login: str) -> dict:
        account = self._require(user_id)
        login = validate_login(login)
        if login == account["login"]:
            return self.public_account(account)
        if (login in self._cfg().allowed_logins or any(g.login == login for g in self._cfg().guests)
                or login in self._cfg().google_signin.admitted_logins):
            self._deny(actor_id, user_id, "rebind", "login occupies another role", "login_occupies_role")
            raise HarnessError(400, ONE_ROLE_ONLY)
        existing = self._db().account_by_login(login)
        if existing is not None and existing["user_id"] != user_id:
            self._deny(actor_id, user_id, "rebind", "login already a member", "login_already_member")
            raise HarnessError(409, "a household account already uses that login")
        def commit() -> None:
            self._db().update_account(user_id, login=login)
            self._audit(actor_id, user_id, "rebind", "ok", metadata={"fields": ["login"]})
        self._apply(commit)
        self.m.google_signin.member_rebound(user_id)
        self.m.revoke_member_streams(user_id)
        return self.public_account(self._db().account_by_id(user_id))

    async def set_enabled(self, actor_id: str, user_id: str, enabled: bool) -> dict:
        account = self._require(user_id)
        was = bool(account.get("enabled", 1))
        if was == enabled:
            return self.public_account(account)
        def commit() -> None:
            self._db().update_account(user_id, enabled=int(enabled))
            self._audit(actor_id, user_id, "enable" if enabled else "disable", "ok",
                        metadata={"fields": ["enabled"], "old_enabled": was, "new_enabled": enabled})
        await self._db().awrite(commit)
        if not enabled:
            await self.m.disable_member(user_id, actor_id=audit_context.coerce(actor_id).actor_id)
        return self.public_account(self._db().account_by_id(user_id))

    def set_quota(self, actor_id: str, user_id: str, disk_quota_bytes: int) -> dict:
        account = self._require(user_id)
        quota = int(disk_quota_bytes)
        if quota < 1:
            raise HarnessError(400, "disk quota must be at least 1 byte")

        def commit() -> None:
            self._db().update_account(user_id, disk_quota_bytes=quota)
            self._audit(actor_id, user_id, "quota", "ok", metadata={
                "fields": ["disk_quota_bytes"], "old_disk_quota_bytes": int(account["disk_quota_bytes"]),
                "new_disk_quota_bytes": quota})
        self._apply(commit)
        return self.public_account(self._db().account_by_id(user_id))

    def set_concurrency(self, actor_id: str, user_id: str, max_running: int | None = None,
                        max_queued: int | None = None) -> dict:
        account = self._require(user_id)
        fields = {}
        if max_running is not None:
            if int(max_running) < 1:
                raise HarnessError(400, "max_running must be at least 1")
            fields["max_running"] = int(max_running)
        if max_queued is not None:
            if int(max_queued) < 1:
                raise HarnessError(400, "max_queued must be at least 1")
            fields["max_queued"] = int(max_queued)
        if fields:
            metadata: dict = {"fields": list(fields)}
            for name, value in fields.items():
                metadata["old_" + name] = int(account[name])
                metadata["new_" + name] = value

            def commit() -> None:
                self._db().update_account(user_id, **fields)
                self._audit(actor_id, user_id, "concurrency", "ok", metadata=metadata)
            self._apply(commit)
        return self.public_account(self._db().account_by_id(user_id))

    def _require(self, user_id: str) -> dict:
        account = self._db().account_by_id(user_id)
        if account is None or account.get("role") != "member":
            raise HarnessError(404, "no household account matches that id")
        return account

    def public_account(self, row: dict, *, include_usage: bool = True) -> dict:
        user_id = row["user_id"]
        running = self._db().count_sessions(user_id, "running")
        queued = self._db().count_sessions(user_id, "queued")
        used = account_usage_bytes(self._cfg(), user_id) if include_usage else 0
        return {
            "user_id": user_id,
            "account_hint": account_hint(user_id),
            "login": row["login"],
            "display_name": row["display_name"],
            "enabled": bool(row.get("enabled", 1)),
            "disk_quota_bytes": int(row["disk_quota_bytes"]),
            "disk_used_bytes": used,
            "max_running": int(row["max_running"]),
            "max_queued": int(row["max_queued"]),
            "running": running,
            "queued": queued,
            "last_activity_at": row.get("last_activity_at"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "google": self.m.google_signin.owner_member_view(user_id),
        }

    def list_public(self) -> list[dict]:
        return [self.public_account(row) for row in self._db().list_accounts()]

    def member_usage(self, user_id: str) -> dict:
        account = self._require(user_id) if user_id != OWNER_USER_ID else None
        used = account_usage_bytes(self._cfg(), user_id)
        limit = int(account["disk_quota_bytes"]) if account else 0
        return {
            "user_id": user_id,
            "disk_used_bytes": used,
            "disk_quota_bytes": limit,
            "running": self._db().count_sessions(user_id, "running"),
            "queued": self._db().count_sessions(user_id, "queued"),
        }


# Silence unused import in type checkers; retention is documented and applied in Database.insert_audit.
_ = AUDIT_RETENTION_DAYS
