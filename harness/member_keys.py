"""Household members' own provider API keys for hosted Claude Code and Codex (#393).

A member adds an Anthropic key (Claude Code) or an OpenAI key (Codex) in Agent Harness Web and their hosted sessions
run on it, billed to their own account. The owner's login, token and keys are never used for a member's session.

- **Storage:** the key is sealed with AES-GCM under a master key in `<data_dir>/member-keys.key` (created on first
  use, owner-readable only where the OS has modes). The database holds only the ciphertext and the last four
  characters, bound to (member, backend) so a row can't be moved to another member. Nightly backups copy the master
  key separately (#414); restore uses it only when its fingerprint matches the database snapshot. Nothing logs, emits or
  returns a key; the API shows the last four characters.
- **Domain (decision 3, recorded):** a member is treated like an App's end user of the Web domain (#365): the session
  carries `end_user = "member:<user_id>"` with an empty App id. That gives the member their own CLI state volume
  (`cli_domains.end_user_volume`, hashed from both ids), the same read-only config and no login volume or owner
  token, and it reuses the usage tally. This module is the pluggable credential source for it (`MemberApiKey`); a
  future `subscription` source for members registers beside it without touching storage or session wiring.
- **Session:** the key reaches the container as `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` by name; the value travels in
  the docker client's environment and never on a command line.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Callable

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import cli_domains, credential_audit, credential_sources
from .credential_sources import CredentialRefused

log = logging.getLogger("harness.member_keys")

PREFIX = cli_domains.MEMBER_PREFIX
KEY_FILE = "member-keys.key"
REQUIRED = "member_api_key_required"
BACKENDS = {
    "claude": {"provider": "Anthropic", "env": "ANTHROPIC_API_KEY"},
    "codex": {"provider": "OpenAI", "env": "OPENAI_API_KEY"},
}
_KEY_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{19,299}")


def end_user_id(user_id: str) -> str:
    """The `end_user` a member's hosted sessions carry."""
    return PREFIX + user_id


def member_of(end_user: str) -> str:
    """The member's user id when `end_user` names a member, else an empty string."""
    return end_user[len(PREFIX):] if (end_user or "").startswith(PREFIX) else ""


