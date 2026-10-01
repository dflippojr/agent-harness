"""Issue #64: Google OpenID Connect sign-in for pre-provisioned household members in bundled Agent Harness Web.

Tailscale Serve admits the device; a linked Google `sub` selects an existing, enabled member. Google never creates an
account, never makes anyone an owner, and never overrides a Tailscale login that already holds a role. Only same-origin
bundled Web on the configured HTTPS `.ts.net` origin can use the session cookie. See docs/google-signin.md for the
precedence table, owner setup, and recovery.

Nothing secret is persisted: SQLite holds the member's `sub`, display email, and SHA-256 hashes of session cookies and
invitation codes. State, nonce, and the PKCE verifier live only in memory for one 10-minute attempt. The authorization
code and Google's tokens are discarded after the ID token is verified.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from .config import ROOT
from .principal import NOT_ALLOWED_DETAIL, OWNER_USER_ID, Principal, _member_principal, resolve_human

log = logging.getLogger("harness.google_signin")

SESSION_COOKIE = "__Host-ah_session"
ATTEMPT_COOKIE = "__Host-ah_google"
CSRF_HEADER = "x-agent-harness-csrf"
CALLBACK_PATH = "/auth/google/callback"
DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
ISSUER = "https://accounts.google.com"
# Google documents both spellings for `iss` in ID tokens; nothing else is accepted.
TOKEN_ISSUERS = (ISSUER, "accounts.google.com")
GOOGLE_HOSTS = frozenset({"accounts.google.com", "oauth2.googleapis.com", "www.googleapis.com"})
SCOPE = "openid email profile"
ALGORITHMS = ("RS256",)
MODES = ("signin", "link", "invite")

IDLE_SECONDS = 7 * 86400
ABSOLUTE_SECONDS = 30 * 86400
INVITE_SECONDS = 15 * 60
ATTEMPT_SECONDS = 10 * 60
TOUCH_SECONDS = 300          # throttle last-seen writes
LEEWAY_SECONDS = 60
JWKS_MIN_REFRESH = 60        # an unknown `kid` refetches keys at most this often
DEFAULT_CACHE_SECONDS = 3600
MAX_CACHE_SECONDS = 86400
STATIC_CACHE_SECONDS = 300
MAX_ATTEMPTS = 256
SECRET_MAX_BYTES = 16384

LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "localhost"})
CLIENT_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,200}\.apps\.googleusercontent\.com$")
SECRET_RE = re.compile(r"^[\x21-\x7e]{8,512}$")
SUB_RE = re.compile(r"^[\x21-\x7e]{1,255}$")
# Everyone, Anonymous, Network, Interactive, Authenticated Users, BUILTIN\Users, BUILTIN\Guests.
BROAD_SIDS = frozenset({"S-1-1-0", "S-1-5-7", "S-1-5-2", "S-1-5-4", "S-1-5-11", "S-1-5-32-545", "S-1-5-32-546"})

GENERIC_ERROR = "Google sign-in did not complete. Try again, or ask the owner for a new link code."
INVITE_ERROR = "that link code is not valid or has expired"
SIGN_IN_REQUIRED = "sign in with Google to continue"
COOKIE_WITHOUT_TAILSCALE = "Google sessions only work through Tailscale"
EXPLANATION = "Tailscale admits this device to the server; Google identifies your pre-approved household account."


class GoogleSigninError(Exception):
    """A refusal with a non-revealing message. `reason` is for audit only and never includes secrets or claims."""

    def __init__(self, status: int, message: str, reason: str = "", code: str = "google_signin_failed"):
        super().__init__(message)
        self.status = status
        self.reason = reason or code
        self.code = code


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pkce_challenge(verifier: str) -> str:
    return _b64(hashlib.sha256(verifier.encode("ascii")).digest())


def csrf_for(token: str) -> str:
    """Per-session CSRF value, derived from the HttpOnly cookie so it is never stored."""
    return _b64(hmac.new(token.encode("utf-8"), b"agent-harness-web-csrf", hashlib.sha256).digest())


# --- configuration ------------------------------------------------------------------------------------------

def public_origin(cfg) -> str:
    """`https://<machine>.<tailnet>.ts.net[:port]` from `public_url`, or "" when it is not a Serve origin."""
    text = (cfg.public_url or "").strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or not host.endswith(".ts.net") or host.count(".") < 3 or parts.username
            or parts.password or parts.query or parts.fragment or parts.path not in ("", "/")):
        return ""
    return f"https://{host}" + (f":{port}" if port not in (None, 443) else "")


def redirect_uri(cfg) -> str:
    origin = public_origin(cfg)
    return origin + CALLBACK_PATH if origin else ""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _windows_allow_sids(path: Path) -> set[str] | None:
    """SIDs granted Allow ACEs on `path`, via Get-Acl. None when the ACL cannot be read."""
    script = ("$a = Get-Acl -LiteralPath $env:AH_SECRET_PATH; foreach ($r in $a.Access) { "
              "if ($r.AccessControlType -eq 'Allow') { try { "
              "$r.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } "
              "catch { 'unresolved' } } }")
    env = {**os.environ, "AH_SECRET_PATH": str(path)}
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=30, env=env,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


# Tests replace this; production reads the real ACL.
acl_reader = _windows_allow_sids


def permission_problem(path: Path, st: os.stat_result) -> str:
    if sys.platform == "win32":
        sids = acl_reader(path)
        if sids is None:
            return "could not read the client_secret_file ACL"
        if sids & BROAD_SIDS or "unresolved" in sids:
            return "client_secret_file must not grant access to Everyone, Users, or Authenticated Users"
        return ""
    if st.st_mode & 0o077:
        return "client_secret_file must be readable only by the daemon account (chmod 600)"
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return "client_secret_file must be owned by the daemon account"
    return ""


def _parse_secret(cfg, data: bytes) -> str:
    """A plain one-line secret, or Google's downloaded web-client JSON (client ID and redirect URIs are checked)."""
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ValueError("client_secret_file is not UTF-8 text") from None
    if not text.startswith("{"):
        if not SECRET_RE.fullmatch(text):
            raise ValueError("client_secret_file must hold one client secret")
        return text
    try:
        web = json.loads(text).get("web")
    except (json.JSONDecodeError, AttributeError):
        raise ValueError("client_secret_file JSON is not a Google web client file") from None
    if not isinstance(web, dict) or not isinstance(web.get("client_secret"), str):
        raise ValueError("client_secret_file JSON is not a Google web client file")
    if web.get("client_id") not in (None, cfg.google_signin.client_id):
        raise ValueError("client_secret_file belongs to a different client_id")
    uris = web.get("redirect_uris")
    if uris is not None and redirect_uri(cfg) not in (uris if isinstance(uris, list) else []):
        raise ValueError("the Google client does not list this server's exact redirect URI")
    if not SECRET_RE.fullmatch(web["client_secret"]):
        raise ValueError("client_secret_file must hold one client secret")
    return web["client_secret"]


