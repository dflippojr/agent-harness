"""Exclusive Hub claim (#543): the standalone Hub pairs by itself, then administers this daemon alone.

1. **Claim request.** The Hub asks with the #519 request model (`POST /api/v1/pair/requests` with `kind: "hub"`, a PKCE
   challenge, the same match code and TTLs). While a Hub is recorded the request is refused at once with 409.
2. **Host-only approval.** The owner approves (`harness hub approve <rid> --match <code>`) or denies it on the daemon
   host. The daemon writes a fresh random secret to `<data_dir>/hub-approval.secret` (owner-only) at every start;
   the CLI reads it and sends it in the `X-Agent-Harness-Hub-Approval` header to the `/hub-claim` routes, which also
   require owner auth. Holding an owner token, a Web session, an App key or the Hub's own token is not enough, and
   neither is reaching the daemon on loopback. A missing or wrong secret is refused with 403 and audited.
3. **Redeem.** The Hub fetches its token once with its verifier: an owner-kind key with the admin scope and role
   `hub`, recorded as the one Hub (`hub_claim`). It can call every owner API route except these three host routes, and
   it can never mint another Hub key (only a claim does).
4. **Release.** `harness hub release --confirm` (same host proof) revokes the Hub key and clears the record, so a new
   Hub may claim. `DELETE /keys/{kid}` on the Hub key is refused and names that command.

The secret is never logged, audited or returned by any route; audit rows carry ids and names only.
"""
from __future__ import annotations

import hmac

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import audit_context, credential_audit
from .local_owner import HUB_HEADER as HEADER
from .manager import HarnessError

ROLE = "hub"
RELEASE_COMMAND = "harness hub release --confirm"
HOST_ONLY = ("approve, deny and release of a Hub claim run on the daemon host: use `harness hub approve`, "
             "`harness hub deny` or `harness hub release --confirm` there")


def is_hub_key(key: dict | None) -> bool:
    return bool(key and key.get("role") == ROLE)


def record_view(row: dict | None) -> dict | None:
    """The Hub record as anyone with owner access sees it: ids, names and times only."""
    if row is None:
        return None
    return {"key_id": row["key_id"], "name": row["name"], "kind": row["kind"], "origin": row["origin"],
            "request_id": row["request_id"], "claimed_at": row["claimed_at"]}


class ApproveHubClaim(BaseModel):
    match: str = Field(max_length=32, description="The match code the Hub shows")


class ReleaseHub(BaseModel):
    confirm: bool = False


