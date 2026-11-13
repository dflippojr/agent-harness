"""Request bodies the daemon parses by hand get the route's own 4xx, never a 500 (#965, following #930).

Two checks:
- every route that reads its JSON body by hand answers a set of malformed or deeply nested bodies exactly as it answers
  a plain non-JSON body (same status, same error code), with nothing changed;
- an audit of the source finds every request-body JSON parse in `harness/` and `harness_modules/` and requires that it
  catches the #930 failure set (`ValueError` and `RecursionError`), so a new hand-parsed route is held to it too.

Synthetic data only: made-up members, names and verifiers generated here."""
from __future__ import annotations

import ast
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness import pairing_requests as pr
from test_hub_claim import CLAIM, _audit, _host
from test_household import ALICE, H
from test_member_keys import CANARY, KEYS, setup

ROOT = Path(__file__).resolve().parent.parent
DEEP = 100_000   # the #930 shape: deep enough that the parser itself gives up
DEPTHS = (500, 900, 1400, 5000, DEEP)   # some parse fine and reach the route's code; the deepest never parse

HOSTILE_BODIES = {  # the #930 table: each is a body json.loads rejects or that isn't an object
    "nested arrays": b"[" * DEEP + b"]" * DEEP,
    "nested objects": b'{"a":' * DEEP + b"1" + b"}" * DEEP,
    "unterminated nesting": b"[" * DEEP,
    "not JSON": b"invalid",
    "invalid UTF-8": b'{"a": "\xff\xfe"}',
    "huge integer": b'{"a": ' + b"9" * 5000 + b"}",
    "array": b"[1]",
    "string": b'"text"',
    "null": b"null",
}


def _nested(depth: int) -> bytes:
    return b"[" * depth + b"]" * depth


def _hostile(fields: tuple[str, ...], base: bytes) -> dict[str, bytes]:
    """The #930 table plus, for each field the route reads, a value nested at each depth in DEPTHS."""
    bodies = dict(HOSTILE_BODIES)
    for field in fields:
        for depth in DEPTHS:
            bodies[f"{field} nested {depth}"] = b"{" + base + b'"' + field.encode() + b'":' + _nested(depth) + b"}"
    return bodies


def _answer(response) -> tuple[int, str]:
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    return response.status_code, error.get("code", "") if isinstance(error, dict) else ""


def _app_headers(client) -> dict:
    app = client.post("/keys", json={"name": "planner", "kind": "app", "scopes": ["sessions"]})
    assert app.status_code == 201, app.text
    return {"Authorization": f"Bearer {app.json()['key']}"}


def _pending_hub_request(client) -> str:
    verifier = secrets.token_urlsafe(48)
    asked = client.post("/api/v1/pair/requests", json={"name": "Example Hub", "kind": "hub",
                                                        "code_challenge": pr.challenge_of(verifier)})
    assert asked.status_code == 201, asked.text
    return asked.json()["id"]


def _owner_keys(m) -> int:
    return len(m.db.list_api_keys())


# Each route: (method, path, headers, the fields it reads, the fixed fields sent beside the nested one, the baseline
# body and what it answers, and a check that nothing changed). Built inside the household so ids and keys are real.
def _member_key_set(client, m, ids):
    return dict(method="PUT", path=f"{KEYS}/claude", headers=H(ALICE), fields=("key",), base=b"",
                baseline=b"invalid", unchanged=lambda: not m.member_keys.has(ids[ALICE], "claude"))


def _end_user_login_code(client, m, _ids):
    return dict(method="POST", path="/api/v1/end-users/dana/logins/claude/no-such-attempt/code",
                headers=_app_headers(client), fields=("code",), base=b"", baseline=b"invalid",
                unchanged=lambda: True)


def _pairing_token(client, m, _ids):
    rid = _pending_hub_request(client)
    return dict(method="POST", path=f"/api/v1/pair/requests/{rid}/token", headers={}, fields=("code_verifier",),
                base=b"", baseline=b"invalid",
                unchanged=lambda: m.db.get_pairing_request(rid)["state"] == "pending")


def _hub_claim_approve(client, m, _ids):
    rid = _pending_hub_request(client)
    return dict(method="POST", path=f"{CLAIM}/requests/{rid}/approve", headers=_host(m), fields=("match",),
                base=b"", baseline=b"invalid", audited="hub.claim.approve",
                unchanged=lambda: m.db.get_pairing_request(rid)["state"] == "pending")


def _hub_claim_release(client, m, _ids):
    return dict(method="POST", path=f"{CLAIM}/release", headers=_host(m), fields=("confirm",), base=b"",
                baseline=b"invalid", audited="hub.release", unchanged=lambda: True)


def _create_key(client, m, _ids):
    before = _owner_keys(m)
    # A body that names no key is the spec's own 400 (`invalid_request`); a malformed body gets the same answer.
    return dict(method="POST", path="/keys", headers={}, fields=("name", "scopes", "origins", "catalog_app_id"),
                base=b'"name": "probe", ', baseline=b"{}", audited="key.create",
                unchanged=lambda: _owner_keys(m) == before)