class MemberKeyError(Exception):
    """A member key step the member can be told about: `status` and `code` become the API error."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


def _probe(backend: str, key: str) -> tuple[bool | None, str]:
    """One cheap authenticated call (list models): (True, "") valid, (False, why) rejected, (None, why) unknown."""
    if backend == "claude":
        url, headers = "https://api.anthropic.com/v1/models?limit=1", {"x-api-key": key,
                                                                         "anthropic-version": "2023-06-01"}
    else:
        url, headers = "https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"}
    try:
        status = httpx.get(url, headers=headers, timeout=15, follow_redirects=False).status_code
    except httpx.HTTPError:
        return None, "could not reach the provider to check the key"
    if status == 200:
        return True, ""
    if status in (401, 403):
        return False, "the provider rejected this key"
    return None, f"the provider answered {status}; the key could not be checked"


class MemberKeys:
    """Encrypted per-member provider keys. `probe` exists for tests: a stub in place of the provider's API."""

    def __init__(self, cfg, db, *, probe: Callable[[str, str], tuple[bool | None, str]] = _probe):
        self.cfg = cfg
        self.db = db
        self._probe = probe
        self._aead: AESGCM | None = None
        self._guard = threading.Lock()

    # --- sealing -----------------------------------------------------------------------------------------------
    def _cipher(self) -> AESGCM:
        with self._guard:
            if self._aead is None:
                self._aead = AESGCM(self._master_key(Path(self.cfg.data_dir) / KEY_FILE))
            return self._aead

    @staticmethod
    def _read_master(path: Path) -> bytes | None:
        try:
            key = base64.b64decode(path.read_bytes().strip(), validate=True)
        except (OSError, ValueError):
            return None
        return key if len(key) == 32 else None

    def _master_key(self, path: Path) -> bytes:
        """The master key, created whole or not at all: written to a temp file and linked into place, so a crash or a
        second process never leaves a partial key. A file that is not a full key can't have sealed anything and is
        replaced."""
        key = self._read_master(path)
        if key is not None:
            return key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64encode(secrets.token_bytes(32)))
                f.flush()
                os.fsync(f.fileno())
            if path.exists():
                path.unlink()   # unreadable or short: nothing was ever sealed under it
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass            # another process won the race: use its key
        finally:
            tmp.unlink(missing_ok=True)
        key = self._read_master(path)
        if key is None:
            raise RuntimeError("the member key file could not be created")
        return key

    @staticmethod
    def _aad(user_id: str, backend: str) -> bytes:
        return f"member-api-key\0{user_id}\0{backend}".encode()

    def _seal(self, user_id: str, backend: str, key: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self._cipher().encrypt(nonce, key.encode(), self._aad(user_id, backend))

    # --- reads -------------------------------------------------------------------------------------------------
    def get(self, user_id: str, backend: str) -> str:
        """The member's key for `backend`, or "" (none stored, or unreadable: a restored backup, a new master key)."""
        row = self.db.member_api_key(user_id, backend) if backend in BACKENDS else None
        if not row:
            return ""
        blob = bytes(row["ciphertext"])
        try:
            return self._cipher().decrypt(blob[:12], blob[12:], self._aad(user_id, backend)).decode()
        except (InvalidTag, ValueError):
            log.warning("a member's %s key could not be decrypted; it must be added again", backend)
            return ""

    def has(self, user_id: str, backend: str) -> bool:
        return bool(self.get(user_id, backend))

    def status(self, user_id: str) -> dict:
        rows = {r["backend"]: r for r in self.db.member_api_keys(user_id)}
        return {"keys": [{"backend": b, "provider": meta["provider"], "env": meta["env"],
                          "configured": b in rows, "last4": rows[b]["last4"] if b in rows else "",
                          "updated_at": rows[b]["updated_at"] if b in rows else None}
                         for b, meta in BACKENDS.items()]}

    # --- writes ------------------------------------------------------------------------------------------------
    @staticmethod
    def _backend(backend: str) -> None:
        if backend not in BACKENDS:
            raise MemberKeyError(400, "unsupported_backend", "API keys are for claude (Anthropic) and codex (OpenAI)")

    def _audit(self, ctx, user_id: str, action: str, outcome: str, metadata: dict) -> None:
        """One audit row (#468): ids, the backend and booleans only; never the key, its ciphertext or last four."""
        credential_audit.record(self.db, ctx or credential_audit.member_context(user_id), action, user_id, outcome,
                                "member", metadata)

    def _write(self, fn):
        return getattr(self.db, "main", self.db).write(fn)

    def set(self, user_id: str, backend: str, key: str, ctx=None) -> dict:
        """Store or replace the member's key. Nothing about the key is echoed back but its last four characters."""
        try:
            self._backend(backend)
        except MemberKeyError:
            self._audit(ctx, user_id, "member_key.set", "denied", {"reason": "unsupported_backend"})
            raise
        key = (key or "").strip()
        if not _KEY_SHAPE.fullmatch(key):
            self._audit(ctx, user_id, "member_key.set", "denied", {"backend": backend, "reason": "invalid_key"})
            raise MemberKeyError(400, "invalid_key",
                                 f"that does not look like an {BACKENDS[backend]['provider']} API key")
        sealed = self._seal(user_id, backend, key)  # encrypted first; the stored change and its audit commit together

        def commit() -> None:
            replaced = self.db.member_api_key(user_id, backend) is not None
            self.db.set_member_api_key(user_id, backend, sealed, key[-4:])
            self._audit(ctx, user_id, "member_key.set", "ok",
                        {"backend": backend, "configured": True, "replaced": replaced})
        self._write(commit)
        return self.status(user_id)

    def delete(self, user_id: str, backend: str, ctx=None) -> dict:
        try:
            self._backend(backend)
        except MemberKeyError:
            self._audit(ctx, user_id, "member_key.delete", "denied", {"reason": "unsupported_backend"})
            raise

        def commit() -> None:
            removed = self.db.delete_member_api_keys(user_id, backend)
            self._audit(ctx, user_id, "member_key.delete", "ok" if removed else "noop",
                        {"backend": backend, "configured": False})
        self._write(commit)
        return self.status(user_id)

    def purge(self, user_id: str) -> int:
        """Delete every key of the member (the account is removed or cut off)."""
        return self.db.delete_member_api_keys(user_id)

    def test(self, user_id: str, backend: str, ctx=None) -> dict:
        """One cheap validation call with the stored key."""
        try:
            self._backend(backend)
        except MemberKeyError:
            self._audit(ctx, user_id, "member_key.test", "denied", {"reason": "unsupported_backend"})
            raise
        key = self.get(user_id, backend)
        if not key:
            self._audit(ctx, user_id, "member_key.test", "noop",
                        {"backend": backend, "configured": False, "result": "noop"})
            raise MemberKeyError(409, REQUIRED, f"add your {BACKENDS[backend]['provider']} API key first")
        ok, why = self._probe(backend, key)
        result = "ok" if ok is True else "rejected" if ok is False else "unavailable"
        self._audit(ctx, user_id, "member_key.test", result,
                    {"backend": backend, "configured": True, "result": result})
        return {"backend": backend, "ok": ok is True, "checked": ok is not None,
                "message": "The key works." if ok else why}


class MemberApiKey(credential_sources.CredentialSource):
    """The credential source for a member's hosted session: their own API key and their own volume, never another
    credential. A member without a key is refused (`member_api_key_required`)."""
    name = "member_api_key"
    label = "member_api_key"
    store: MemberKeys | None = None   # set by the Manager

    def selects(self, app_id: str, end_user: str) -> bool:
        return not app_id and bool(member_of(end_user))

    def docker_args(self, backend: str, cfg, app_id: str, end_user: str) -> list[str]:
        return cli_domains.docker_args(backend, cfg, "", end_user=end_user)

    async def ready(self, backend: str, cfg, app_id: str, end_user: str) -> None:
        if backend not in BACKENDS:
            raise CredentialRefused("member_backend_unsupported", f"{backend} can't run on a member's API key")
        store = self.store
        if store is None or not await asyncio.to_thread(store.has, member_of(end_user), backend):
            raise CredentialRefused(REQUIRED, f"add your {BACKENDS[backend]['provider']} API key in your settings "
                                              f"to use {backend.title()}")
        try:
            await cli_domains.prepare(backend, cfg, "", end_user)
        except RuntimeError as e:
            raise CredentialRefused("member_unavailable", str(e)) from e


SOURCE = credential_sources.register(MemberApiKey())
