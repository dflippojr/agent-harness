"""Issue #64 routes: Google sign-in for household members (bundled Web) and the owner's coarse controls.

Member routes carry no user id, so no principal can act on another member's Google link. Owner routes see only
coarse metadata (linked state, display email, times, session count) and can create or cancel a one-time link code,
revoke Web sessions, and unlink. Owners can never start a Google authorization as a member.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import google_signin as gs
from .manager import HarnessError

log = logging.getLogger("harness.google_signin")

NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
SIGNED_IN = "/#/agents"
LINKED = "/#/profile/account"
FAILED = "/#/signin/failed"


class StartRequest(BaseModel):
    mode: str = "signin"
    code: str = ""  # one-time link code; only ever in this POST body


class ConfirmRequest(BaseModel):
    confirm: bool = False


def _error(e: gs.GoogleSigninError) -> HarnessError:
    return HarnessError(e.status, str(e), code=e.code)


def _member(request: Request, mgr):
    """The enabled, bundled same-origin member making this request, or 403."""
    from .apps import auth
    key = auth(request, "sessions")
    ident = getattr(request.state, "access", None)
    if (key.get("kind") != "member" or not key.get("bundled") or ident is None or not ident.is_member
            or ident.user_id != key.get("user_id")):
        raise HarnessError(403, "only a signed-in household member can manage their Google sign-in")
    return mgr(request), ident


def _same_origin(request: Request, m) -> None:
    problem = m.google_signin.same_origin_problem(request)
    if problem:
        raise HarnessError(403, problem)


def _actor(request: Request) -> str:
    ident = getattr(request.state, "access", None)
    return ident.user_id if ident is not None and ident.allowed else "unknown"


def register(app: FastAPI, mgr) -> list[dict]:
    """Install the routes. Returns the owner admin operations for the admin API index."""
    from .admin import PREFIX, require_admin

    @app.get("/api/v1/auth/session")
    async def auth_session(request: Request):
        m = mgr(request)
        svc = m.google_signin
        ident = request.state.access
        web = request.state.web_auth
        offer = svc.enabled() and web.trusted and (web.admitted or ident.is_member)
        body = {
            "google": {"available": bool(offer), "explanation": gs.EXPLANATION if offer else ""},
            "admitted": web.admitted,
            "signed_in": bool(web.session),
            "role": ident.role if ident.allowed else None,
            # Returned only to the session it belongs to; Web keeps it in memory, never in storage or URLs.
            "csrf": gs.csrf_for(web.token) if web.token else None,
        }
        return JSONResponse(body, headers=NO_STORE)

    @app.post("/api/v1/auth/google/start")
    async def google_start(body: StartRequest, request: Request):
        m = mgr(request)
        _same_origin(request, m)
        try:
            url, browser = await asyncio.to_thread(m.google_signin.start, request, request.state.access,
                                                   body.mode, body.code)
        except gs.GoogleSigninError as e:
            raise _error(e) from None
        response = JSONResponse({"authorization_url": url}, headers=NO_STORE)
        gs.set_attempt_cookie(response, browser)
        return response

    @app.get(gs.CALLBACK_PATH, include_in_schema=False)
    async def google_callback(request: Request):
        m = mgr(request)
        web = request.state.web_auth
        params = {k: request.query_params.get(k) for k in ("state", "code", "error", "iss")}
        try:
            user_id, token = await asyncio.to_thread(m.google_signin.finish, request, request.state.access, params)
        except gs.GoogleSigninError as e:
            m.db.insert_audit(_actor(request), "", "google_callback", "denied", e.reason)
            target = FAILED
            token = ""
        except Exception as e:  # noqa: BLE001 - never surface details (or a traceback) from the callback
            log.warning("Google sign-in callback failed (%s)", type(e).__name__)
            m.db.insert_audit(_actor(request), "", "google_callback", "denied", "internal_error")
            target = FAILED
            token = ""
        else:
            target = SIGNED_IN if web.admitted else LINKED
        response = RedirectResponse(target, status_code=303, headers=NO_STORE)
        gs.clear_attempt_cookie(response)
        if token:
            web.clear_cookie = False
            gs.set_session_cookie(response, token)
        return response

    @app.post("/api/v1/auth/logout")
    async def logout(request: Request):
        m = mgr(request)
        web = request.state.web_auth
        _same_origin(request, m)
        if web.token:
            problem = m.google_signin.csrf_problem(request, web)
            if problem:
                raise HarnessError(403, problem)
            m.google_signin.logout(web.token, _actor(request))
        response = JSONResponse({"ok": True}, headers=NO_STORE)
        web.clear_cookie = False
        gs.clear_session_cookie(response)
        return response

    @app.get("/api/v1/me/google")
    async def my_google(request: Request):
        m, ident = _member(request, mgr)
        return JSONResponse(m.google_signin.member_view(ident.user_id, request.state.web_auth), headers=NO_STORE)

    @app.delete("/api/v1/me/google")
    async def my_google_unlink(body: ConfirmRequest, request: Request):
        m, ident = _member(request, mgr)
        _same_origin(request, m)
        if not body.confirm:
            raise HarnessError(400, "confirm unlinking Google")
        m.google_signin.unlink(ident.user_id, ident.user_id)
        request.state.web_auth.clear_cookie = True
        return JSONResponse(m.google_signin.member_view(ident.user_id), headers=NO_STORE)

    def _account(request: Request, user_id: str):
        from .accounts import AccountService
        AccountService(mgr(request))._require(user_id)
        return mgr(request).google_signin

    @app.get(PREFIX + "/google-signin")
    async def google_status(request: Request, refresh: bool = False):
        require_admin(request, mgr)
        svc = mgr(request).google_signin
        return await asyncio.to_thread(svc.owner_view, refresh)

    @app.post(PREFIX + "/accounts/{user_id}/google/invitation", status_code=201)
    async def google_invite(user_id: str, request: Request):
        require_admin(request, mgr)
        svc = _account(request, user_id)
        try:
            return JSONResponse(svc.create_invitation(_actor(request), user_id), status_code=201, headers=NO_STORE)
        except gs.GoogleSigninError as e:
            raise _error(e) from None

    @app.delete(PREFIX + "/accounts/{user_id}/google/invitation")
    async def google_invite_cancel(user_id: str, request: Request):
        require_admin(request, mgr)
        svc = _account(request, user_id)
        svc.cancel_invitation(_actor(request), user_id)
        return svc.owner_member_view(user_id)

    @app.post(PREFIX + "/accounts/{user_id}/google/revoke-sessions")
    async def google_revoke(user_id: str, request: Request):
        require_admin(request, mgr)
        svc = _account(request, user_id)
        revoked = svc.revoke_sessions(_actor(request), user_id)
        return {**svc.owner_member_view(user_id), "revoked": revoked}

    @app.delete(PREFIX + "/accounts/{user_id}/google")
    async def google_unlink(user_id: str, body: ConfirmRequest, request: Request):
        require_admin(request, mgr)
        if not body.confirm:
            raise HarnessError(400, "confirm unlinking Google")
        svc = _account(request, user_id)
        svc.unlink(_actor(request), user_id)
        return svc.owner_member_view(user_id)

    gs.install_log_redaction()
    return [
        {"method": "GET", "path": PREFIX + "/google-signin"},
        {"method": "POST", "path": PREFIX + "/accounts/{user_id}/google/invitation"},
        {"method": "DELETE", "path": PREFIX + "/accounts/{user_id}/google/invitation"},
        {"method": "POST", "path": PREFIX + "/accounts/{user_id}/google/revoke-sessions"},
        {"method": "DELETE", "path": PREFIX + "/accounts/{user_id}/google"},
    ]