def read_client_secret(cfg) -> str:
    """Validate the owner-managed secret file and return the secret. Errors never include the secret or its path."""
    text = (cfg.google_signin.client_secret_file or "").strip()
    if not text:
        raise ValueError("client_secret_file is not set")
    path = Path(text)
    if not path.is_absolute():
        raise ValueError("client_secret_file must be an absolute path")
    try:
        st = os.lstat(path)
    except OSError:
        raise ValueError("client_secret_file is missing or unreadable") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ValueError("client_secret_file must be a regular file, not a link")
    if not 0 < st.st_size <= SECRET_MAX_BYTES:
        raise ValueError("client_secret_file is empty or too large")
    if _inside(path, ROOT):
        raise ValueError("client_secret_file must live outside the Agent Harness source tree")
    problem = permission_problem(path, st)
    if problem:
        raise ValueError(problem)
    try:
        data = path.read_bytes()
    except OSError:
        raise ValueError("client_secret_file is missing or unreadable") from None
    return _parse_secret(cfg, data)


def config_problems(cfg) -> list[str]:
    """Static readiness checks, in the order an owner would fix them. Empty means ready to preflight."""
    conf = cfg.google_signin
    if not conf.enabled:
        return ["Google sign-in is turned off (google_signin.enabled)"]
    problems = []
    if not public_origin(cfg):
        problems.append("public_url must be the HTTPS Tailscale Serve origin, e.g. https://tower.tailnet.ts.net")
    if (cfg.host or "").strip().lower() not in LOOPBACK_PEERS:
        problems.append("listen.host must be loopback so only Tailscale Serve can supply member logins")
    if not cfg.allowed_logins:
        problems.append("allowed_logins must name the owner before members can sign in")
    if not CLIENT_ID_RE.fullmatch((conf.client_id or "").strip()):
        problems.append("client_id must be a Google OAuth web client ID (*.apps.googleusercontent.com)")
    guests = {g.login for g in cfg.guests}
    overlap = set(conf.admitted_logins) & (set(cfg.allowed_logins) | guests)
    if overlap:
        problems.append("admitted_logins must not include an owner or guest login")
    try:
        read_client_secret(cfg)
    except ValueError as e:
        problems.append(str(e))
    return problems


def _max_age(headers: httpx.Headers) -> float:
    match = re.search(r"max-age=(\d+)", headers.get("cache-control", ""))
    seconds = int(match.group(1)) if match else DEFAULT_CACHE_SECONDS
    return float(min(max(seconds, 60), MAX_CACHE_SECONDS))


