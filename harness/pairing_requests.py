"""Zero-touch App pairing (#519): the daemon hands an App its `ha-` token directly, so the owner never sees one.

One request model for browser and native Apps (docs/app-api.md#zero-touch-pairing):

1. **App-initiated.** The App asks with `POST /api/v1/pair/requests` (public, rate-limited): name, scopes, optional
   `catalog_app_id` and a PKCE S256 `code_challenge`. It gets a request id and a short match code to show.
2. **Owner approval.** The owner sees the request (`GET /api/admin/v1/pairing-requests`, `harness pairing-requests
   list`) with each scope's disclosure copy and approves it with the match code, acknowledging elevated scopes
   separately, or denies it. Approval is a state change only: no secret exists yet.
3. **Direct delivery.** The App redeems with `POST /api/v1/pair/requests/{id}/token` and its `code_verifier`; the key
   is minted then and returned once. A browser request redeems only from its exact origin, a native one never from a
   browser.
4. **Hub-initiated.** The owner arms a pre-approved slot (`POST /api/admin/v1/pairing-requests`) for a catalog app id,
   scopes and an optional origin, and hands the App only the slot's id. The App claims it with its challenge
   (`POST /api/v1/pair/requests/{id}/claim`). A browser claim must come from the armed origin and is approved at
   once; a native claim gets a match code the owner confirms (`/confirm`) before the token is released.

A Hub claim (#543, harness/hub_claim.py) is the same request with `kind: "hub"`: it asks for no scopes, is refused
while a Hub is recorded, is approved or denied only on the daemon host, and redeems to the one `hub` key.

A request has 10 minutes to be approved (or claimed, or confirmed) and 5 minutes after approval to be redeemed. A
redeemed, denied or expired request is never reused. Rows keep the hash of the challenge, never a token or the
verifier, and owner responses carry request metadata only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import audit_context, catalog_ids, credential_audit
from .manager import HarnessError

PENDING, ARMED, CLAIMED, APPROVED = "pending", "armed", "claimed", "approved"
DENIED, REDEEMED, EXPIRED = "denied", "redeemed", "expired"
ACTIVE = (PENDING, ARMED, CLAIMED, APPROVED)
APPROVE_TTL_SECONDS = 10 * 60   # to approve, claim or confirm (decision 3)
REDEEM_TTL_SECONDS = 5 * 60     # to fetch the token once approved
KEEP_SECONDS = 24 * 3600        # finished requests stay listed this long
# One caller (the tailnet login tailscaled vouched for, else the connection's address) may ask this often and keep this
# many requests waiting, whatever Origin it sends; past the global cap nobody can add another until the owner decides or
# they expire.
SOURCE_RATE = (10, 10 * 60)
SOURCE_PENDING_CAP = 3
PENDING_CAP = 50

CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)       # base64url(sha256(verifier)), unpadded
VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}", re.ASCII)   # RFC 7636 section 4.1

# docs/marketplace-design.md section 5.2: the owner sees this text for every requested scope, in every client.
ELEVATED = frozenset({"sessions:all", "approvals", "remote_control", "memory_library", "homelab"})
DISCLOSURES = {
    "sessions": "This app can start agent tasks on your harness and read the tasks it created. It cannot see other "
                "apps' or your Control Center sessions.",
    "sessions:all": "This app can **read every session on this harness**, including work you started yourself and "
                    "work other apps started. It cannot create sessions unless it also has `sessions`.",
    "approvals": "This app can **approve or deny tool calls** in its own tasks, including shell and file changes the "
                 "agent requests. That is a substitute for you tapping Approve.",
    "images": "This app can generate images on your harness. Image jobs pause language-model work for a few minutes.",
    "inference": "This app can call your local model as a raw completion API, outside an agent session. That still "
                 "uses the same GPU.",
    "remote_control": "This app can **start Claude Remote Control** in a project folder. Those sessions run as native "
                      "Claude Code on your machine: no harness queue, sandbox, or harness approvals.",
    "models:warm": "This app can **load your local model** when you start chatting in it. Loading takes RAM and the "
                   "GPU for about a minute.",
    "memory_library": "This app's tasks can **read your memory library** and propose changes to it. You still approve "
                      "every change.",
    "homelab": "This app's tasks can **read your homelab services' logs, config and metrics** and ask to restart or "
               "rebuild them.",
    # A Hub claim's one scope (#543): never an App's.
    "admin": "This is a **Hub claim**: the app becomes this harness's one admin console, with every owner power except "
             "approving, denying or releasing a Hub. Approve it only on the daemon host with `harness hub approve`.",
}
HUB = "hub"

# Read on every decision, so a test can move time forward.
clock = time.time


class CreatePairingRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kind: str = Field(default="app", description='"app", or "hub" for an exclusive Hub claim (#543); a Hub claim '
                                                 'takes no scopes')
    scopes: list[str] = Field(default_factory=lambda: ["sessions"])
    catalog_app_id: str = Field(default="", description="Optional catalog app id (a label, never part of a token)")
    code_challenge: str = Field(description="base64url(sha256(code_verifier)), unpadded (PKCE S256)")
    code_challenge_method: str = "S256"


class ClaimPairingRequest(BaseModel):
    code_challenge: str = Field(description="base64url(sha256(code_verifier)), unpadded (PKCE S256)")
    code_challenge_method: str = "S256"


class PairingRequestResponse(BaseModel):
    """What the App learns: never the owner's view, never a secret."""
    id: str
    state: str
    match_code: str = ""
    expires_at: float
    browser: bool


