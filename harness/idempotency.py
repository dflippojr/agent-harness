"""Idempotent App session creation (#462).

An App that lost the response to POST /api/v1/sessions retries it with the same `Idempotency-Key` header and gets the
session the first request created (200, `Idempotency-Replayed: true`) instead of a second run. v1 covers App tokens
on that one route. The App's own store keeps one row per key (migration 0058): the hash of the App id and the key, a
digest of the validated request body and the session id, written in the transaction that inserts the session, so a
crash after the commit still finds it and a failed request leaves no row. Nothing of the prompt, context or tools is
kept. A key is protected for `WINDOW_SECONDS` from the first successful create, then reusable. Under a protected key:
a different body is 409 `idempotency_conflict`; a session the App erased is 410 `idempotency_session_erased` (the
erase clears the row's session id, leaving a tombstone until expiry) and is never created again.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from .manager import HarnessError

HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotency-Replayed"
WINDOW_SECONDS = 24 * 3600
_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")
CONFLICT = "idempotency_conflict"
ERASED = "idempotency_session_erased"
clock = time.time  # tests move it


def check_key(key: str) -> str:
    if not _KEY.fullmatch(key):
        raise HarnessError(400, f"{HEADER} must be 1-128 ASCII letters, digits, hyphens or underscores",
                           "invalid_idempotency_key")
    return key


def key_hash(app_id: str, key: str) -> str:
    return hashlib.sha256(f"{app_id}\0{key}".encode()).hexdigest()


def request_digest(body: dict) -> str:
    """A digest of every creation field (end user, tools, context and metadata included), independent of key order."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def new_record(app_id: str, key: str, body: dict) -> dict:
    """The row to write with the session (`session_id` is filled in by the manager)."""
    now = clock()
    return {"key_hash": key_hash(app_id, key), "request_digest": request_digest(body), "session_id": "",
            "created_at": now, "expires_at": now + WINDOW_SECONDS}


def replayed_session(store, record: dict) -> str | None:
    """The id of the session this key already created, or None when the key is free. Raises 409 for a different
    request and 410 when that session was erased."""
    row = store.find_idempotency_key(record["key_hash"], clock())
    if row is None:
        return None
    if row["request_digest"] != record["request_digest"]:
        raise HarnessError(409, f"this {HEADER} was already used for a different request", CONFLICT)
    if not row["session_id"] or store.get_session(row["session_id"]) is None:
        raise HarnessError(410, f"the session this {HEADER} created was erased; it is not created again",
                           ERASED)
    return row["session_id"]