def _google_https(url) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and (parts.hostname or "") in GOOGLE_HOSTS and parts.port in (None, 443)


# --- Google OpenID Connect -----------------------------------------------------------------------------------

class GoogleOidc:
    """Discovery, rotation-aware JWKS cache, code exchange, and ID-token verification. Never calls UserInfo."""

    def __init__(self, http: httpx.Client | None = None, clock=time.time):
        self._http = http
        self.clock = clock
        self._lock = threading.Lock()
        self._discovery: dict | None = None
        self._discovery_until = 0.0
        self._keys: dict = {}
        self._keys_until = 0.0
        self._keys_fetched = 0.0

    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=10.0, follow_redirects=False)
        return self._http

    def _get_json(self, url: str) -> tuple[dict, httpx.Headers]:
        resp = self.http().get(url, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            raise GoogleSigninError(503, GENERIC_ERROR, "google_unavailable")
        try:
            body = resp.json()
        except ValueError:
            raise GoogleSigninError(503, GENERIC_ERROR, "google_bad_response") from None
        if not isinstance(body, dict):
            raise GoogleSigninError(503, GENERIC_ERROR, "google_bad_response")
        return body, resp.headers

    def discovery(self, force: bool = False) -> dict:
        with self._lock:
            if not force and self._discovery and self.clock() < self._discovery_until:
                return self._discovery
        try:
            body, headers = self._get_json(DISCOVERY_URL)
        except httpx.HTTPError:
            raise GoogleSigninError(503, GENERIC_ERROR, "google_unavailable") from None
        if (body.get("issuer") != ISSUER
                or not all(_google_https(body.get(k)) for k in ("authorization_endpoint", "token_endpoint",
                                                                  "jwks_uri"))
                or "S256" not in (body.get("code_challenge_methods_supported") or [])
                or "RS256" not in (body.get("id_token_signing_alg_values_supported") or [])):
            raise GoogleSigninError(503, GENERIC_ERROR, "discovery_invalid")
        with self._lock:
            self._discovery = body
            self._discovery_until = self.clock() + _max_age(headers)
        return body

    def _fetch_keys(self) -> None:
        import jwt
        uri = self.discovery()["jwks_uri"]
        try:
            body, headers = self._get_json(uri)
        except httpx.HTTPError:
            raise GoogleSigninError(503, GENERIC_ERROR, "google_unavailable") from None
        seen: dict[str, int] = {}
        for jwk in body.get("keys") or []:
            if isinstance(jwk, dict) and isinstance(jwk.get("kid"), str):
                seen[jwk["kid"]] = seen.get(jwk["kid"], 0) + 1
        keys = {}
        for jwk in body.get("keys") or []:
            if not isinstance(jwk, dict) or seen.get(jwk.get("kid"), 0) != 1:
                continue  # missing or ambiguous key id
            if jwk.get("kty") != "RSA" or jwk.get("use", "sig") != "sig" or jwk.get("alg", "RS256") != "RS256":
                continue
            try:
                keys[jwk["kid"]] = jwt.PyJWK(jwk, algorithm="RS256")
            except (jwt.PyJWKError, jwt.InvalidKeyError, ValueError, TypeError, KeyError):
                continue
        now = self.clock()
        with self._lock:
            self._keys = keys
            self._keys_fetched = now
            self._keys_until = now + _max_age(headers)

    def key_for(self, kid: str):
        now = self.clock()
        with self._lock:
            fresh = now < self._keys_until
            key = self._keys.get(kid) if fresh else None
            may_refresh = not fresh or now - self._keys_fetched >= JWKS_MIN_REFRESH
        if key is not None:
            return key
        if may_refresh:
            self._fetch_keys()
            with self._lock:
                key = self._keys.get(kid)
        if key is None:
            raise GoogleSigninError(401, GENERIC_ERROR, "unknown_key")
        return key

    def exchange(self, code: str, verifier: str, client_id: str, secret: str, redirect: str) -> str:
        """Server-side code exchange. Returns only the ID token; the access token is dropped here."""
        token_endpoint = self.discovery()["token_endpoint"]
        try:
            resp = self.http().post(token_endpoint, data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                "client_id": client_id, "client_secret": secret, "code_verifier": verifier,
            }, headers={"Accept": "application/json"})
        except httpx.HTTPError:
            raise GoogleSigninError(503, GENERIC_ERROR, "google_unavailable") from None
        if resp.status_code != 200:
            raise GoogleSigninError(401, GENERIC_ERROR, "code_rejected")
        try:
            body = resp.json()
        except ValueError:
            raise GoogleSigninError(401, GENERIC_ERROR, "token_response_invalid") from None
        id_token = body.get("id_token") if isinstance(body, dict) else None
        if not isinstance(id_token, str) or not id_token:
            raise GoogleSigninError(401, GENERIC_ERROR, "token_response_invalid")
        return id_token

    def verify(self, id_token: str, client_id: str, nonce: str, not_before: float) -> dict:
        """Verify signature, algorithm, issuer, audience/azp, time, nonce, and verified email. Returns sub/email/name."""
        import jwt
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError:
            raise GoogleSigninError(401, GENERIC_ERROR, "token_malformed") from None
        if header.get("alg") not in ALGORITHMS or not isinstance(header.get("kid"), str) or not header["kid"]:
            raise GoogleSigninError(401, GENERIC_ERROR, "token_algorithm")
        key = self.key_for(header["kid"])
        now = self.clock()
        try:
            claims = jwt.decode(
                id_token, key=key, algorithms=list(ALGORITHMS), audience=client_id, issuer=TOKEN_ISSUERS,
                leeway=LEEWAY_SECONDS, options={"require": ["iss", "sub", "aud", "exp", "iat"],
                                                "verify_exp": False, "verify_iat": False,
                                                "verify_nbf": False})
        except jwt.PyJWTError:
            raise GoogleSigninError(401, GENERIC_ERROR, "token_invalid") from None
        exp, iat = claims.get("exp"), claims.get("iat")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (exp, iat)):
            raise GoogleSigninError(401, GENERIC_ERROR, "token_time")
        if exp <= now - LEEWAY_SECONDS or iat > now + LEEWAY_SECONDS or iat < not_before - LEEWAY_SECONDS:
            raise GoogleSigninError(401, GENERIC_ERROR, "token_time")
        aud = claims.get("aud")
        azp = claims.get("azp")
        if (isinstance(aud, list) and len(aud) != 1 and azp is None) or (azp is not None and azp != client_id):
            raise GoogleSigninError(401, GENERIC_ERROR, "token_audience")
        token_nonce = claims.get("nonce")
        if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, nonce):
            raise GoogleSigninError(401, GENERIC_ERROR, "token_nonce")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not SUB_RE.fullmatch(sub):
            raise GoogleSigninError(401, GENERIC_ERROR, "token_subject")
        email = claims.get("email")
        verified = claims.get("email_verified")
        if not isinstance(email, str) or "@" not in email or len(email) > 320 or verified not in (True, "true"):
            raise GoogleSigninError(401, GENERIC_ERROR, "email_unverified")
        name = claims.get("name")
        return {"sub": sub, "email": email, "name": name[:80] if isinstance(name, str) else ""}