class ArmPairingRequest(BaseModel):
    catalog_app_id: str
    scopes: list[str] = Field(default_factory=lambda: ["sessions"])
    origin: str = Field(default="", max_length=500, description="The browser App's exact origin; empty for native")
    name: str = Field(default="", max_length=60)
    acknowledge_elevated: bool = False


class ApprovePairingRequest(BaseModel):
    match: str = Field(max_length=32, description="The match code the App shows")
    acknowledge_elevated: bool = False


class ConfirmPairingRequest(BaseModel):
    match: str = Field(max_length=32, description="The match code the App shows")


def disclosures(scopes: list[str], cfg=None) -> list[dict]:
    from .apps import all_scopes
    known = all_scopes(cfg)
    return [{"scope": s, "tier": "elevated" if s in ELEVATED or s == "admin" else "standard",
             "text": DISCLOSURES.get(s) or known.get(s, "")} for s in scopes]


def challenge_of(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _match_code() -> str:
    return f"{secrets.randbelow(10 ** 6):06d}"


def _same_match(row: dict, given: str) -> bool:
    given = re.sub(r"[\s-]", "", given or "")
    return bool(row["match_code"]) and hmac.compare_digest(given.encode(), row["match_code"].encode())


def owner_view(row: dict, cfg=None) -> dict:
    """The request as the owner (Hub, CLI) sees it: metadata only, never the challenge hash or who asked."""
    scopes = row["scopes"].split()
    state = row["state"]
    return {"id": row["id"], "kind": row["kind"], "name": row["name"], "scopes": scopes,
            "catalog_app_id": row["catalog_app_id"] or "", "origin": row["origin"], "browser": bool(row["origin"]),
            "armed": bool(row["armed"]), "state": state,
            # A Hub claim's code is never shown to the owner: they must read it off the real Hub's screen, or an
            # attacker's look-alike request could be approved by copying the code from `harness hub status`.
            "match_code": row["match_code"] if state in (PENDING, CLAIMED) and row["kind"] != HUB else "",
            "needs": {PENDING: "approve", ARMED: "claim", CLAIMED: "confirm", APPROVED: "redeem"}.get(state, ""),
            "created_at": row["created_at"], "expires_at": row["expires_at"], "approved_at": row["approved_at"],
            "finished_at": row["finished_at"], "key_id": row["key_id"],
            "elevated": [s for s in scopes if s in ELEVATED or s == "admin"], "disclosures": disclosures(scopes, cfg)}


def app_view(row: dict) -> dict:
    state = row["state"]
    return {"id": row["id"], "state": state, "match_code": row["match_code"] if state in (PENDING, CLAIMED) else "",
            "expires_at": row["expires_at"], "browser": bool(row["origin"])}


def sweep(db, now: float) -> None:
    """Expire what ran out of time, one `pairing_request.expire` row each. Runs inside the caller's write."""
    for row in db.expire_pairing_requests(now, ACTIVE, KEEP_SECONDS):
        action = "hub.claim.expire" if row["kind"] == HUB else "pairing_request.expire"
        credential_audit.record(db, audit_context.SYSTEM, action, row["id"], "ok",
                                "pairing_request", {"request_id": row["id"], "reason": "expired"})


def _scopes(values: list[str], cfg) -> str:
    """The requested scopes as stored, or ValueError. `admin` is the owner's and never an App's."""
    from .apps import all_scopes
    if "admin" in values:
        raise ValueError("admin is an owner scope; Apps cannot request it")
    known = all_scopes(cfg)
    if not values or any(s not in known for s in values):
        raise ValueError(f"unknown or empty scopes; known: {', '.join(known)}")
    return " ".join(dict.fromkeys(values))


def _challenge(value: str, method: str) -> str:
    if method != "S256":
        raise ValueError("code_challenge_method must be S256")
    if not CHALLENGE.fullmatch(value or ""):
        raise ValueError("code_challenge must be the unpadded base64url SHA-256 of the code_verifier (43 characters)")
    return _digest(value)


def _request_origin(request: Request) -> str:
    """The caller's normalized Origin, "" for a native caller, or 403 for something that isn't an origin."""
    from .apps import normalize_origin
    raw = request.headers.get("origin", "")
    if not raw:
        return ""
    try:
        return normalize_origin(raw)
    except ValueError as e:
        raise HarnessError(403, str(e))


def _caller(request: Request) -> str:
    """Who asked, hashed: the tailnet login tailscaled vouched for, else the connection's address. Never the Origin,
    which any native client can set to anything, so one caller cannot spend another's allowance by naming its App."""
    login = request.headers.get("tailscale-user-login")
    raw = "login:" + login if login else "addr:" + (request.client.host if request.client else "")
    return _digest(raw)


NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _refuse(status: int, detail: str, code: str = "") -> dict:
    return {"error": (status, detail, code)}


def _raise_refusal(out: dict) -> None:
    if "error" in out:
        status, detail, code = out["error"]
        raise HarnessError(status, detail, code=code)


def register_app(app: FastAPI) -> None:
    """The public App routes. They take no token: the owner's approval and the PKCE verifier are the checks."""
    from .api import mgr
    from .apps import API_VERSION, PairResponse

    @app.post("/api/v1/pair/requests", status_code=201, response_model=PairingRequestResponse)
    async def create_pairing_request(body: CreatePairingRequest, request: Request):
        """Ask the owner to pair this App (#519). Returns the request id and a match code to show the user."""
        m = mgr(request)
        origin = _request_origin(request)
        source = _caller(request)
        unknown = credential_audit.unknown_context()
        hub = body.kind == HUB
        action = "hub.claim.request" if hub else "pairing_request.create"
        try:
            name = body.name.strip()
            if not name:
                raise ValueError("name is required")
            if body.kind not in ("app", HUB):
                raise ValueError('kind must be "app" or "hub"')
            if hub and "scopes" in body.model_fields_set:
                raise ValueError("a Hub claim takes no scopes: the Hub key's powers are fixed")
            scopes = "admin" if hub else _scopes(body.scopes, m.cfg)
            catalog_app_id = catalog_ids.normalize(body.catalog_app_id)
            challenge = _challenge(body.code_challenge, body.code_challenge_method)
        except ValueError as e:
            await m.db.main.awrite(credential_audit.record, m.db, unknown, action, "", "denied",
                                   "pairing_request", {"reason": "invalid_request"})
            raise HarnessError(400, str(e))

        def commit():
            db, now = m.db.main, clock()
            sweep(db, now)
            if hub and db.hub_claim() is not None:  # exclusive (#543): no API path replaces the Hub
                credential_audit.record(db, unknown, "hub.claim.refused", "", "denied", "pairing_request",
                                        {"reason": "hub_claimed", "browser": bool(origin)})
                return _refuse(409, "this harness already has a Hub; the owner releases it on the daemon host with "
                                    "`harness hub release --confirm`", "hub_claimed")
            recent, mine, total = db.pairing_request_load(source, now - SOURCE_RATE[1], (PENDING,))
            if recent >= SOURCE_RATE[0] or mine >= SOURCE_PENDING_CAP or total >= PENDING_CAP:
                reason = "rate_limited" if recent >= SOURCE_RATE[0] else "too_many_pending"
                credential_audit.record(db, unknown, action, "", "denied", "pairing_request", {"reason": reason})
                return _refuse(429, "too many pairing requests; wait for the owner or try again later", reason)
            row = {"id": "pr-" + secrets.token_hex(12), "kind": body.kind, "name": name, "scopes": scopes,
                   "catalog_app_id": catalog_app_id, "origin": origin, "source": source, "challenge_hash": challenge,
                   "match_code": _match_code(), "state": PENDING, "armed": 0, "created_at": now,
                   "expires_at": now + APPROVE_TTL_SECONDS, "approved_at": None, "finished_at": None, "key_id": ""}
            db.insert_pairing_request(row)
            credential_audit.record(db, unknown, action, row["id"], "ok", "pairing_request",
                                    {"request_id": row["id"], "scopes": scopes.split(), "catalog_app_id": catalog_app_id,
                                     "browser": bool(origin), "armed": False})
            return {"row": row}
        out = await m.db.main.awrite(commit)
        _raise_refusal(out)
        return JSONResponse(app_view(out["row"]), status_code=201, headers=NO_STORE)

    @app.post("/api/v1/pair/requests/{rid}/claim", response_model=PairingRequestResponse)
    async def claim_pairing_request(rid: str, body: ClaimPairingRequest, request: Request):
        """Attach this App's code_challenge to a slot the owner armed from the Hub (#519)."""
        m = mgr(request)
        origin = _request_origin(request)
        unknown = credential_audit.unknown_context()
        try:
            challenge = _challenge(body.code_challenge, body.code_challenge_method)
        except ValueError as e:
            raise HarnessError(400, str(e))

        def commit():
            db, now = m.db.main, clock()
            sweep(db, now)
            row = db.get_pairing_request(rid)
            refusal = None
            if row is None:
                refusal = (404, "no pairing request has that id", "not_found")
            elif row["state"] != ARMED:
                refusal = (409, "this pairing request cannot be claimed", "not_claimable")
            elif row["origin"] != origin:
                refusal = (403, "this pairing request is not approved for this origin", "origin_mismatch")
            if refusal:
                credential_audit.record(db, unknown, "pairing_request.claim", "", "denied", "pairing_request",
                                        {"reason": refusal[2]})
                return _refuse(*refusal)
            if origin:  # the exact-origin binding is the check: approved at once (decision 2)
                fields = {"state": APPROVED, "approved_at": now, "expires_at": now + REDEEM_TTL_SECONDS}
            else:       # a native claim waits for the owner to confirm its match code
                fields = {"state": CLAIMED, "match_code": _match_code(), "expires_at": now + APPROVE_TTL_SECONDS}
            if not db.update_pairing_request(rid, (ARMED,), challenge_hash=challenge, **fields):
                return _refuse(409, "this pairing request cannot be claimed", "not_claimable")
            credential_audit.record(db, unknown, "pairing_request.claim", rid, "ok", "pairing_request",
                                    {"request_id": rid, "browser": bool(origin)})
            return {"row": db.get_pairing_request(rid)}
        out = await m.db.main.awrite(commit)
        _raise_refusal(out)
        return JSONResponse(app_view(out["row"]), headers=NO_STORE)

    @app.post("/api/v1/pair/requests/{rid}/token", status_code=201, response_model=PairResponse, openapi_extra={
        "requestBody": {"required": True, "content": {"application/json": {"schema": {
            "type": "object", "required": ["code_verifier"],
            "properties": {"code_verifier": {"type": "string", "minLength": 43, "maxLength": 128}}}}}}})
    async def redeem_pairing_request(rid: str, request: Request):
        """Fetch the App's token once the owner approved (#519): minted now, returned once, to the verifier's holder.
        Until then it answers 409 `pairing_pending`. The body is read by hand, so a validation error can never echo
        the verifier back."""
        m = mgr(request)
        origin = _request_origin(request)
        try:
            body = await request.json()
        except ValueError:
            body = None
        verifier = body.get("code_verifier") if isinstance(body, dict) else None
        verifier = verifier if isinstance(verifier, str) and VERIFIER.fullmatch(verifier) else ""

        def commit():
            db, now = m.db.main, clock()
            sweep(db, now)
            row = db.get_pairing_request(rid)
            hub = row is not None and row["kind"] == HUB
            refusal = _redeem_refusal(row, origin, verifier)
            if refusal:
                status, detail, reason = refusal
                if status != 409:  # waiting is not a refusal
                    credential_audit.record(db, credential_audit.unknown_context(),
                                            "hub.claim.redeem" if hub else "pairing_request.redeem", "",
                                            "denied", "pairing_request", {"reason": reason})
                return _refuse(status, detail, "pairing_pending" if status == 409 else "")
            if hub:
                return _redeem_hub(db, row, now)
            key, secret = db.redeem_pairing_request(rid, now)
            if key is None:
                return _refuse(400, "this pairing request was already redeemed")
            credential_audit.record(db, credential_audit.device_context(key["id"], "app"), "pairing_request.redeem",
                                    key["id"], "ok", "api_key",
                                    {"key_id": key["id"], "request_id": rid, "kind": "app",
                                     "scopes": key["scopes"].split(), "catalog_app_id": key["catalog_app_id"],
                                     "browser": bool(row["origin"])})
            return {"key": key, "secret": secret}
        out = await m.db.main.awrite(commit)
        _raise_refusal(out)
        return JSONResponse({"token": out["secret"], "app": out["key"], "api_version": API_VERSION},
                            status_code=201, headers=NO_STORE)


def _redeem_hub(db, row: dict, now: float) -> dict:
    """Mint the one Hub key (#543), or refuse when a Hub was recorded since this claim was approved."""
    key, secret, error = db.redeem_hub_claim(row["id"], now)
    if error == "claimed":
        db.update_pairing_request(row["id"], (APPROVED,), state=DENIED, finished_at=now)
        credential_audit.record(db, credential_audit.unknown_context(), "hub.claim.refused", row["id"], "denied",
                                "pairing_request", {"request_id": row["id"], "reason": "hub_claimed"})
        return _refuse(409, "this harness already has a Hub", "hub_claimed")
    if key is None:
        return _refuse(400, "this pairing request was already redeemed")
    ctx = audit_context.AuditContext(key["id"], HUB, key["id"], "app_api")
    credential_audit.record(db, ctx, "hub.claim.redeem", key["id"], "ok", "api_key",
                            {"key_id": key["id"], "request_id": row["id"], "catalog_app_id": key["catalog_app_id"],
                             "browser": bool(row["origin"])})
    return {"key": key, "secret": secret}


def _redeem_refusal(row: dict | None, origin: str, verifier: str) -> tuple[int, str, str] | None:
    """Why this redeem mints nothing, as (status, detail, audit reason); None when it may mint. Only the verifier's
    holder learns whether the owner has decided yet."""
    if row is None:
        return 404, "no pairing request has that id", "not_found"
    state = row["state"]
    if state in (REDEEMED, EXPIRED, DENIED):
        return {REDEEMED: (400, "this pairing request was already redeemed", "request_used"),
                EXPIRED: (400, "this pairing request expired", "request_expired"),
                DENIED: (403, "the owner denied this pairing request", "request_denied")}[state]
    if not row["challenge_hash"]:
        return 400, "claim this pairing request before redeeming it", "not_claimable"
    if row["origin"] and origin != row["origin"]:
        return 403, "this pairing request is not approved for this origin", "origin_mismatch"
    if not row["origin"] and origin:
        return 403, "a native App's pairing request cannot be redeemed from a browser", "native_only"
    if not verifier or not hmac.compare_digest(_digest(challenge_of(verifier)), row["challenge_hash"]):
        return 400, "invalid code_verifier", "invalid_verifier"
    if state != APPROVED:
        return 409, "waiting for the owner to approve this pairing request", ""
    return None


def register_admin(app: FastAPI, mgr, require_admin) -> list[dict]:
    """The owner side (/api/admin/v1/pairing-requests): list, arm, approve, confirm, deny. Returns the operations
    for discovery."""
    from .admin import PREFIX
    base = PREFIX + "/pairing-requests"

    async def decide(request: Request, key, rid: str, action: str, states: tuple[str, ...], check, fields, meta):
        """Run one owner decision on request `rid` in one write: sweep, check, compare-and-set, audit."""
        m = mgr(request)
        ctx = audit_context.owner_context(key)

        def commit():
            db, now = m.db.main, clock()
            sweep(db, now)
            row = db.get_pairing_request(rid)
            refusal = None
            if row is None:
                refusal = (404, "no pairing request has that id", "not_found")
            elif row["kind"] == HUB:  # a Hub claim is decided on the daemon host only (#543)
                from .hub_claim import HOST_ONLY
                refusal = (403, HOST_ONLY, "host_proof_required")
            elif row["state"] not in states:
                refusal = (409, f"this pairing request is {row['state']}", "not_approvable")
            else:
                refusal = check(row)
            if refusal:
                outcome = "noop" if refusal[0] in (404, 409) else "denied"
                credential_audit.record(db, ctx, action, rid if row else "", outcome, "pairing_request",
                                        {"request_id": rid, "reason": refusal[2]} if row else {"reason": "not_found"})
                return _refuse(*refusal)
            if not db.update_pairing_request(rid, states, **fields(now)):
                return _refuse(409, "this pairing request changed; list it again", "not_approvable")
            credential_audit.record(db, ctx, action, rid, "ok", "pairing_request",
                                    {"request_id": rid, **meta(row)})
            return {"row": db.get_pairing_request(rid)}
        out = await m.db.main.awrite(commit)
        _raise_refusal(out)
        return JSONResponse(owner_view(out["row"], m.cfg), headers=NO_STORE)

    def approved(now: float) -> dict:
        return {"state": APPROVED, "approved_at": now, "expires_at": now + REDEEM_TTL_SECONDS}

    def _elevated_unacknowledged(scopes: list[str], acknowledged: bool) -> bool:
        return bool(ELEVATED.intersection(scopes)) and not acknowledged

    @app.get(base)
    async def list_pairing_requests(request: Request):
        """Owner view of pairing requests: metadata, match codes and disclosure copy; never a token or verifier."""
        require_admin(request, mgr)
        m = mgr(request)

        def commit():
            sweep(m.db.main, clock())
            return m.db.main.list_pairing_requests()
        rows = await m.db.main.awrite(commit)
        return JSONResponse([owner_view(r, m.cfg) for r in rows], headers=NO_STORE)

    @app.post(base, status_code=201)
    async def arm_pairing_request(body: ArmPairingRequest, request: Request):
        """Arm a pre-approved slot for a Hub entry (#519): no secret; the App claims it with its own challenge."""
        from .apps import normalize_origin
        key = require_admin(request, mgr)
        m = mgr(request)
        ctx = audit_context.owner_context(key)
        try:
            catalog_app_id = catalog_ids.normalize(body.catalog_app_id)
            if not catalog_app_id:
                raise ValueError("catalog_app_id is required")
            scopes = _scopes(body.scopes, m.cfg)
            origin = normalize_origin(body.origin) if body.origin.strip() else ""
            if _elevated_unacknowledged(scopes.split(), body.acknowledge_elevated):
                raise ValueError("elevated scopes need acknowledge_elevated")
        except ValueError as e:
            reason = "acknowledgement_required" if "acknowledge" in str(e) else "invalid_request"
            await m.db.main.awrite(credential_audit.record, m.db, ctx, "pairing_request.create", "", "denied",
                                   "pairing_request", {"reason": reason})
            raise HarnessError(400, str(e))

        def commit():
            db, now = m.db.main, clock()
            sweep(db, now)
            row = {"id": "pr-" + secrets.token_hex(12), "kind": "app", "name": body.name.strip() or catalog_app_id,
                   "scopes": scopes, "catalog_app_id": catalog_app_id, "origin": origin, "source": "",
                   "challenge_hash": "", "match_code": "", "state": ARMED, "armed": 1, "created_at": now,
                   "expires_at": now + APPROVE_TTL_SECONDS, "approved_at": now, "finished_at": None, "key_id": ""}
            db.insert_pairing_request(row)
            credential_audit.record(db, ctx, "pairing_request.create", row["id"], "ok", "pairing_request",
                                    {"request_id": row["id"], "scopes": scopes.split(), "catalog_app_id": catalog_app_id,
                                     "browser": bool(origin), "armed": True,
                                     "acknowledged": bool(ELEVATED.intersection(scopes.split()))})
            return row
        row = await m.db.main.awrite(commit)
        return JSONResponse(owner_view(row, m.cfg), status_code=201, headers=NO_STORE)

    @app.post(base + "/{rid}/approve")
    async def approve_pairing_request(rid: str, body: ApprovePairingRequest, request: Request):
        """Approve an App's pairing request with the match code it shows. Elevated scopes need acknowledge_elevated.
        No token exists until the App redeems."""
        key = require_admin(request, mgr)

        def check(row):
            if _elevated_unacknowledged(row["scopes"].split(), body.acknowledge_elevated):
                return 400, "elevated scopes need acknowledge_elevated", "acknowledgement_required"
            if not _same_match(row, body.match):
                return 400, "the match code does not match the one the App shows", "match_mismatch"
            return None
        return await decide(request, key, rid, "pairing_request.approve", (PENDING,), check,
                            approved,
                            lambda row: {"scopes": row["scopes"].split(),
                                         "acknowledged": bool(ELEVATED.intersection(row["scopes"].split()))})

    @app.post(base + "/{rid}/confirm")
    async def confirm_pairing_request(rid: str, body: ConfirmPairingRequest, request: Request):
        """Confirm a native App's claim on an armed slot with the match code it shows; then it may redeem."""
        key = require_admin(request, mgr)

        def check(row):
            if not _same_match(row, body.match):
                return 400, "the match code does not match the one the App shows", "match_mismatch"
            return None
        return await decide(request, key, rid, "pairing_request.approve", (CLAIMED,), check,
                            approved,
                            lambda row: {"scopes": row["scopes"].split(), "confirmed": True})

    @app.post(base + "/{rid}/deny")
    async def deny_pairing_request(rid: str, request: Request):
        """Deny a pairing request, or withdraw an armed slot or an approval the App has not redeemed yet."""
        key = require_admin(request, mgr)
        return await decide(request, key, rid, "pairing_request.deny", ACTIVE, lambda _row: None,
                            lambda now: {"state": DENIED, "finished_at": now}, lambda _row: {})

    return [{"method": "GET", "path": base}, {"method": "POST", "path": base},
            {"method": "POST", "path": base + "/{rid}/approve"}, {"method": "POST", "path": base + "/{rid}/confirm"},
            {"method": "POST", "path": base + "/{rid}/deny"}]
