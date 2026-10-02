"""Issue #64: Google sign-in for pre-provisioned household members, against a mocked Google (no network)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from harness import google_signin as gs
from harness.admin import PREFIX
from harness.api import create_app
from harness.config import GoogleSigninConfig, GuestAccess, Project
from harness.llm import Completion
from harness.manager import Manager
from harness.principal import resolve_human

from test_daemon import Script, make_cfg
from test_household import ALICE, BOB, GUEST, OWNER

BASE = "https://tower.tailnet.ts.net"
KITCHEN = "kitchen@example.com"      # admitted only to use a linked Google session
STRANGER = "stranger@example.com"    # tailnet login with no role and not admitted
CLIENT_ID = "1234-test.apps.googleusercontent.com"
SECRET = "GOCSPX-test-secret-value"
REDIRECT = BASE + gs.CALLBACK_PATH
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ACCESS_TOKEN = "ya29.fake-access-token"
SAME_ORIGIN = {"Origin": BASE, "Sec-Fetch-Site": "same-origin"}
ALICE_SUB = "109876543210987654321"
BOB_SUB = "100000000000000000002"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class _PyJwtDatetime:
    """Stands in for `datetime` inside PyJWT so its exp/iat checks follow the test clock."""

    def __init__(self, clock):
        self.clock = clock

    def now(self, tz=None):
        return datetime.fromtimestamp(self.clock(), tz=tz)

    def __getattr__(self, name):
        return getattr(datetime, name)


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeGoogle:
    """Discovery, JWKS, and token endpoints. Enforces PKCE, exact redirect, client secret, and single-use codes."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.keys = {"k1": rsa.generate_private_key(public_exponent=65537, key_size=2048)}
        self.sign_kid = "k1"
        self.codes: dict[str, dict] = {}
        self.calls = Counter()
        self.down = False
        self.discovery_override: dict = {}
        self.issued: list[str] = []   # every ID/access token value handed out

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls[url] += 1
        if self.down:
            return httpx.Response(503)
        if url == gs.DISCOVERY_URL:
            return httpx.Response(200, json={
                "issuer": gs.ISSUER, "authorization_endpoint": AUTH_URL, "token_endpoint": TOKEN_URL,
                "jwks_uri": JWKS_URL, "userinfo_endpoint": "https://openidconnect.googleapis.com/v1/userinfo",
                "code_challenge_methods_supported": ["plain", "S256"],
                "id_token_signing_alg_values_supported": ["RS256"], **self.discovery_override,
            }, headers={"Cache-Control": "public, max-age=3600"})
        if url == JWKS_URL:
            keys = []
            for kid, key in self.keys.items():
                nums = key.public_key().public_numbers()
                keys.append({"kty": "RSA", "alg": "RS256", "use": "sig", "kid": kid,
                             "n": _b64url(nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")),
                             "e": _b64url(nums.e.to_bytes(3, "big"))})
            return httpx.Response(200, json={"keys": keys}, headers={"Cache-Control": "public, max-age=3600"})
        if url == TOKEN_URL and request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            entry = self.codes.pop(form.get("code", ""), None)
            challenge = _b64url(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
            if (entry is None or form.get("grant_type") != "authorization_code" or form.get("client_id") != CLIENT_ID
                    or form.get("client_secret") != SECRET or form.get("redirect_uri") != REDIRECT
                    or challenge != entry["challenge"]):
                return httpx.Response(400, json={"error": "invalid_grant"})
            id_token = self.id_token(entry)
            self.issued.append(id_token)
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "id_token": id_token,
                                             "expires_in": 3599, "token_type": "Bearer", "scope": gs.SCOPE})
        return httpx.Response(404)

    def id_token(self, entry: dict) -> str:
        now = int(self.clock())
        claims = {"iss": gs.ISSUER, "aud": CLIENT_ID, "azp": CLIENT_ID, "sub": entry["sub"], "email": entry["email"],
                  "email_verified": True, "iat": now, "exp": now + 3600, "nonce": entry["nonce"], "name": "Member"}
        claims.update(entry.get("claims") or {})
        for key, value in list(claims.items()):
            if value is None:
                del claims[key]
        header = {"kid": entry.get("kid") or self.sign_kid, **(entry.get("header") or {})}
        if entry.get("raw"):
            return entry["raw"](claims, header)
        key = entry.get("key") or self.keys[header["kid"]]
        return jwt.encode(claims, key, algorithm="RS256", headers=header)

    def authorize(self, url: str, sub: str, email: str, **extra) -> tuple[str, str]:
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTH_URL
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert q["response_type"] == "code" and q["client_id"] == CLIENT_ID and q["redirect_uri"] == REDIRECT
        assert q["scope"] == "openid email profile" and q["code_challenge_method"] == "S256"
        assert q["access_type"] == "online" and "include_granted_scopes" not in q
        assert len(q["state"]) >= 43 and len(q["nonce"]) >= 43
        code = "4/0fake-" + os.urandom(12).hex()
        self.codes[code] = {"sub": sub, "email": email, "nonce": q["nonce"], "challenge": q["code_challenge"],
                            **extra}
        return q["state"], code


class Browser:
    """One browser profile: its own cookie jar and in-memory CSRF value."""

    def __init__(self, client: TestClient, login: str | None):
        self.client = client
        self.login = login
        self.jar = httpx.Cookies()
        self.csrf = ""

    def request(self, method: str, path: str, *, json=None, headers=None, login="__self__", origin=True,
                params=None) -> httpx.Response:
        login = self.login if login == "__self__" else login
        h = {}
        if login:
            h["Tailscale-User-Login"] = login
        if origin and method not in ("GET", "HEAD"):
            h.update(SAME_ORIGIN)
        if self.csrf:
            h[gs.CSRF_HEADER] = self.csrf
        h.update(headers or {})
        self.client.cookies = self.jar
        r = self.client.request(method, path, json=json, headers=h, params=params, follow_redirects=False)
        self.jar = httpx.Cookies(self.client.cookies)
        return r

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    def refresh_csrf(self) -> dict:
        body = self.get("/api/v1/auth/session").json()
        self.csrf = body.get("csrf") or ""
        return body

    def session_cookie(self) -> str:
        return self.jar.get(gs.SESSION_COOKIE, domain="tower.tailnet.ts.net") or ""