# --- per-request admission ------------------------------------------------------------------------------------

@dataclass
class WebAuth:
    """What the guard learned about the Google session for this request."""
    trusted: bool = False         # loopback peer carrying a Serve-supplied Tailscale login
    admitted: bool = False        # tailnet login admitted only to use a linked Google session
    token: str = ""               # valid session cookie value (never logged)
    session: dict | None = None
    via_session: bool = False     # the principal came from the cookie, so mutations need CSRF
    clear_cookie: bool = False
    set_cookie: dict = field(default_factory=dict)


@dataclass
class Attempt:
    mode: str
    user_id: str
    invite_hash: str
    login: str
    state: str
    nonce: str
    verifier: str
    started: float
    deadline: float


def peer_is_loopback(request) -> bool:
    client = request.client
    return bool(client and client.host in LOOPBACK_PEERS)


def _origin_of(value: str) -> str:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return ""
    if not parts.scheme or not parts.hostname:
        return ""
    default = (parts.scheme == "https" and port in (None, 443)) or (parts.scheme == "http" and port in (None, 80))
    return f"{parts.scheme}://{parts.hostname.lower()}" + ("" if default else f":{port}")


def _pre_auth(login: str) -> Principal:
    return Principal(kind="owner", user_id=OWNER_USER_ID, allowed=False, login=login, detail=SIGN_IN_REQUIRED)


def _cookie_without_tailscale() -> Principal:
    return Principal(kind="owner", user_id=OWNER_USER_ID, allowed=False, detail=COOKIE_WITHOUT_TAILSCALE)


SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
PRE_AUTH_GET = frozenset({"/", "/health", "/sw.js", "/manifest.webmanifest", "/api/v1/auth/session",
                          CALLBACK_PATH})
PRE_AUTH_POST = frozenset({"/api/v1/auth/google/start", "/api/v1/auth/logout"})