ROUTES = {
    "apps: PUT /api/v1/me/api-keys/{backend}": _member_key_set,
    "apps: POST /api/v1/end-users/{end_user}/logins/{backend}/{attempt_id}/code": _end_user_login_code,
    "pairing_requests: POST /api/v1/pair/requests/{rid}/token": _pairing_token,
    "hub_claim: POST /hub-claim/requests/{rid}/approve": _hub_claim_approve,
    "hub_claim: POST /hub-claim/release": _hub_claim_release,
    "api: POST /keys": _create_key,
}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_hostile_bodies_get_the_routes_own_4xx(tmp_path, monkeypatch, route):
    """#965: each hand-parsed route answers every hostile body as it answers a plain malformed one, never with a 5xx,
    and changes nothing. Routes that audit a bad body (Hub claim, key creation) write an `invalid_request` row for it."""
    with setup(tmp_path, monkeypatch) as (owner_client, m, ids):
        spec = ROUTES[route](owner_client, m, ids)
        # Not entered: the household's own client already started the app. This one reports a server error as a
        # 500 instead of raising it, so the test sees what a caller would.
        client = TestClient(owner_client.app, raise_server_exceptions=False)
        if "audited" in spec:
            denied_before = len(_audit(owner_client, spec["audited"], outcome="denied"))

        def send(content: bytes):
            return client.request(spec["method"], spec["path"], headers={**spec["headers"],
                                  "Content-Type": "application/json"}, content=content)

        expected = _answer(send(spec["baseline"]))
        assert 400 <= expected[0] < 500 and expected[1], (route, expected)
        bodies = _hostile(spec["fields"], spec["base"])
        wrong = {name: (r.status_code, r.text[:120]) for name, content in bodies.items()
                 if _answer(r := send(content)) != expected}
        assert wrong == {}, f"expected {expected} for every body"
        assert spec["unchanged"]()
        if "audited" in spec:
            rows = _audit(owner_client, spec["audited"], outcome="denied")
            assert len(rows) - denied_before == len(bodies) + 1   # each hostile body, and the baseline
            assert {r["metadata"]["reason"] for r in rows} == {"invalid_request"}


def test_valid_bodies_still_work(tmp_path, monkeypatch):
    """No change for a well-formed body: a member stores a key, the owner mints one, a Hub's request is approved."""
    with setup(tmp_path, monkeypatch) as (client, m, ids):
        assert client.put(f"{KEYS}/claude", json={"key": CANARY}, headers=H(ALICE)).status_code == 200
        assert m.member_keys.has(ids[ALICE], "claude")
        made = client.post("/keys", json={"name": "probe", "scopes": ["inference"]})
        assert made.status_code == 201 and made.json()["key"], made.text
        verifier = secrets.token_urlsafe(48)
        asked = client.post("/api/v1/pair/requests", json={"name": "Example Hub", "kind": "hub",
                                                            "code_challenge": pr.challenge_of(verifier)}).json()
        approved = client.post(f"{CLAIM}/requests/{asked['id']}/approve", headers=_host(m),
                               json={"match": asked["match_code"]})
        assert approved.status_code == 200, approved.text
        assert client.post(f"/api/v1/pair/requests/{asked['id']}/token",
                           json={"code_verifier": verifier}).status_code == 201


# --- class-level check: every request-body JSON parse catches the #930 failure set ---------------------------------

FAILURE_SET = {"ValueError", "RecursionError"}
SHARED_GUARD = "BODY_PARSE_ERRORS"   # harness.request_body's tuple of the failure set
HELPER = "json_body"                 # harness.request_body's guarded `await request.json()`
# Every hand-parsed request body known when #965 landed. The audit must find each one (directly or through the
# shared helper), so a refactor that hides a parse from it fails here instead of passing by finding nothing.
KNOWN_SITES = {
    ("harness/apps.py", "api_member_key_set"),
    ("harness/apps.py", "submit_end_user_login_code"),
    ("harness/pairing_requests.py", "redeem_pairing_request"),
    ("harness/hub_claim.py", "_parse"),
    ("harness/api.py", "create_key"),
    ("harness_modules/endpoint/service.py", "_parse_body"),
}


def _is_request_annotation(node) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "Request"
    if isinstance(node, ast.Attribute):
        return node.attr == "Request"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.split(".")[-1] == "Request"
    return False