class Env:
    def __init__(self, tmp_path, monkeypatch, *, enabled=True, guests=None):
        cfg = make_cfg(tmp_path)
        cfg.allowed_logins = [OWNER]
        cfg.guests = guests or [GuestAccess(login=GUEST)]
        cfg.public_url = BASE
        cfg.host = "127.0.0.1"
        cfg.projects["lab"] = Project(name="lab", homelab=True)
        self.secret_file = tmp_path / "google-client-secret.txt"
        self.secret_file.write_text(SECRET + "\n", encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(self.secret_file, 0o600)
        monkeypatch.setattr(gs, "acl_reader", lambda path: {"S-1-5-18", "S-1-5-32-544", "S-1-5-21-1-2-3-1001"})
        cfg.google_signin = GoogleSigninConfig(enabled=enabled, client_id=CLIENT_ID,
                                               client_secret_file=str(self.secret_file), admitted_logins=[KITCHEN])
        self.cfg = cfg
        self.clock = Clock()
        self.monkeypatch = monkeypatch
        self.follow_clock()
        self.google = FakeGoogle(self.clock)
        self.m = Manager(cfg, chat=Script([Completion(content="done")]))
        self._wire(self.m)
        self.client = TestClient(create_app(self.m), base_url=BASE, client=("127.0.0.1", 50000))
        self.client.__enter__()
        self.ids = {}
        for login in (ALICE, BOB):
            r = self.client.post(f"{PREFIX}/accounts", json={"login": login, "display_name": login[:3]},
                                 headers={"Tailscale-User-Login": OWNER})
            assert r.status_code == 201, r.text
            self.ids[login] = r.json()["user_id"]

    def follow_clock(self):
        self.monkeypatch.setattr("jwt.api_jwt.datetime", _PyJwtDatetime(self.clock))

    def _wire(self, m):
        m.google_signin.clock = self.clock
        m.google_signin.oidc = gs.GoogleOidc(http=httpx.Client(transport=self.google.transport()), clock=self.clock)

    def close(self):
        self.client.__exit__(None, None, None)

    def browser(self, login):
        return Browser(self.client, login)

    def owner(self, method, path, **kw):
        return self.client.request(method, path, headers={"Tailscale-User-Login": OWNER}, **kw)

    def start(self, b: Browser, mode="signin", code="") -> httpx.Response:
        return b.post("/api/v1/auth/google/start", json={"mode": mode, "code": code})

    def callback(self, b: Browser, state: str, code: str, **extra) -> httpx.Response:
        params = {"state": state, "code": code, **extra}
        return b.get(gs.CALLBACK_PATH, params=params, headers={"Sec-Fetch-Site": "cross-site"})

    def flow(self, b: Browser, sub: str, email: str, mode="signin", code="", **extra) -> httpx.Response:
        r = self.start(b, mode, code)
        assert r.status_code == 200, r.text
        state, gcode = self.google.authorize(r.json()["authorization_url"], sub, email, **extra)
        return self.callback(b, state, gcode)

    def invite(self, login) -> str:
        r = self.owner("POST", f"{PREFIX}/accounts/{self.ids[login]}/google/invitation")
        assert r.status_code == 201, r.text
        return r.json()["code"]

    def link(self, login, sub, email=None) -> Browser:
        """Direct-Tailscale member self-link; returns that member's browser."""
        b = self.browser(login)
        r = self.flow(b, sub, email or login, mode="link")
        assert r.status_code == 303 and r.headers["location"] == "/#/profile/account", r.headers
        return b

    def kitchen_signin(self, sub=ALICE_SUB, email=ALICE) -> Browser:
        b = self.browser(KITCHEN)
        r = self.flow(b, sub, email)
        assert r.headers["location"] == "/#/agents", r.headers
        b.refresh_csrf()
        return b


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    e.close()


def me(b: Browser) -> httpx.Response:
    return b.get("/api/v1/me")


# --- defaults, configuration, and preflight -------------------------------------------------------------------

def test_defaults_off_preserve_existing_principals(tmp_path, monkeypatch):
    assert GoogleSigninConfig().enabled is False
    e = Env(tmp_path, monkeypatch, enabled=False)
    try:
        for login in (None, OWNER, ALICE, BOB, GUEST, KITCHEN, STRANGER):
            b = e.browser(login)
            b.jar.set(gs.SESSION_COOKIE, "forged", domain="tower.tailnet.ts.net")
            expected = resolve_human(e.cfg, login, e.m.db)
            r = b.get("/me")
            if expected.allowed:
                assert r.status_code == 200 and r.json()["role"] == expected.role, (login, r.text)
            else:
                assert r.status_code == 403, (login, r.text)
        body = e.browser(KITCHEN).get("/api/v1/auth/session")
        assert body.status_code == 403  # Google off: an admitted-only login is just an unknown login
        assert e.browser(ALICE).get("/api/v1/auth/session").json()["google"]["available"] is False
        r = e.start(e.browser(ALICE), "link")
        assert r.status_code == 503
    finally:
        e.close()


def test_new_tables_hold_no_rows_until_used(env):
    con = sqlite3.connect(env.cfg.db_path)
    for table in ("google_identities", "google_link_invitations", "web_sessions"):
        assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    con.close()


@pytest.mark.parametrize("change, fragment", [
    (lambda c, e: setattr(c, "public_url", "http://tower.tailnet.ts.net"), "public_url"),
    (lambda c, e: setattr(c, "public_url", "https://tower.example.com"), "public_url"),
    (lambda c, e: setattr(c, "public_url", "https://tower.tailnet.ts.net/app"), "public_url"),
    (lambda c, e: setattr(c, "host", "0.0.0.0"), "listen.host"),
    (lambda c, e: setattr(c, "allowed_logins", []), "allowed_logins"),
    (lambda c, e: setattr(c.google_signin, "client_id", "not-a-client"), "client_id"),
    (lambda c, e: setattr(c.google_signin, "client_secret_file", ""), "client_secret_file is not set"),
    (lambda c, e: setattr(c.google_signin, "client_secret_file", "relative.txt"), "absolute"),
    (lambda c, e: setattr(c.google_signin, "client_secret_file", str(e.secret_file) + ".missing"), "missing"),
    (lambda c, e: e.secret_file.write_text("", encoding="utf-8"), "empty"),
    (lambda c, e: e.secret_file.write_text("two words", encoding="utf-8"), "one client secret"),
    (lambda c, e: setattr(c.google_signin, "admitted_logins", [OWNER]), "admitted_logins"),
    (lambda c, e: e.secret_file.write_text(json.dumps({"web": {
        "client_id": CLIENT_ID, "client_secret": SECRET, "redirect_uris": ["https://localhost/cb"]}}),
        encoding="utf-8"), "redirect URI"),
    (lambda c, e: e.secret_file.write_text(json.dumps({"web": {
        "client_id": "other.apps.googleusercontent.com", "client_secret": SECRET}}), encoding="utf-8"),
     "different client_id"),
])
def test_config_problems_keep_feature_disabled(env, change, fragment):
    change(env.cfg, env)
    problems = gs.config_problems(env.cfg)
    assert any(fragment in p for p in problems), problems
    assert all(SECRET not in p and str(env.secret_file) not in p for p in problems)
    env.m.google_signin._static = None
    assert not env.m.google_signin.enabled()
    assert env.browser(KITCHEN).get("/api/v1/auth/session").status_code == 403


def test_google_json_client_file_and_exact_redirect(env):
    env.secret_file.write_text(json.dumps({"web": {"client_id": CLIENT_ID, "client_secret": SECRET,
                                                   "redirect_uris": [REDIRECT]}}), encoding="utf-8")
    assert gs.config_problems(env.cfg) == []
    assert gs.read_client_secret(env.cfg) == SECRET
    assert gs.redirect_uri(env.cfg) == REDIRECT


def test_secret_file_permissions(env, monkeypatch):
    if sys.platform == "win32":
        monkeypatch.setattr(gs, "acl_reader", lambda path: {"S-1-5-18", "S-1-1-0"})
        assert any("Everyone" in p for p in gs.config_problems(env.cfg))
        monkeypatch.setattr(gs, "acl_reader", lambda path: None)
        assert any("ACL" in p for p in gs.config_problems(env.cfg))
    else:
        os.chmod(env.secret_file, 0o644)
        assert any("chmod 600" in p for p in gs.config_problems(env.cfg))


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows ACL read")
def test_windows_acl_reader_reads_real_acl(tmp_path):
    path = tmp_path / "secret.txt"
    path.write_text(SECRET, encoding="utf-8")
    sids = gs._windows_allow_sids(path)
    if sids is None:
        pytest.skip("PowerShell Get-Acl is unavailable in this environment (the product treats this as insecure)")
    assert sids and all(s.startswith("S-1-") or s == "unresolved" for s in sids)


def test_admitted_login_overlapping_a_member_fails_closed(env):
    env.cfg.google_signin.admitted_logins = [ALICE]
    problems = env.m.google_signin.problems()
    assert any("member login" in p for p in problems), problems
    assert any("member login" in p for p in gs.config_problems(env.cfg, [ALICE]))
    assert not env.m.google_signin.enabled()
    view = env.owner("GET", f"{PREFIX}/google-signin", params={"refresh": "true"}).json()
    assert view["ready"] is False and view["preflight"]["ok"] is False


def test_unreadable_acl_is_retried_not_cached(env, monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows ACL cache")
    calls = []

    def flaky(path):
        calls.append(path)
        return None if len(calls) == 1 else {"S-1-5-18"}
    monkeypatch.setattr(gs, "acl_reader", flaky)
    gs._acl_cache.clear()
    st = os.stat(env.secret_file)
    assert gs.permission_problem(env.secret_file, st) == "could not read the client_secret_file ACL"
    assert gs.permission_problem(env.secret_file, st) == ""


def test_secret_inside_source_tree_refused(env):
    # any small regular file inside the repo; harness.yaml itself can outgrow SECRET_MAX_BYTES (checked first)
    env.cfg.google_signin.client_secret_file = str(gs.ROOT / "requirements.txt")
    assert any("source tree" in p for p in gs.config_problems(env.cfg))


def test_preflight_and_owner_view(env):
    r = env.owner("GET", f"{PREFIX}/google-signin", params={"refresh": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] and body["preflight"]["ok"] and body["redirect_uri"] == REDIRECT
    assert SECRET not in r.text and str(env.secret_file) not in r.text
    env.google.discovery_override = {"issuer": "https://evil.example.com"}
    body = env.owner("GET", f"{PREFIX}/google-signin", params={"refresh": "true"}).json()
    assert body["preflight"]["ok"] is False
    env.google.discovery_override = {"code_challenge_methods_supported": ["plain"]}
    assert env.owner("GET", f"{PREFIX}/google-signin", params={"refresh": "true"}).json()["preflight"]["ok"] is False
    env.google.discovery_override = {"token_endpoint": "https://evil.example.com/token"}
    assert env.owner("GET", f"{PREFIX}/google-signin", params={"refresh": "true"}).json()["preflight"]["ok"] is False
    assert env.browser(ALICE).get(f"{PREFIX}/google-signin").status_code == 403


def test_google_outage_never_blocks_owner_or_existing_sessions(env):
    b = env.link(ALICE, ALICE_SUB)
    kitchen = env.kitchen_signin()
    env.google.down = True
    env.m.google_signin.oidc._discovery = None
    assert env.owner("GET", f"{PREFIX}/accounts").status_code == 200
    assert me(kitchen).json()["user_id"] == env.ids[ALICE]
    assert me(b).status_code == 200
    r = env.start(env.browser(KITCHEN), "signin")
    assert r.status_code == 503 and SECRET not in r.text


# --- enrollment -----------------------------------------------------------------------------------------------

def test_self_link_sets_strict_cookie_and_links_sub(env):
    b = env.browser(ALICE)
    r = env.start(b, "link")
    assert r.status_code == 200
    attempt_cookie = r.headers["set-cookie"]
    assert attempt_cookie.startswith(gs.ATTEMPT_COOKIE + "=")
    state, code = env.google.authorize(r.json()["authorization_url"], ALICE_SUB, "alice@gmail.com")
    r = env.callback(b, state, code)
    assert r.status_code == 303 and r.headers["location"] == "/#/profile/account"
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
    cookies = r.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith(gs.SESSION_COOKIE + "="))
    lowered = session.lower()
    for flag in ("secure", "httponly", "samesite=lax", "path=/"):
        assert flag in lowered
    assert "domain=" not in lowered
    assert any(c.startswith(gs.ATTEMPT_COOKIE + "=") and "max-age=0" in c.lower() for c in cookies)
    view = b.get("/api/v1/me/google").json()
    assert view["linked"] and view["email"] == "alice@gmail.com" and view["active_web_sessions"] == 1
    assert "sub" not in view and ALICE_SUB not in json.dumps(view)
    owner_view = env.owner("GET", f"{PREFIX}/accounts/{env.ids[ALICE]}").json()["google"]
    assert owner_view["linked"] and owner_view["email"] == "alice@gmail.com"
    assert ALICE_SUB not in json.dumps(owner_view)


def test_owner_invitation_enrollment_single_use(env):
    code = env.invite(ALICE)
    assert code.startswith("ahg-") and len(code) >= 46
    audit = env.owner("GET", f"{PREFIX}/accounts/audit").text
    assert code not in audit and gs._hash(code) not in audit
    b = env.browser(KITCHEN)
    assert b.get("/api/v1/me").status_code == 401
    r = env.flow(b, ALICE_SUB, ALICE, mode="invite", code=code)
    assert r.headers["location"] == "/#/agents"
    b.refresh_csrf()
    assert me(b).json()["user_id"] == env.ids[ALICE]
    # replay: the code is gone
    r = env.start(env.browser(KITCHEN), "invite", code)
    assert r.status_code == 400 and r.json()["detail"] == gs.INVITE_ERROR


def test_invitation_expiry_replacement_cancel_and_disable(env):
    first = env.invite(ALICE)
    second = env.invite(ALICE)  # replacement
    b = env.browser(KITCHEN)
    assert env.start(b, "invite", first).json()["detail"] == gs.INVITE_ERROR
    assert env.start(b, "invite", second).status_code == 200
    env.clock.advance(gs.INVITE_SECONDS + 1)
    assert env.start(b, "invite", second).json()["detail"] == gs.INVITE_ERROR
    third = env.invite(ALICE)
    assert env.owner("DELETE", f"{PREFIX}/accounts/{env.ids[ALICE]}/google/invitation").status_code == 200
    assert env.start(b, "invite", third).json()["detail"] == gs.INVITE_ERROR
    fourth = env.invite(ALICE)
    env.owner("PATCH", f"{PREFIX}/accounts/{env.ids[ALICE]}", json={"enabled": False})
    assert env.start(b, "invite", fourth).json()["detail"] == gs.INVITE_ERROR
    env.owner("PATCH", f"{PREFIX}/accounts/{env.ids[ALICE]}", json={"enabled": True})
    assert env.start(b, "invite", fourth).json()["detail"] == gs.INVITE_ERROR  # disable invalidated it


def test_invitation_consumed_mid_flow_fails_generically(env):
    code = env.invite(ALICE)
    b = env.browser(KITCHEN)
    r = env.start(b, "invite", code)
    state, gcode = env.google.authorize(r.json()["authorization_url"], ALICE_SUB, ALICE)
    env.owner("DELETE", f"{PREFIX}/accounts/{env.ids[ALICE]}/google/invitation")
    r = env.callback(b, state, gcode)
    assert r.headers["location"] == "/#/signin/failed"
    assert not b.session_cookie()


def test_concurrent_invitation_redemption_links_once(env):
    code = env.invite(ALICE)
    a, b = env.browser(KITCHEN), env.browser(KITCHEN)
    ra, rb = env.start(a, "invite", code), env.start(b, "invite", code)
    sa = env.google.authorize(ra.json()["authorization_url"], ALICE_SUB, ALICE)
    sb = env.google.authorize(rb.json()["authorization_url"], "other-sub", ALICE)
    results = {}

    def run(name, browser, pair):
        results[name] = env.callback(browser, *pair).headers["location"]
    threads = [threading.Thread(target=run, args=("a", a, sa)), threading.Thread(target=run, args=("b", b, sb))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results.values()) == ["/#/agents", "/#/signin/failed"]
    assert env.m.db.google_identity(env.ids[ALICE]) is not None


def test_one_sub_per_member_and_one_member_per_sub(env):
    env.link(ALICE, ALICE_SUB)
    b = env.browser(BOB)
    r = env.flow(b, ALICE_SUB, BOB, mode="link")
    assert r.headers["location"] == "/#/signin/failed"
    assert env.m.db.google_identity(env.ids[BOB]) is None
    r = env.start(env.browser(ALICE), "link")
    assert r.status_code == 409  # unlink first
    assert env.owner("POST", f"{PREFIX}/accounts/{env.ids[ALICE]}/google/invitation").status_code == 409


def test_mutable_email_does_not_change_identity(env):
    env.link(ALICE, ALICE_SUB, "alice@old.example")
    b = env.browser(KITCHEN)
    r = env.flow(b, ALICE_SUB, "alice@new.example")
    assert r.headers["location"] == "/#/agents"
    assert env.m.db.google_identity(env.ids[ALICE])["email"] == "alice@new.example"
    # a different sub with Alice's email is not Alice
    r = env.flow(env.browser(KITCHEN), "someone-else", "alice@new.example")
    assert r.headers["location"] == "/#/signin/failed"


def test_owner_guest_and_unadmitted_cannot_start(env):
    for login in (OWNER, GUEST, STRANGER, None):
        for mode in ("signin", "link"):
            r = env.start(env.browser(login), mode)
            assert r.status_code in (401, 403), (login, mode, r.text)
    code = env.invite(ALICE)
    for login in (OWNER, GUEST, STRANGER, BOB):
        r = env.start(env.browser(login), "invite", code)
        assert r.status_code in (400, 403), (login, r.text)


def test_start_requires_same_origin(env):
    b = env.browser(KITCHEN)
    assert b.post("/api/v1/auth/google/start", json={"mode": "signin"}, origin=False).status_code == 403
    r = b.post("/api/v1/auth/google/start", json={"mode": "signin"},
               headers={"Origin": "https://evil.example.com", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


# --- OIDC verification ----------------------------------------------------------------------------------------

def _other_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.mark.parametrize("extra", [
    {"claims": {"iss": "https://evil.example.com"}},
    {"claims": {"aud": "other.apps.googleusercontent.com", "azp": "other.apps.googleusercontent.com"}},
    {"claims": {"azp": "other.apps.googleusercontent.com"}},
    {"claims": {"aud": [CLIENT_ID, "other"], "azp": None}},
    {"claims": {"nonce": "wrong-nonce"}},
    {"claims": {"nonce": None}},
    {"claims": {"email_verified": False}},
    {"claims": {"email_verified": None}},
    {"claims": {"email": None}},
    {"claims": {"sub": None}},
    {"claims": {"sub": ""}},
    {"claims": {"exp": None}},
    {"claims": {"iat": None}},
    {"key": "other"},
    {"header": {"alg": "HS256"}, "raw": lambda c, h: jwt.encode(c, "secret-key-for-hmac-test-0123456789",
                                                                 algorithm="HS256", headers={"kid": "k1"})},
    {"raw": lambda c, h: jwt.encode(c, None, algorithm="none", headers={"kid": "k1"})},
    {"raw": lambda c, h: "not-a-jwt"},
    {"header": {"kid": "unknown"}},
])
def test_id_token_rejections_are_generic(env, extra):
    env.link(ALICE, ALICE_SUB)
    if extra.get("key") == "other":
        extra = {**extra, "key": _other_key()}
    b = env.browser(KITCHEN)
    r = env.flow(b, ALICE_SUB, ALICE, **extra)
    assert r.status_code == 303 and r.headers["location"] == "/#/signin/failed"
    assert not b.session_cookie()
    assert me(b).status_code == 401


@pytest.mark.parametrize("skew, ok", [
    ({"exp_offset": -(gs.LEEWAY_SECONDS + 5)}, False),
    ({"exp_offset": -(gs.LEEWAY_SECONDS - 30)}, True),
    ({"iat_offset": gs.LEEWAY_SECONDS + 30}, False),
    ({"iat_offset": gs.LEEWAY_SECONDS - 30}, True),
    ({"iat_offset": -(gs.ATTEMPT_SECONDS + gs.LEEWAY_SECONDS + 30)}, False),
])
def test_time_skew(env, skew, ok):
    env.link(ALICE, ALICE_SUB)
    now = int(env.clock())
    claims = {"exp": now + skew.get("exp_offset", 3600), "iat": now + skew.get("iat_offset", 0)}
    r = env.flow(env.browser(KITCHEN), ALICE_SUB, ALICE, claims=claims)
    assert (r.headers["location"] == "/#/agents") is ok


def test_pyjwt_rejects_expired_token_even_if_injected_clock_says_fresh(env):
    env.link(ALICE, ALICE_SUB)
    env.monkeypatch.setattr("jwt.api_jwt.datetime", datetime)  # PyJWT back on the real wall clock
    real = int(time.time())
    env.clock.now = real - 6000  # the injected clock still sees the token as fresh
    claims = {"iat": real - 6000, "exp": real - 3600}
    assert claims["exp"] > env.clock() + gs.LEEWAY_SECONDS  # the manual checks alone would accept it
    r = env.flow(env.browser(KITCHEN), ALICE_SUB, ALICE, claims=claims)
    assert r.headers["location"] == "/#/signin/failed"


def test_jwks_rotation_and_cache_expiry(env):
    env.link(ALICE, ALICE_SUB)
    assert env.google.calls[JWKS_URL] == 1
    env.kitchen_signin()
    assert env.google.calls[JWKS_URL] == 1  # cached
    env.google.keys["k2"] = _other_key()
    env.google.sign_kid = "k2"
    env.clock.advance(gs.JWKS_MIN_REFRESH + 1)
    env.kitchen_signin()
    assert env.google.calls[JWKS_URL] == 2  # unknown kid refetches once
    r = env.flow(env.browser(KITCHEN), ALICE_SUB, ALICE, header={"kid": "k9"})
    assert r.headers["location"] == "/#/signin/failed"
    assert env.google.calls[JWKS_URL] == 2  # rate-limited, no refetch storm
    del env.google.keys["k1"]
    env.clock.advance(3601)
    env.kitchen_signin()
    assert env.google.calls[JWKS_URL] == 3  # max-age expiry
    r = env.flow(env.browser(KITCHEN), ALICE_SUB, ALICE, header={"kid": "k1"}, key=_other_key())
    assert r.headers["location"] == "/#/signin/failed"  # retired key no longer accepted


def test_duplicate_kid_is_ambiguous(env):
    env.link(ALICE, ALICE_SUB)
    original = env.google.handle

    def dup(request):
        resp = original(request)
        if str(request.url) == JWKS_URL:
            keys = resp.json()["keys"]
            return httpx.Response(200, json={"keys": keys + keys})
        return resp
    env.google.handle = dup
    env.m.google_signin.oidc = gs.GoogleOidc(http=httpx.Client(transport=httpx.MockTransport(dup)),
                                             clock=env.clock)
    r = env.flow(env.browser(KITCHEN), ALICE_SUB, ALICE)
    assert r.headers["location"] == "/#/signin/failed"


# --- callback state machine -----------------------------------------------------------------------------------

def test_callback_state_pkce_replay_timeout_denial_and_mixup(env):
    env.link(ALICE, ALICE_SUB)
    b = env.browser(KITCHEN)
    # wrong state
    r = env.start(b, "signin")
    state, code = env.google.authorize(r.json()["authorization_url"], ALICE_SUB, ALICE)
    assert env.callback(b, "x" + state, code).headers["location"] == "/#/signin/failed"
    # the attempt was consumed by the failed callback: the right state no longer works either
    assert env.callback(b, state, code).headers["location"] == "/#/signin/failed"
    # code replay
    r = env.start(b, "signin")
    state, code = env.google.authorize(r.json()["authorization_url"], ALICE_SUB, ALICE)
    assert env.callback(b, state, code).headers["location"] == "/#/agents"
    assert env.callback(b, state, code).headers["location"] == "/#/signin/failed"
    b.refresh_csrf()  # signed in now: starting again is a cookie-authenticated mutation
    # another browser cannot finish this browser's attempt
    other = env.browser(KITCHEN)
    r = env.start(b, "signin")
    state, code = env.google.authorize(r.json()["authorization_url"], ALICE_SUB, ALICE)
    assert env.callback(other, state, code).headers["location"] == "/#/signin/failed"
    # 10-minute deadline
    r = env.start(b, "signin")
    state, code = env.google.authorize(r.json()["authorization_url"], ALICE_SUB, ALICE)
    env.clock.advance(gs.ATTEMPT_SECONDS + 1)
    assert env.callback(b, state, code).headers["location"] == "/#/signin/failed"
    # denial at Google
    r = env.start(b, "signin")
    state, _ = env.google.authorize(r.json()["authorization_url"], ALICE_SUB, ALICE)
    r = b.get(gs.CALLBACK_PATH, params={"state": state, "error": "access_denied"})
    assert r.headers["location"] == "/#/signin/failed"
    # issuer mix-up (RFC 9207 `iss` parameter)
    r = env.start(b, "signin")
    state, code = env.google.authorize(r.json()["authorization_url"], ALICE_SUB, ALICE)
    assert env.callback(b, state, code, iss="https://evil.example.com").headers["location"] == "/#/signin/failed"
    # PKCE: a tampered verifier is refused by the token endpoint
    r = env.start(b, "signin")
    state, code = env.google.authorize(r.json()["authorization_url"], ALICE_SUB, ALICE)
    for attempt in env.m.google_signin._attempts.values():
        attempt.verifier = "tampered" + attempt.verifier
    assert env.callback(b, state, code).headers["location"] == "/#/signin/failed"


def test_callback_login_must_match_start(env):
    env.link(ALICE, ALICE_SUB)
    b = env.browser(KITCHEN)
    r = env.start(b, "signin")
    state, code = env.google.authorize(r.json()["authorization_url"], ALICE_SUB, ALICE)
    b.login = BOB
    assert env.callback(b, state, code).headers["location"] == "/#/signin/failed"


def test_refresh_during_flow_starts_over(env):
    env.link(ALICE, ALICE_SUB)
    b = env.browser(KITCHEN)
    first = env.start(b, "signin")
    s1, c1 = env.google.authorize(first.json()["authorization_url"], ALICE_SUB, ALICE)
    second = env.start(b, "signin")  # page refreshed, user pressed the button again
    s2, c2 = env.google.authorize(second.json()["authorization_url"], ALICE_SUB, ALICE)
    assert env.callback(b, s1, c1).headers["location"] == "/#/signin/failed"
    b2 = env.browser(KITCHEN)
    b2.jar = b.jar
    r = env.start(b2, "signin")
    s3, c3 = env.google.authorize(r.json()["authorization_url"], ALICE_SUB, ALICE)
    assert env.callback(b2, s3, c3).headers["location"] == "/#/agents"


def test_unlinked_google_account_gets_generic_error(env):
    r = env.flow(env.browser(KITCHEN), "nobody", "nobody@example.com")
    assert r.headers["location"] == "/#/signin/failed"
    rows = env.owner("GET", f"{PREFIX}/accounts/audit").json()
    assert any(row["action"] == "google_callback" and row["outcome"] == "denied" for row in rows)


# --- principal precedence -------------------------------------------------------------------------------------

def test_precedence_matrix(env):
    env.link(ALICE, ALICE_SUB)
    env.link(BOB, BOB_SUB)
    kitchen = env.kitchen_signin()   # Alice's Google session on the admitted device
    cookie = kitchen.session_cookie()

    def with_cookie(login, peer_loopback=True):
        b = env.browser(login)
        b.jar.set(gs.SESSION_COOKIE, cookie, domain="tower.tailnet.ts.net")
        if not peer_loopback:
            client = TestClient(env.client.app, base_url=BASE, client=("100.64.0.9", 50000))
            b.client = client
        return b

    # same member: the direct login stands, the session is attached
    r = me(with_cookie(ALICE))
    assert r.json()["user_id"] == env.ids[ALICE]
    # different member: never switches to Alice
    r = me(with_cookie(BOB))
    assert r.json()["user_id"] == env.ids[BOB]
    # owner stays owner; guest stays guest
    assert with_cookie(OWNER).get("/me").json()["role"] == "owner"
    assert with_cookie(GUEST).get("/me").json()["role"] == "guest"
    # unmapped, not admitted: refused, never Alice
    assert me(with_cookie(STRANGER)).status_code == 403
    # admitted device: Alice through Google
    assert me(with_cookie(KITCHEN)).json()["user_id"] == env.ids[ALICE]
    # absent login with a cookie: never the localhost owner
    r = with_cookie(None).get("/me")
    assert r.status_code == 403 and r.json()["detail"] == gs.COOKIE_WITHOUT_TAILSCALE
    # forged Tailscale header from a non-loopback peer: not admitted, the cookie is ignored
    assert me(with_cookie(KITCHEN, peer_loopback=False)).status_code == 403
    # no cookie anywhere: #62 behavior unchanged
    assert me(env.browser(ALICE)).json()["user_id"] == env.ids[ALICE]
    assert env.browser(None).get("/me").json()["role"] == "owner"
    assert env.browser(KITCHEN).get("/me").status_code == 401


def test_pre_auth_reaches_only_shell_and_sign_in(env):
    b = env.browser(KITCHEN)
    assert b.get("/").status_code == 200
    assert b.get("/app.js").status_code == 200
    assert b.get("/health").status_code == 200
    body = b.get("/api/v1/auth/session").json()
    assert body == {"google": {"available": True, "explanation": gs.EXPLANATION}, "admitted": True,
                    "signed_in": False, "role": None, "csrf": None}
    for path in ("/api/v1/me", "/api/v1/projects", "/sessions", f"{PREFIX}/accounts", "/me"):
        r = b.get(path)
        assert r.status_code == 401 and r.json()["error"]["code"] == "sign_in_required", path
    assert env.browser(OWNER).get("/api/v1/auth/session").json()["google"]["available"] is False


# --- sessions -------------------------------------------------------------------------------------------------

def test_csrf_and_origin_required_for_cookie_mutations(env):
    env.link(ALICE, ALICE_SUB)
    b = env.kitchen_signin()
    csrf = b.csrf
    assert csrf and csrf != b.session_cookie()
    b.csrf = ""
    r = b.post("/api/v1/projects", json={"name": "nocsrf"})
    assert r.status_code == 403 and "CSRF" in r.json()["detail"]
    b.csrf = "wrong"
    assert b.post("/api/v1/projects", json={"name": "badcsrf"}).status_code == 403
    b.csrf = csrf
    # a foreign Origin drops the cookie entirely (sign-in required); a missing one fails the CSRF check
    assert b.post("/api/v1/projects", json={"name": "badorigin"},
                  headers={"Origin": "https://evil.example.com"}).status_code == 401
    assert b.post("/api/v1/projects", json={"name": "badsite"},
                  headers={"Sec-Fetch-Site": "same-site"}).status_code == 401
    assert b.post("/api/v1/projects", json={"name": "noorigin"}, origin=False).status_code == 403
    r = b.post("/api/v1/projects", json={"name": "ok"})
    assert r.status_code in (200, 201), r.text


def test_cross_origin_requests_cannot_use_the_cookie(env):
    env.link(ALICE, ALICE_SUB)
    b = env.kitchen_signin()
    for headers in ({"Origin": "https://app.example.com"}, {"Sec-Fetch-Site": "cross-site"},
                    {"Origin": "https://tower.tailnet.ts.net:8443"}):
        r = b.get("/api/v1/me", headers=headers)
        assert r.status_code == 401, headers
        assert "access-control-allow-credentials" not in r.headers
    r = b.request("OPTIONS", "/api/v1/me", headers={"Origin": "https://app.example.com",
                                                     "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-credentials" not in r.headers


def test_logout_revokes_and_closes_streams(env):
    env.link(ALICE, ALICE_SUB)
    b = env.kitchen_signin()
    epoch = env.m.stream_epoch.get(env.ids[ALICE], 0)
    saved = b.session_cookie()
    b.csrf = ""
    assert b.post("/api/v1/auth/logout").status_code == 403  # CSRF
    b.refresh_csrf()
    r = b.post("/api/v1/auth/logout")
    assert r.status_code == 200
    assert env.m.stream_epoch[env.ids[ALICE]] == epoch + 1
    assert me(b).status_code == 401
    stale = env.browser(KITCHEN)
    stale.jar.set(gs.SESSION_COOKIE, saved, domain="tower.tailnet.ts.net")
    assert me(stale).status_code == 401


def test_idle_and_absolute_expiry(env):
    env.link(ALICE, ALICE_SUB)
    b = env.kitchen_signin()
    env.clock.advance(gs.IDLE_SECONDS - 10)
    assert me(b).status_code == 200  # touched
    env.clock.advance(gs.IDLE_SECONDS - 10)
    assert me(b).status_code == 200
    env.clock.advance(gs.IDLE_SECONDS + 1)
    assert me(b).status_code == 401  # idle
    b = env.kitchen_signin()
    created = env.m.db.web_session(gs._hash(b.session_cookie()))["created_at"]
    for _ in range(4):
        env.clock.advance(gs.IDLE_SECONDS - 60)
        assert me(b).status_code == 200
    env.clock.advance(gs.IDLE_SECONDS - 60)
    assert env.clock() - created > gs.ABSOLUTE_SECONDS
    assert me(b).status_code == 401  # absolute


def test_touch_is_throttled(env):
    env.link(ALICE, ALICE_SUB)
    b = env.kitchen_signin()
    h = gs._hash(b.session_cookie())
    before = env.m.db.web_session(h)["last_seen_at"]
    env.clock.advance(10)
    me(b)
    assert env.m.db.web_session(h)["last_seen_at"] == before
    env.clock.advance(gs.TOUCH_SECONDS)
    me(b)
    assert env.m.db.web_session(h)["last_seen_at"] > before


def test_sign_in_regenerates_session(env):
    env.link(ALICE, ALICE_SUB)
    b = env.kitchen_signin()
    old = b.session_cookie()
    env.flow(b, ALICE_SUB, ALICE)
    assert b.session_cookie() != old
    assert env.m.db.web_session(gs._hash(old))["revoked_at"] is not None


def test_member_unlink_and_relink(env):
    env.link(ALICE, ALICE_SUB)
    b = env.kitchen_signin()
    view = b.get("/api/v1/me/google").json()
    assert view["signed_in_with_google"] and view["unlink_removes_this_device"]
    assert b.delete("/api/v1/me/google", json={"confirm": False}).status_code == 400
    r = b.delete("/api/v1/me/google", json={"confirm": True})
    assert r.status_code == 200 and not r.json()["linked"]
    assert me(b).status_code == 401
    # member data and direct login survive
    assert me(env.browser(ALICE)).json()["user_id"] == env.ids[ALICE]
    env.link(ALICE, "new-sub-for-alice")
    assert env.flow(env.browser(KITCHEN), ALICE_SUB, ALICE).headers["location"] == "/#/signin/failed"
    assert env.flow(env.browser(KITCHEN), "new-sub-for-alice", ALICE).headers["location"] == "/#/agents"


def test_owner_revoke_all_and_unlink(env):
    env.link(ALICE, ALICE_SUB)
    b1, b2 = env.kitchen_signin(), env.kitchen_signin()
    assert env.owner("GET", f"{PREFIX}/accounts/{env.ids[ALICE]}").json()["google"]["active_web_sessions"] == 3
    epoch = env.m.stream_epoch.get(env.ids[ALICE], 0)
    r = env.owner("POST", f"{PREFIX}/accounts/{env.ids[ALICE]}/google/revoke-sessions")
    assert r.json()["revoked"] == 3 and env.m.stream_epoch[env.ids[ALICE]] == epoch + 1
    assert me(b1).status_code == 401 and me(b2).status_code == 401
    b3 = env.kitchen_signin()
    assert env.owner("DELETE", f"{PREFIX}/accounts/{env.ids[ALICE]}/google", json={"confirm": True}).status_code == 200
    assert me(b3).status_code == 401
    assert env.m.db.google_identity(env.ids[ALICE]) is None
    assert me(env.browser(ALICE)).status_code == 200


def test_owner_actions_require_owner(env):
    env.link(ALICE, ALICE_SUB)
    b = env.kitchen_signin()
    for method, path in (("POST", f"{PREFIX}/accounts/{env.ids[BOB]}/google/invitation"),
                         ("POST", f"{PREFIX}/accounts/{env.ids[BOB]}/google/revoke-sessions"),
                         ("GET", f"{PREFIX}/google-signin")):
        assert b.request(method, path).status_code == 403
        assert env.browser(BOB).request(method, path).status_code == 403
    assert env.owner("POST", f"{PREFIX}/accounts/u-nope/google/invitation").status_code == 404


def test_disable_reenable_and_rebind_revoke_sessions(env):
    env.link(ALICE, ALICE_SUB)
    b = env.kitchen_signin()
    env.owner("PATCH", f"{PREFIX}/accounts/{env.ids[ALICE]}", json={"enabled": False})
    assert me(b).status_code in (401, 403)
    assert env.flow(env.browser(KITCHEN), ALICE_SUB, ALICE).headers["location"] == "/#/signin/failed"
    env.owner("PATCH", f"{PREFIX}/accounts/{env.ids[ALICE]}", json={"enabled": True})
    assert me(b).status_code == 401  # revoked sessions stay revoked
    b = env.kitchen_signin()
    assert me(b).status_code == 200
    env.owner("PATCH", f"{PREFIX}/accounts/{env.ids[ALICE]}", json={"login": "alice2@example.com"})
    assert me(b).status_code == 401
    r = env.owner("POST", f"{PREFIX}/accounts", json={"login": KITCHEN, "display_name": "Kitchen"})
    assert r.status_code == 400  # an admitted-only login cannot become a member


def test_daemon_restart_keeps_sessions_and_drops_attempts(env):
    env.link(ALICE, ALICE_SUB)
    b = env.kitchen_signin()
    pending = env.browser(KITCHEN)
    r = env.start(pending, "signin")
    state, code = env.google.authorize(r.json()["authorization_url"], ALICE_SUB, ALICE)
    env.close()
    env.m.db.close()
    env.m = Manager(env.cfg, chat=Script([Completion(content="done")]))
    env._wire(env.m)
    env.client = TestClient(create_app(env.m), base_url=BASE, client=("127.0.0.1", 50000))
    env.client.__enter__()
    b.client = pending.client = env.client
    assert me(b).json()["user_id"] == env.ids[ALICE]
    assert env.callback(pending, state, code).headers["location"] == "/#/signin/failed"


# --- no secrets at rest or in output --------------------------------------------------------------------------

def test_no_secret_material_in_storage_logs_or_responses(env, caplog):
    caplog.set_level(logging.DEBUG)
    responses = []
    code = env.invite(ALICE)
    b = env.browser(KITCHEN)
    r = env.start(b, "invite", code)
    responses.append(r.text)
    url = r.json()["authorization_url"]
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    state, gcode = env.google.authorize(url, ALICE_SUB, ALICE)
    attempt = next(iter(env.m.google_signin._attempts.values()))
    verifier = attempt.verifier
    r = env.callback(b, state, gcode)
    responses.append(r.text + json.dumps(dict(r.headers)))
    session = b.session_cookie()
    responses.append(b.refresh_csrf().__repr__())
    responses.append(b.get("/api/v1/me/google").text)
    responses.append(env.owner("GET", f"{PREFIX}/accounts").text)
    responses.append(env.owner("GET", f"{PREFIX}/accounts/audit").text)
    responses.append(env.owner("GET", f"{PREFIX}/google-signin", params={"refresh": "true"}).text)
    secrets_ = [SECRET, code, gcode, state, q["nonce"], verifier, ACCESS_TOKEN, session, *env.google.issued]
    dump = "\n".join(sqlite3.connect(env.cfg.db_path).iterdump())
    # The test client's own httpx log prints request URLs; the daemon's uvicorn access log is redacted (see below).
    logs = "\n".join(rec.getMessage() for rec in caplog.records if rec.name != "httpx")
    audit = env.owner("GET", f"{PREFIX}/accounts/audit").text
    for value in secrets_:
        assert value not in dump
        assert value not in logs
        assert value not in "".join(responses[2:])
    for value in (SECRET, code, gcode, verifier, ACCESS_TOKEN, *env.google.issued):
        assert value not in "".join(responses[:2])
    for value in (ALICE_SUB, *env.google.issued):
        assert value not in audit
        assert value not in "".join(responses)
    assert gs._hash(code) not in audit


def test_access_log_redacts_callback_query():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:1", "GET", gs.CALLBACK_PATH + "?state=s3cr3t&code=4/abc", "1.1", 303),
                               None)
    gs.RedactCallbackQuery().filter(record)
    text = record.getMessage()
    assert "s3cr3t" not in text and "4/abc" not in text and gs.CALLBACK_PATH in text


def test_web_bundle_keeps_csrf_in_memory_only():
    from pathlib import Path
    web = Path(gs.__file__).parent / "web"
    client_js = (web / "client.mjs").read_text(encoding="utf-8")
    app_js = (web / "app.js").read_text(encoding="utf-8")
    for source in (client_js, app_js):
        for line in source.splitlines():
            if "csrf" in line.lower():
                assert "localStorage" not in line and "sessionStorage" not in line and "setItem" not in line
    assert "X-Agent-Harness-CSRF" in client_js