class GoogleSignin:
    """Owned by the Manager. All persistent state is in SQLite; sign-in attempts are in memory only."""

    def __init__(self, manager, oidc: GoogleOidc | None = None, clock=time.time):
        self.m = manager
        self.clock = clock
        self.oidc = oidc or GoogleOidc(clock=clock)
        self._attempts: dict[str, Attempt] = {}
        self._lock = threading.Lock()
        self._static: tuple[tuple, float, list[str]] | None = None
        self._preflight: dict | None = None
        web = Path(__file__).parent / "web"
        self._web_files = frozenset("/" + p.name for p in web.iterdir() if p.is_file()) if web.is_dir() else frozenset()

    @property
    def cfg(self):
        return self.m.cfg

    @property
    def db(self):
        return self.m.db

    # configuration and preflight
    def _static_key(self) -> tuple:
        conf = self.cfg.google_signin
        try:
            st = os.stat(conf.client_secret_file) if conf.client_secret_file else None
            file_key = (st.st_mtime_ns, st.st_size, st.st_mode) if st else None
        except OSError:
            file_key = None
        return (conf.enabled, conf.client_id, conf.client_secret_file, tuple(conf.admitted_logins),
                self.cfg.public_url, self.cfg.host, tuple(self.cfg.allowed_logins),
                tuple(g.login for g in self.cfg.guests), file_key)

    def problems(self) -> list[str]:
        key = self._static_key()
        cached = self._static
        if cached and cached[0] == key and self.clock() - cached[1] < STATIC_CACHE_SECONDS:
            return cached[2]
        problems = config_problems(self.cfg)
        self._static = (key, self.clock(), problems)
        return problems

    def enabled(self) -> bool:
        """Static configuration is valid. Existing sessions keep working through a Google outage."""
        return self.cfg.google_signin.enabled and not self.problems()

    def preflight(self, force: bool = False) -> dict:
        problems = list(self.problems())
        if not problems:
            try:
                self.oidc.discovery(force=force)
            except GoogleSigninError:
                problems.append("Google's OpenID discovery document could not be fetched or failed validation")
        self._preflight = {"ok": not problems, "problems": problems, "checked_at": self.clock()}
        return self._preflight

    def owner_view(self, refresh: bool = False) -> dict:
        conf = self.cfg.google_signin
        preflight = self.preflight(force=True) if refresh else (self._preflight or {
            "ok": None, "problems": self.problems(), "checked_at": None})
        return {"enabled": bool(conf.enabled), "ready": self.enabled(), "client_id": conf.client_id or "",
                "redirect_uri": redirect_uri(self.cfg), "admitted_logins": list(conf.admitted_logins),
                "preflight": preflight}

    # admission and principal precedence (docs/google-signin.md)
    def _context_ok(self, request) -> bool:
        """Cookie auth is honored only for same-origin bundled Web on the public Serve origin."""
        origin = request.headers.get("origin", "")
        if origin and _origin_of(origin) != public_origin(self.cfg):
            return False
        return request.headers.get("sec-fetch-site", "same-origin") in ("same-origin", "none")

    def resolve(self, request, login: str | None) -> Principal:
        base = resolve_human(self.cfg, login, self.db)
        web = WebAuth()
        request.state.web_auth = web
        token = request.cookies.get(SESSION_COOKIE, "")
        conf = self.cfg.google_signin
        if not conf.enabled:
            return base
        usable = self.enabled()
        web.trusted = bool(login) and peer_is_loopback(request)
        web.admitted = (usable and web.trusted and login in conf.admitted_logins and base.kind == "owner"
                        and not base.allowed and base.detail == NOT_ALLOWED_DETAIL)
        if not token:
            return _pre_auth(login) if web.admitted else base
        if login is None:
            # Without a Serve-supplied login a request is local. A Google cookie here did not come through Serve's
            # identity path, so it never falls back to the localhost owner.
            web.clear_cookie = True
            return _cookie_without_tailscale()
        if not usable or not web.trusted or not self._context_ok(request):
            return _pre_auth(login) if web.admitted else base
        row = self.session_row(token)
        if row is None:
            web.clear_cookie = True
            return _pre_auth(login) if web.admitted else base
        if base.kind == "member":
            if base.user_id == row["user_id"]:
                web.token, web.session = token, row
            return base  # a login mapped to one member never switches members through Google
        if not web.admitted:
            return base  # owner, guest, and refused logins ignore Google sessions
        account = self.db.account_by_id(row["user_id"])
        if account is None or account.get("role") != "member":
            web.clear_cookie = True
            return _pre_auth(login)
        web.token, web.session, web.via_session = token, row, True
        return _member_principal(account, login)

    def pre_auth_allowed(self, method: str, path: str) -> bool:
        if method in ("GET", "HEAD"):
            return path in PRE_AUTH_GET or path in self._web_files or path.startswith("/static/")
        return method == "POST" and path in PRE_AUTH_POST

    def same_origin_problem(self, request) -> str:
        origin = request.headers.get("origin", "")
        if not origin or _origin_of(origin) != public_origin(self.cfg):
            return "cross-origin request refused"
        if request.headers.get("sec-fetch-site") != "same-origin":
            return "cross-origin request refused"
        return ""

    def csrf_problem(self, request, web: WebAuth) -> str:
        problem = self.same_origin_problem(request)
        if problem:
            return problem
        sent = request.headers.get(CSRF_HEADER, "")
        if not web.token or not sent or not hmac.compare_digest(sent, csrf_for(web.token)):
            return "missing or invalid CSRF token"
        return ""

    # sessions
    def session_row(self, token: str) -> dict | None:
        if not token or len(token) > 128:
            return None
        id_hash = _hash(token)
        row = self.db.web_session(id_hash)
        now = self.clock()
        if (row is None or row.get("revoked_at") is not None or now >= row["expires_at"]
                or now - row["last_seen_at"] >= IDLE_SECONDS):
            return None
        account = self.db.account_by_id(row["user_id"])
        if account is None or not bool(account.get("enabled", 1)) or self.db.google_identity(row["user_id"]) is None:
            return None
        if now - row["last_seen_at"] >= TOUCH_SECONDS:
            self.db.touch_web_session(id_hash, now)
        return row

    def issue_session(self, user_id: str, replacing: str = "") -> str:
        now = self.clock()
        if replacing:
            self.db.revoke_web_session(_hash(replacing), now)
        token = secrets.token_urlsafe(32)
        self.db.insert_web_session(_hash(token), user_id, now, now + ABSOLUTE_SECONDS)
        self.db.purge_web_sessions(now - ABSOLUTE_SECONDS)
        return token

    def logout(self, token: str, actor_id: str) -> None:
        user_id = self.db.revoke_web_session(_hash(token), self.clock()) if token else None
        if user_id:
            self.db.insert_audit(actor_id, user_id, "google_logout", "ok")
            self.m.revoke_member_streams(user_id)

    def revoke_sessions(self, actor_id: str, user_id: str, reason: str = "revoke_sessions") -> int:
        count = self.db.revoke_web_sessions(user_id, self.clock())
        self.m.revoke_member_streams(user_id)
        self.db.insert_audit(actor_id, user_id, f"google_{reason}", "ok", f"{count} sessions")
        return count

    def member_disabled(self, user_id: str) -> None:
        self.db.cancel_google_invitation(user_id)
        self.db.revoke_web_sessions(user_id, self.clock())

    def member_rebound(self, user_id: str) -> None:
        self.db.revoke_web_sessions(user_id, self.clock())

    # views
    def member_view(self, user_id: str, web: WebAuth | None = None) -> dict:
        ident = self.db.google_identity(user_id)
        now = self.clock()
        via_session = bool(web and web.via_session)
        return {
            "available": self.enabled(), "linked": ident is not None,
            "email": ident["email"] if ident else "", "linked_at": ident["linked_at"] if ident else None,
            "last_sign_in_at": ident["last_sign_in_at"] if ident else None,
            "active_web_sessions": self.db.count_web_sessions(user_id, now, IDLE_SECONDS),
            "signed_in_with_google": via_session,
            # Unlinking from a device that Tailscale admits only for Google removes this device's way in.
            "unlink_removes_this_device": via_session,
            "explanation": EXPLANATION,
        }

    def owner_member_view(self, user_id: str) -> dict:
        ident = self.db.google_identity(user_id)
        now = self.clock()
        invitation = self.db.google_invitation(user_id, now)
        return {
            "linked": ident is not None, "email": ident["email"] if ident else "",
            "linked_at": ident["linked_at"] if ident else None,
            "last_sign_in_at": ident["last_sign_in_at"] if ident else None,
            "active_web_sessions": self.db.count_web_sessions(user_id, now, IDLE_SECONDS),
            "invitation_expires_at": invitation["expires_at"] if invitation else None,
        }

    # owner and member account actions
    def create_invitation(self, actor_id: str, user_id: str) -> dict:
        account = self.db.account_by_id(user_id)
        if account is None or account.get("role") != "member":
            raise GoogleSigninError(404, "no household account matches that id", code="not_found")
        if not bool(account.get("enabled", 1)):
            raise GoogleSigninError(409, "enable this household account first", code="account_disabled")
        if self.db.google_identity(user_id) is not None:
            raise GoogleSigninError(409, "unlink the current Google account first", code="already_linked")
        code = "ahg-" + secrets.token_urlsafe(32)  # 256 bits
        now = self.clock()
        self.db.put_google_invitation(user_id, _hash(code), now, now + INVITE_SECONDS)
        self.db.insert_audit(actor_id, user_id, "google_invite", "ok")
        return {"code": code, "expires_at": now + INVITE_SECONDS}

    def cancel_invitation(self, actor_id: str, user_id: str) -> bool:
        cancelled = self.db.cancel_google_invitation(user_id)
        self.db.insert_audit(actor_id, user_id, "google_invite_cancel", "ok" if cancelled else "noop")
        return cancelled

    def unlink(self, actor_id: str, user_id: str) -> bool:
        removed = self.db.unlink_google_identity(user_id)
        self.db.cancel_google_invitation(user_id)
        self.db.revoke_web_sessions(user_id, self.clock())
        self.m.revoke_member_streams(user_id)
        self.db.insert_audit(actor_id, user_id, "google_unlink", "ok" if removed else "noop")
        return removed

    # authorization flow
    def _prune(self, now: float) -> None:
        for key in [k for k, a in self._attempts.items() if a.deadline <= now]:
            del self._attempts[key]

    def _enabled_member(self, user_id: str) -> dict | None:
        account = self.db.account_by_id(user_id)
        if account is None or account.get("role") != "member" or not bool(account.get("enabled", 1)):
            return None
        return account

    def start(self, request, ident: Principal, mode: str, code: str = "") -> tuple[str, str]:
        """Bind a new attempt to this browser, admission, and account; return (authorization URL, browser token)."""
        web: WebAuth = request.state.web_auth
        if not self.enabled():
            raise GoogleSigninError(503, "Google sign-in is not available on this server", code="unavailable")
        if not web.trusted:
            raise GoogleSigninError(403, "Google sign-in only works through Tailscale on this server's address",
                                    code="untrusted_ingress")
        if mode not in MODES:
            raise GoogleSigninError(400, "unknown sign-in mode", code="bad_request")
        login = request.headers.get("tailscale-user-login", "")
        user_id, invite_hash = "", ""
        if mode == "signin":
            if not web.admitted:
                raise GoogleSigninError(403, "Google sign-in is for household members on an admitted device",
                                        code="not_admitted")
        elif mode == "link":
            if not ident.is_member:
                raise GoogleSigninError(403, "only a signed-in household member can link their own Google account",
                                        code="not_member")
            user_id = ident.user_id
            if self.db.google_identity(user_id) is not None:
                raise GoogleSigninError(409, "unlink the current Google account first", code="already_linked")
        else:
            code = (code or "").strip()
            invite_hash = _hash(code) if 0 < len(code) <= 128 else ""
            user_id = self.db.google_invitation_user(invite_hash, self.clock()) if invite_hash else None
            if (not user_id or not self._enabled_member(user_id) or self.db.google_identity(user_id) is not None
                    or not (web.admitted or (ident.is_member and ident.user_id == user_id))):
                self.db.insert_audit(ident.user_id if ident.allowed else "unknown", "", "google_invite_redeem",
                                     "denied")
                raise GoogleSigninError(400, INVITE_ERROR, code="invalid_invitation")
        discovery = self.oidc.discovery()
        now = self.clock()
        verifier = secrets.token_urlsafe(64)
        attempt = Attempt(mode=mode, user_id=user_id, invite_hash=invite_hash, login=login,
                          state=secrets.token_urlsafe(32), nonce=secrets.token_urlsafe(32), verifier=verifier,
                          started=now, deadline=now + ATTEMPT_SECONDS)
        browser = secrets.token_urlsafe(32)
        with self._lock:
            self._prune(now)
            if len(self._attempts) >= MAX_ATTEMPTS:
                raise GoogleSigninError(429, "too many sign-in attempts; try again shortly", code="busy")
            self._attempts[_hash(browser)] = attempt
        params = {
            "response_type": "code", "client_id": self.cfg.google_signin.client_id,
            "redirect_uri": redirect_uri(self.cfg), "scope": SCOPE, "state": attempt.state,
            "nonce": attempt.nonce, "code_challenge": pkce_challenge(verifier), "code_challenge_method": "S256",
            "access_type": "online", "prompt": "select_account",
        }
        return discovery["authorization_endpoint"] + "?" + urlencode(params), browser

    def _take_attempt(self, browser: str) -> Attempt | None:
        if not browser or len(browser) > 128:
            return None
        with self._lock:
            return self._attempts.pop(_hash(browser), None)  # single use, even when the callback then fails

    def finish(self, request, ident: Principal, params) -> tuple[str, str]:
        """Complete the callback. Returns (user_id, new session token) or raises GoogleSigninError (generic)."""
        web: WebAuth = request.state.web_auth
        attempt = self._take_attempt(request.cookies.get(ATTEMPT_COOKIE, ""))
        now = self.clock()
        state = params.get("state") or ""
        if attempt is None or not hmac.compare_digest(state, attempt.state):
            raise GoogleSigninError(400, GENERIC_ERROR, "state_mismatch")
        if now >= attempt.deadline:
            raise GoogleSigninError(400, GENERIC_ERROR, "attempt_expired")
        if params.get("iss") not in (None, ISSUER):
            raise GoogleSigninError(400, GENERIC_ERROR, "issuer_mixup")
        if params.get("error"):
            raise GoogleSigninError(400, GENERIC_ERROR, "denied_at_google")
        login = request.headers.get("tailscale-user-login", "")
        if not self.enabled() or not web.trusted or login != attempt.login:
            raise GoogleSigninError(403, GENERIC_ERROR, "admission_changed")
        code = params.get("code") or ""
        if not code or len(code) > 2048:
            raise GoogleSigninError(400, GENERIC_ERROR, "code_missing")
        try:
            secret = read_client_secret(self.cfg)
        except ValueError:
            raise GoogleSigninError(503, GENERIC_ERROR, "secret_unavailable") from None
        conf = self.cfg.google_signin
        id_token = self.oidc.exchange(code, attempt.verifier, conf.client_id, secret, redirect_uri(self.cfg))
        claims = self.oidc.verify(id_token, conf.client_id, attempt.nonce, attempt.started)
        del id_token, secret, code
        if attempt.mode == "signin":
            user_id = self._finish_signin(web, claims, now)
        else:
            user_id = self._finish_link(web, ident, attempt, claims, now)
        return user_id, self.issue_session(user_id, replacing=web.token)

    def _finish_signin(self, web: WebAuth, claims: dict, now: float) -> str:
        if not web.admitted:
            raise GoogleSigninError(403, GENERIC_ERROR, "not_admitted")
        identity = self.db.google_identity_by_sub(claims["sub"])
        if identity is None or not self._enabled_member(identity["user_id"]):
            raise GoogleSigninError(403, GENERIC_ERROR, "no_linked_member")
        self.db.touch_google_sign_in(identity["user_id"], claims["email"], now)
        self.db.insert_audit(identity["user_id"], identity["user_id"], "google_signin", "ok")
        return identity["user_id"]

    def _finish_link(self, web: WebAuth, ident: Principal, attempt: Attempt, claims: dict, now: float) -> str:
        user_id = attempt.user_id
        if not (web.admitted or (ident.is_member and ident.user_id == user_id)):
            raise GoogleSigninError(403, GENERIC_ERROR, "principal_changed")
        try:
            with self.db.tx():
                if self._enabled_member(user_id) is None:
                    raise GoogleSigninError(403, GENERIC_ERROR, "member_unavailable")
                if attempt.mode == "invite" and not self.db.consume_google_invitation(
                        user_id, attempt.invite_hash, now):
                    raise GoogleSigninError(400, GENERIC_ERROR, "invitation_used_or_expired")
                if not self.db.link_google_identity(user_id, claims["sub"], claims["email"], now):
                    raise GoogleSigninError(409, GENERIC_ERROR, "already_linked")
        except GoogleSigninError as e:
            self.db.insert_audit(user_id, user_id, f"google_{attempt.mode}", "denied", e.reason)
            raise
        # Linking is identity-sensitive: earlier Web sessions for this member do not survive it.
        self.db.revoke_web_sessions(user_id, now)
        self.m.revoke_member_streams(user_id)
        self.db.insert_audit(user_id, user_id, f"google_{attempt.mode}", "ok")
        return user_id


# --- cookies and logging ---------------------------------------------------------------------------------------

def set_session_cookie(response, token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=ABSOLUTE_SECONDS, path="/", secure=True, httponly=True,
                        samesite="lax")


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


def set_attempt_cookie(response, browser: str) -> None:
    response.set_cookie(ATTEMPT_COOKIE, browser, max_age=ATTEMPT_SECONDS, path="/", secure=True, httponly=True,
                        samesite="lax")


def clear_attempt_cookie(response) -> None:
    response.delete_cookie(ATTEMPT_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


class RedactCallbackQuery(logging.Filter):
    """uvicorn's access log prints the request line; drop the callback's query (code, state, error)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(_redact(a) for a in args)
        if isinstance(record.msg, str) and CALLBACK_PATH + "?" in record.msg:
            record.msg = _redact(record.msg)
        return True


def _redact(value):
    if isinstance(value, str) and CALLBACK_PATH in value and "?" in value:
        return re.sub(re.escape(CALLBACK_PATH) + r"\?\S*", CALLBACK_PATH + "?[redacted]", value)
    return value


def install_log_redaction() -> None:
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactCallbackQuery) for f in logger.filters):
            logger.addFilter(RedactCallbackQuery())