def register_admin(app: FastAPI, mgr, require_admin) -> list[dict]:
    """The owner side (/api/admin/v1/hub-claim): status, and the host-only approve, deny and release."""
    from . import pairing_requests as pr
    from .admin import PREFIX
    base = PREFIX + "/hub-claim"

    async def host_proof(request: Request, action: str) -> audit_context.AuditContext:
        """Owner auth, not the Hub's own key, and the host-only secret; or 403 with a `denied` audit row (which
        stores nothing the caller sent)."""
        key = require_admin(request, mgr)
        m = mgr(request)
        ctx = audit_context.owner_context(key)
        reason = ""
        if is_hub_key(key):
            reason = "hub_key"
        else:
            expected = getattr(m, "hub_approval_secret", "")
            sent = request.headers.get(HEADER, "")
            if not (expected and sent and hmac.compare_digest(sent.encode(), expected.encode())):
                reason = "host_proof_required"
        if reason:
            await m.db.main.awrite(credential_audit.record, m.db, ctx, action, "", "denied", "hub_claim",
                                   {"reason": reason})
            raise HarnessError(403, "the Hub cannot approve, deny or release a Hub claim" if reason == "hub_key"
                               else HOST_ONLY, code=reason)
        return ctx

    async def decide(request: Request, rid: str, action: str, states: tuple[str, ...], check, fields):
        ctx = await host_proof(request, action)
        m = mgr(request)

        def commit():
            db, now = m.db.main, pr.clock()
            pr.sweep(db, now)
            row = db.get_pairing_request(rid)
            refusal: tuple[int, str, str] | None = None
            if row is None or row["kind"] != ROLE:
                refusal = (404, "no Hub claim request has that id", "not_found")
            elif row["state"] not in states:
                refusal = (409, f"this Hub claim request is {row['state']}", "not_approvable")
            else:
                refusal = check(db, row)
            if refusal:
                known = row is not None and row["kind"] == ROLE
                outcome = "noop" if refusal[0] in (404, 409) else "denied"
                credential_audit.record(db, ctx, action, rid if known else "", outcome, "pairing_request",
                                        {"request_id": rid, "reason": refusal[2]} if known else
                                        {"reason": "not_found"})
                return pr._refuse(*refusal)
            if not db.update_pairing_request(rid, states, **fields(now)):
                return pr._refuse(409, "this Hub claim request changed; check `harness hub status`", "not_approvable")
            credential_audit.record(db, ctx, action, rid, "ok", "pairing_request", {"request_id": rid})
            return {"row": db.get_pairing_request(rid)}
        out = await m.db.main.awrite(commit)
        pr._raise_refusal(out)
        return JSONResponse(pr.owner_view(out["row"], m.cfg), headers=pr.NO_STORE)

    @app.get(base)
    async def hub_claim_status(request: Request):
        """Whether a Hub is claimed, its record (never a token or hash), and Hub claim requests still open."""
        require_admin(request, mgr)
        m = mgr(request)

        def commit():
            db = m.db.main
            pr.sweep(db, pr.clock())
            return db.hub_claim(), [r for r in db.list_pairing_requests() if r["kind"] == ROLE]
        record, requests = await m.db.main.awrite(commit)
        return JSONResponse({"claimed": record is not None, "hub": record_view(record),
                             "requests": [pr.owner_view(r, m.cfg) for r in requests]}, headers=pr.NO_STORE)

    @app.post(base + "/requests/{rid}/approve")
    async def approve_hub_claim(rid: str, body: ApproveHubClaim, request: Request):
        """Approve a Hub claim with the match code the Hub shows. Host only: needs the approval secret header."""
        def check(db, row):
            if db.hub_claim() is not None:
                return 409, "a Hub is already claimed; release it first with `harness hub release --confirm`", \
                    "hub_claimed"
            if not pr._same_match(row, body.match):
                return 400, "the match code does not match the one the Hub shows", "match_mismatch"
            return None
        return await decide(request, rid, "hub.claim.approve", (pr.PENDING,), check,
                            lambda now: {"state": pr.APPROVED, "approved_at": now,
                                         "expires_at": now + pr.REDEEM_TTL_SECONDS})

    @app.post(base + "/requests/{rid}/deny")
    async def deny_hub_claim(rid: str, request: Request):
        """Deny a Hub claim request, or withdraw an approval the Hub has not redeemed yet. Host only."""
        return await decide(request, rid, "hub.claim.deny", pr.ACTIVE, lambda _db, _row: None,
                            lambda now: {"state": pr.DENIED, "finished_at": now})

    @app.post(base + "/release")
    async def release_hub(body: ReleaseHub, request: Request):
        """Revoke the Hub's key and clear the record so a new Hub may claim. Host only; needs confirm."""
        ctx = await host_proof(request, "hub.release")
        m = mgr(request)
        if not body.confirm:
            await m.db.main.awrite(credential_audit.record, m.db, ctx, "hub.release", "", "denied", "hub_claim",
                                   {"reason": "invalid_request"})
            raise HarnessError(400, "confirm the release: it revokes the Hub's key at once")

        def commit():
            db = m.db.main
            record = db.release_hub(pr.clock())
            if record is None:
                credential_audit.record(db, ctx, "hub.release", "", "noop", "hub_claim", {"reason": "no_hub"})
                return None
            credential_audit.record(db, ctx, "hub.release", record["key_id"], "ok", "hub_claim",
                                    {"key_id": record["key_id"], "request_id": record["request_id"]})
            return record
        record = await m.db.main.awrite(commit)
        if record is None:
            raise HarnessError(404, "no Hub is claimed")
        return JSONResponse({"claimed": False, "released": record_view(record)}, headers=pr.NO_STORE)

    return [{"method": "GET", "path": base}, {"method": "POST", "path": base + "/requests/{rid}/approve"},
            {"method": "POST", "path": base + "/requests/{rid}/deny"}, {"method": "POST", "path": base + "/release"}]