def _caught(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return {"BaseException"}
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    nodes = [n.value if isinstance(n, ast.Starred) else n for n in nodes]   # (*BODY_PARSE_ERRORS, Other)
    names = {n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else "" for n in nodes}
    return names | (FAILURE_SET if SHARED_GUARD in names else set())


class _BodyParses(ast.NodeVisitor):
    """Finds, per function with a `Request` parameter: `<request>.json()`, `json.loads(...)`, `<request>.body()` and
    calls to the shared helper, each with whether an enclosing `try` catches the whole failure set."""

    def __init__(self, path: str):
        self.path = path
        self.functions: list[tuple[str, set[str]]] = []   # (name, request parameter names)
        self.tries: list[set[str]] = []                   # handler names of each enclosing try body
        self.parses: list[tuple[str, str, int, bool]] = []   # (function, kind, line, guarded)
        self.reads: list[tuple[str, int]] = []                # raw body reads: (function, line)

    def _function(self, node):
        args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        params = {a.arg for a in args if (a.annotation is not None and _is_request_annotation(a.annotation))
                  or (a.annotation is None and a.arg in ("request", "req"))}   # an unannotated helper too
        self.functions.append((node.name, params))
        saved, self.tries = self.tries, []
        self.generic_visit(node)
        self.tries = saved
        self.functions.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def visit_Try(self, node):
        caught = set().union(*(_caught(h) for h in node.handlers))
        self.tries.append(caught)
        for child in node.body:
            self.visit(child)
        self.tries.pop()
        for child in [*node.handlers, *node.orelse, *node.finalbody]:
            self.visit(child)

    visit_TryStar = visit_Try

    def _guarded(self) -> bool:
        caught = set().union(*self.tries) if self.tries else set()
        return FAILURE_SET <= caught or bool(caught & {"Exception", "BaseException"})

    def visit_Call(self, node):
        if self.functions and (params := self.functions[-1][1]):
            name = self.functions[-1][0]
            func = node.func
            on_request = (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                          and func.value.id in params)
            if on_request and func.attr == "json":
                self.parses.append((name, "request.json()", node.lineno, self._guarded()))
            elif on_request and func.attr == "body":
                self.reads.append((name, node.lineno))
            elif (isinstance(func, ast.Attribute) and func.attr in ("loads", "load")
                  and isinstance(func.value, ast.Name) and func.value.id == "json"):
                self.parses.append((name, "json.loads", node.lineno, self._guarded()))
            elif (isinstance(func, ast.Name) and func.id == HELPER) or (
                    isinstance(func, ast.Attribute) and func.attr == HELPER):
                self.parses.append((name, HELPER, node.lineno, True))
        self.generic_visit(node)


def _scan() -> tuple[list[tuple[str, str, str, int, bool]], list[tuple[str, str, int]]]:
    parses, reads = [], []
    for top in ("harness", "harness_modules"):
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            visitor = _BodyParses(rel)
            visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=rel))
            parses += [(rel, *p) for p in visitor.parses]
            reads += [(rel, *r) for r in visitor.reads]
    return parses, reads


def test_every_request_body_parse_catches_the_930_failure_set():
    """Escaped-defect class check (#965): a request-body JSON parse that catches only `ValueError` lets a deeply nested
    body's `RecursionError` become a 500. Every parse in a function that takes a `Request` must sit in a `try` that
    catches `ValueError` and `RecursionError` (or use the shared helper), and every raw body read must be parsed
    under that guard in the same function."""
    from harness.request_body import BODY_PARSE_ERRORS   # the audit trusts this name, so check what it holds
    assert {e.__name__ for e in BODY_PARSE_ERRORS} == FAILURE_SET
    parses, reads = _scan()
    unguarded = [f"{path}:{line} {fn}: {kind}" for path, fn, kind, line, guarded in parses if not guarded]
    assert unguarded == [], "request-body parses that don't catch ValueError and RecursionError"
    guarded_in = {(path, fn) for path, fn, kind, _line, guarded in parses if kind == "json.loads" and guarded}
    unparsed = [f"{path}:{line} {fn}" for path, fn, line in reads if (path, fn) not in guarded_in]
    assert unparsed == [], "raw request-body reads with no guarded json.loads in the same function"
    found = {(path, fn) for path, fn, *_ in parses}
    assert KNOWN_SITES <= found, f"the audit no longer sees {sorted(KNOWN_SITES - found)}"


def test_the_audit_flags_a_parse_that_catches_only_value_error():
    """The audit itself: the #930 shape is flagged, the fixed shapes pass."""
    def scan(source: str) -> list[bool]:
        visitor = _BodyParses("example.py")
        visitor.visit(ast.parse(source))
        return [guarded for *_rest, guarded in visitor.parses]

    old = "async def f(request: Request):\n    try:\n        return await request.json()\n    except ValueError:\n        pass\n"
    assert scan(old) == [False]
    assert scan(old.replace("except ValueError", "except (ValueError, RecursionError)")) == [True]
    assert scan(old.replace("except ValueError", "except BODY_PARSE_ERRORS")) == [True]
    assert scan(old.replace("except ValueError", "except (*BODY_PARSE_ERRORS, ValidationError)")) == [True]
    assert scan(old.replace("except ValueError", "except (ValueError, ValidationError)")) == [False]
    assert scan("async def f(request: Request):\n    return json.loads(await request.body())\n") == [False]
    assert scan("async def f(request: Request):\n    return await json_body(request)\n") == [True]
    assert scan("def f(text):\n    return json.loads(text)\n") == []   # not a request body
    assert scan("async def _read(request):\n    return json.loads(await request.body())\n") == [False]
