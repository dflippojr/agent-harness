"""Python client for the agent-harness app API (/api/v1). One file, depends only on httpx.

    from harness_client import Harness, tool

    @tool("Add an item to the shopping list", item={"type": "string"})
    def add_item(item: str) -> str:
        shopping.append(item)
        return f"added {item}"

    h = Harness("https://tower.your-tailnet.ts.net", token="ha-...")
    result = h.run("Plan dinner for four and add what I need to the list",
                   context={"Pantry": "rice, eggs, olive oil"}, tools=[add_item])
    print(result.answer)

`run` creates a session, answers the agent's calls to your tools as they arrive, and returns when the session ends.
An App pairs without the owner ever handling its token (#519): `Harness.request_pairing(...)` (or `claim_pairing` for
a slot armed from the Hub) holds the PKCE verifier, the App shows its `match_code`, and `Harness.redeem_pairing(...)`
waits for the owner's approval and returns a client with the token. The standalone Hub claims the daemon the same way
with `Harness.request_hub_claim(...)` (#543); the owner approves it on the daemon host with `harness hub approve`. Everything else (pair, capabilities, backends, create_session, events, send, add_context, approvals, cancel,
submit_tool_result, generate_image, upscale_image) is a thin wrapper over the HTTP API described in docs/app-api.md. Run
`Harness.validate_openapi()` in an integration check to detect client/server contract drift.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, TypedDict

import httpx

TERMINAL = ("done", "failed", "cancelled")
SDK_API_MAJOR = "1"
JSON_MEDIA_TYPE = "application/json"

# Used by validate_openapi() and CI. Paths use the server's OpenAPI templates, not formatted runtime ids.
SDK_OPERATIONS = {
    "audit": ("get", "/api/v1/audit"),
    "info": ("get", "/api/v1"), "pair": ("post", "/api/v1/pair"),
    "request_pairing": ("post", "/api/v1/pair/requests"),
    "claim_pairing": ("post", "/api/v1/pair/requests/{rid}/claim"),
    "redeem_pairing": ("post", "/api/v1/pair/requests/{rid}/token"),
    "backends": ("get", "/api/v1/backends"), "create_session": ("post", "/api/v1/sessions"),
    "sessions": ("get", "/api/v1/sessions"), "session": ("get", "/api/v1/sessions/{ref}"),
    "send": ("post", "/api/v1/sessions/{ref}/messages"),
    "add_context": ("post", "/api/v1/sessions/{ref}/context"),
    "cancel": ("post", "/api/v1/sessions/{ref}/cancel"),
    "pending_tool_calls": ("get", "/api/v1/sessions/{ref}/tool_calls"),
    "submit_tool_result": ("post", "/api/v1/sessions/{ref}/tool_calls/{call_id}"),
    "pending_approvals": ("get", "/api/v1/sessions/{ref}/approvals"),
    "decide_approval": ("post", "/api/v1/sessions/{ref}/approvals/{approval_id}"),
    "events": ("get", "/api/v1/sessions/{ref}/events"),
    "generate_image": ("post", "/api/v1/images"),
    "upscale_image": ("post", "/api/v1/images/{iid}/upscale"),
}
# Keyed off SDK_OPERATIONS so a path only ever spells itself once, in the table above.
SDK_REQUEST_FIELDS = {
    SDK_OPERATIONS["pair"]: {"code"},
    SDK_OPERATIONS["request_pairing"]: {"name", "kind", "scopes", "catalog_app_id", "code_challenge",
                                        "code_challenge_method"},
    SDK_OPERATIONS["claim_pairing"]: {"code_challenge", "code_challenge_method"},
    SDK_OPERATIONS["redeem_pairing"]: {"code_verifier"},
    SDK_OPERATIONS["create_session"]: {"prompt", "project", "backend", "model", "title", "context", "tools",
                                       "metadata", "tools_only", "retention_days", "end_user"},
    SDK_OPERATIONS["send"]: {"content"},
    SDK_OPERATIONS["add_context"]: {"context"},
    SDK_OPERATIONS["submit_tool_result"]: {"output", "ok"},
    SDK_OPERATIONS["decide_approval"]: {"decision", "note"},
    SDK_OPERATIONS["generate_image"]: {"prompt", "model", "aspect_ratio", "upscale"},
    SDK_OPERATIONS["upscale_image"]: {"upscale"},
}


class PairedApp(TypedDict, total=False):
    id: str
    name: str
    prefix: str
    created_at: float
    scopes: str
    kind: str
    origins: list[str]
    catalog_app_id: str
    role: str


class PairingRequestStatus(TypedDict, total=False):
    id: str
    state: str
    match_code: str
    expires_at: float
    browser: bool


class Capabilities(TypedDict, total=False):
    profile: str
    required: dict[str, bool]
    modules: dict[str, bool]
    hosted_backends: list[str]


class BackendStatus(TypedDict, total=False):
    name: str
    available: bool
    logged_in: bool
    auth: str
    billing: str
    model: str
    effort: str
    limits: dict
    today: dict
    week: dict
    notice: str
    billing_warning: str
    usage_by_source: dict[str, dict]
    provider_policy: dict | None


class ProviderFailure(TypedDict, total=False):
    code: str
    provider: str
    message: str
    retryable: bool


class Session(TypedDict, total=False):
    id: str
    project: str
    target: str
    backend: str
    model: str
    title: str
    status: str
    stop_reason: str
    created_at: float
    updated_at: float
    answer: str
    totals: dict
    run: dict
    failure: ProviderFailure | None
    last_event_seq: int
    app_tools: list[str]
    metadata: dict


class Event(TypedDict, total=False):
    seq: int | None
    session_id: str
    ts: float
    type: str
    data: dict


class Approval(TypedDict, total=False):
    id: str
    tool: str
    args: dict
    reason: str
    detail: str
    status: str


SDK_RESPONSE_TYPES = {
    SDK_OPERATIONS["backends"]: ("200", BackendStatus, True),
    SDK_OPERATIONS["create_session"]: ("201", Session, False),
    SDK_OPERATIONS["sessions"]: ("200", Session, True),
    SDK_OPERATIONS["session"]: ("200", Session, False),
    SDK_OPERATIONS["send"]: ("200", Session, False),
    SDK_OPERATIONS["add_context"]: ("200", Session, False),
    SDK_OPERATIONS["cancel"]: ("200", Session, False),
    SDK_OPERATIONS["pending_approvals"]: ("200", Approval, True),
    SDK_OPERATIONS["decide_approval"]: ("200", Approval, False),
    SDK_OPERATIONS["request_pairing"]: ("201", PairingRequestStatus, False),
    SDK_OPERATIONS["claim_pairing"]: ("200", PairingRequestStatus, False),
}
# Objects nested in a response: (operation, property) -> (status, TypedDict).
SDK_NESTED_RESPONSE_TYPES = {
    (SDK_OPERATIONS["pair"], "app"): ("201", PairedApp),
    (SDK_OPERATIONS["redeem_pairing"], "app"): ("201", PairedApp),
}


@dataclass
class PairingRequest:
    """A zero-touch pairing in progress (#519). Show `match_code` to the user, then pass it to
    `Harness.redeem_pairing`. It holds the PKCE verifier, which is sent only to redeem the token."""
    base_url: str
    id: str
    state: str
    match_code: str
    expires_at: float
    origin: str = ""
    code_verifier: str = field(default="", repr=False)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]
    timeout_seconds: int = 600

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters,
                "timeout_seconds": self.timeout_seconds}


def tool(description: str, required: list[str] | None = None, timeout_seconds: int = 600, **properties: dict):
    """Decorator: turn a function into a Tool. Keyword arguments are JSON Schema properties; all are required
    unless `required` says otherwise."""
    def wrap(fn: Callable[..., Any]) -> Tool:
        return Tool(fn.__name__, description, {"type": "object", "properties": properties,
                                               "required": list(properties) if required is None else required},
                    fn, timeout_seconds)
    return wrap


@dataclass
class RunResult:
    session: Session
    events: list[Event] = field(default_factory=list)

    @property
    def answer(self) -> str:
        return self.session.get("answer") or ""

    @property
    def status(self) -> str:
        return self.session.get("status", "")

    @property
    def usage(self) -> dict:
        return self.session.get("totals") or {}

    @property
    def limits(self) -> dict:
        latest = next((e["data"] for e in reversed(self.events) if e.get("type") == "rate_limit"), None)
        return latest or ((self.session.get("run") or {}).get("rate_limits") or {})

    @property
    def billing_notices(self) -> list[str]:
        return [str(e.get("data", {}).get("message") or "") for e in self.events
                if e.get("type") == "billing_warning"]

    @property
    def errors(self) -> list[dict]:
        return [e.get("data") or {} for e in self.events if e.get("type") == "error"]

    @property
    def failure(self) -> ProviderFailure | None:
        return self.session.get("failure")


class HarnessError(Exception):
    def __init__(self, status: int, detail: str, code: str = ""):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail
        self.code = code or {400: "invalid_request", 401: "authentication_required", 403: "forbidden",
                             404: "not_found", 409: "conflict", 413: "payload_too_large",
                             429: "rate_limited"}.get(status, "server_error" if status >= 500 else "http_error")
        self.retryable = status == 429 or status >= 500


class ContractError(Exception):
    pass


class Harness:
    def __init__(self, base_url: str, token: str = "", timeout: float = 60, origin: str = ""):
        self.base = base_url.rstrip("/")
        self.token = token
        self.origin = origin
        self.audit_warning = ""
        self.paired_app: PairedApp | None = None  # set by pair(): the key it minted, without the secret
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if origin:
            headers["Origin"] = origin
        self.client = httpx.Client(base_url=self.base, timeout=timeout, headers=headers)

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @classmethod
    def pair(cls, base_url: str, code: str, origin: str, timeout: float = 60) -> "Harness":
        """Redeem a one-time browser pairing code and return an origin-bound client. Its `paired_app` is the key
        the code minted (name, scopes, `catalog_app_id`, ...), without the secret."""
        with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, headers={"Origin": origin}) as client:
            resp = client.post("/api/v1/pair", json={"code": code})
            if resp.status_code >= 400:
                cls._raise_response(resp)
            paired = resp.json()
        harness = cls(base_url, paired["token"], timeout=timeout, origin=origin)
        harness.paired_app = paired.get("app") or {}
        return harness

    @staticmethod
    def _pkce() -> tuple[str, str]:
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return verifier, challenge

    @classmethod
    def _pairing_post(cls, base_url: str, path: str, body: dict, origin: str, timeout: float) -> dict:
        headers = {"Origin": origin} if origin else {}
        with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, headers=headers) as client:
            resp = client.post(path, json=body)
            if resp.status_code >= 400:
                cls._raise_response(resp)
            return resp.json()

    @classmethod
    def _require_feature(cls, base_url: str, feature: str, origin: str, timeout: float) -> None:
        """Refuse up front, with a clear error, when the Server predates `features[feature]` (GET /api/v1). A Server
        that wants credentials to read its root (an App on the daemon's own machine has none yet) is not refused: the
        pairing request itself is open, and its answer decides."""
        headers = {"Origin": origin} if origin else {}
        with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, headers=headers) as client:
            resp = client.get("/api/v1")
            if resp.status_code in (401, 403):
                return
            if resp.status_code >= 400:
                cls._raise_response(resp)
            features = resp.json().get("features") or {}
        if not features.get(feature):
            raise HarnessError(501, f"this Server does not support {feature}; update it, or pair with an "
                                    "owner-made pairing code", "feature_unsupported")

    @classmethod
    def request_pairing(cls, base_url: str, name: str, scopes: tuple[str, ...] | list[str] = ("sessions",),
                        catalog_app_id: str = "", origin: str = "", timeout: float = 60) -> PairingRequest:
        """Ask the owner to pair this App. A browser App passes its exact `origin`; a native App leaves it empty.
        Show the result's `match_code`; the owner approves with it."""
        cls._require_feature(base_url, "pairing_requests", origin, timeout)
        verifier, challenge = cls._pkce()
        out = cls._pairing_post(base_url, "/api/v1/pair/requests", {
            "name": name, "scopes": list(scopes), "catalog_app_id": catalog_app_id, "code_challenge": challenge,
            "code_challenge_method": "S256"}, origin, timeout)
        return PairingRequest(base_url, out["id"], out["state"], out.get("match_code") or "", out["expires_at"],
                              origin, verifier)

    @classmethod
    def claim_pairing(cls, base_url: str, request_id: str, origin: str = "", timeout: float = 60) -> PairingRequest:
        """Claim a slot the owner armed from the Hub (they hand the App its id). A native claim returns a
        `match_code` for the owner to confirm; a browser claim from the armed origin is approved at once."""
        cls._require_feature(base_url, "pairing_requests", origin, timeout)
        verifier, challenge = cls._pkce()
        out = cls._pairing_post(base_url, f"/api/v1/pair/requests/{request_id}/claim", {
            "code_challenge": challenge, "code_challenge_method": "S256"}, origin, timeout)
        return PairingRequest(base_url, out["id"], out["state"], out.get("match_code") or "", out["expires_at"],
                              origin, verifier)

    @classmethod
    def request_hub_claim(cls, base_url: str, name: str, catalog_app_id: str = "", origin: str = "",
                          timeout: float = 60) -> PairingRequest:
        """Ask to become this daemon's one Hub (#543). Show the result's `match_code`; the owner approves it on the
        daemon host (`harness hub approve <id> --match <code>`). `redeem_pairing` then returns a client holding the
        Hub key. A Server that already has a Hub refuses with 409 `hub_claimed`."""
        cls._require_feature(base_url, "hub_claim", origin, timeout)
        verifier, challenge = cls._pkce()
        out = cls._pairing_post(base_url, "/api/v1/pair/requests", {
            "name": name, "kind": "hub", "catalog_app_id": catalog_app_id, "code_challenge": challenge,
            "code_challenge_method": "S256"}, origin, timeout)
        return PairingRequest(base_url, out["id"], out["state"], out.get("match_code") or "", out["expires_at"],
                              origin, verifier)

    @classmethod
    def redeem_pairing(cls, pairing: PairingRequest, wait: float = 600, poll_interval: float = 2,
                       timeout: float = 60) -> "Harness":
        """Wait up to `wait` seconds for the owner's approval, then fetch the token (once) and return a client
        using it. Its `paired_app` is the key that was minted, without the secret."""
        deadline = time.monotonic() + wait
        while True:
            try:
                paired = cls._pairing_post(pairing.base_url, f"/api/v1/pair/requests/{pairing.id}/token",
                                           {"code_verifier": pairing.code_verifier}, pairing.origin, timeout)
                break
            except HarnessError as e:
                if e.code != "pairing_pending" or time.monotonic() + poll_interval > deadline:
                    raise
            time.sleep(poll_interval)
        harness = cls(pairing.base_url, paired["token"], timeout=timeout, origin=pairing.origin)
        harness.paired_app = paired.get("app") or {}
        return harness

    # plumbing
    @staticmethod
    def _raise_response(resp: httpx.Response) -> None:
        error = {}
        try:
            payload = resp.json()
            detail = payload.get("detail", resp.text)
            error = payload.get("error") or {}
            code = error.get("code", "")
        except ValueError:
            detail, code = resp.text, ""
        exc = HarnessError(resp.status_code, str(detail), str(code))
        exc.operation_id = error.get("operation_id", "")
        exc.may_have_completed = bool(error.get("may_have_completed"))
        if exc.may_have_completed:
            exc.retryable = False
        raise exc

    def _call(self, method: str, path: str, **kwargs) -> Any:
        resp = self.client.request(method, f"/api/v1{path}", **kwargs)
        self.audit_warning = resp.headers.get("X-Agent-Harness-Audit-Warning", "")
        if resp.status_code >= 400:
            self._raise_response(resp)
        return resp.json() if resp.headers.get("content-type", "").startswith(JSON_MEDIA_TYPE) else resp.content

    def info(self) -> dict:
        return self._call("GET", "")

    def audit(self, limit: int = 200, before_id: int | None = None, **filters) -> dict:
        """Read only this App/member's operational trail; opaque newest-first cursor and safe filters."""
        params = {"limit": limit, **filters}
        if before_id is not None:
            params["before_id"] = before_id
        return self._call("GET", "/audit", params=params)

    def capabilities(self) -> Capabilities:
        return self.info()["capabilities"]

    def backends(self) -> list[BackendStatus]:
        return self._call("GET", "/backends")

    @staticmethod
    def _resolve_schema(schema: dict, value: dict) -> dict:
        while "$ref" in value:
            value = schema["components"]["schemas"][value["$ref"].rsplit("/", 1)[-1]]
        return value

    def _check_request_fields(self, schema: dict, name: str, method: str, path: str) -> str | None:
        operation = (schema.get("paths", {}).get(path) or {}).get(method)
        if operation is None:
            return f"{name}: missing {method.upper()} {path}"
        expected = SDK_REQUEST_FIELDS.get((method, path))
        if expected is None:
            return None
        body = (((operation.get("requestBody") or {}).get("content") or {}).get(JSON_MEDIA_TYPE) or {}).get(
            "schema")
        if not body:
            return f"{name}: OpenAPI has no JSON request schema"
        body = self._resolve_schema(schema, body)
        actual = set((body.get("properties") or {}).keys())
        if expected != actual:
            return f"{name}: SDK fields {sorted(expected)} != OpenAPI fields {sorted(actual)}"
        return None

    def _check_response_fields(self, schema: dict, method: str, path: str, status: str, response_type: type,
                               is_list: bool, nested: str = "") -> str | None:
        operation = (schema.get("paths", {}).get(path) or {}).get(method) or {}
        response = (((operation.get("responses") or {}).get(status) or {}).get("content") or {}).get(
            JSON_MEDIA_TYPE, {}).get("schema")
        if not response:
            return f"{method.upper()} {path}: OpenAPI has no JSON response schema"
        if is_list:
            response = response.get("items") or {}
        response = self._resolve_schema(schema, response)
        if nested:
            response = self._resolve_schema(schema, (response.get("properties") or {}).get(nested) or {})
        missing = set(response_type.__annotations__) - set((response.get("properties") or {}).keys())
        if missing:
            return f"{method.upper()} {path}: SDK response fields missing from OpenAPI: {sorted(missing)}"
        return None

    def validate_openapi(self, schema: dict | None = None) -> None:
        """Fail if this SDK's operations or JSON body fields drift from the daemon OpenAPI document."""
        fetched = schema is None
        schema = schema or self.client.get("/openapi.json").json()
        found = [self._check_request_fields(schema, name, method, path)
                 for name, (method, path) in SDK_OPERATIONS.items()]
        found += [self._check_response_fields(schema, method, path, status, response_type, is_list)
                  for (method, path), (status, response_type, is_list) in SDK_RESPONSE_TYPES.items()]
        found += [self._check_response_fields(schema, method, path, status, response_type, False, prop)
                  for ((method, path), prop), (status, response_type) in SDK_NESTED_RESPONSE_TYPES.items()]
        errors = [e for e in found if e]
        version = str((self.info() if fetched else {}).get("api_version") or "")
        major, _, _ = version.partition(".")
        if version and major != SDK_API_MAJOR:
            errors.append(f"SDK supports API major {SDK_API_MAJOR}, daemon reports {version}")
        if errors:
            raise ContractError("; ".join(errors))

    # sessions
    def create_session(self, prompt: str, project: str | None = None, context: dict[str, str] | None = None,
                       tools: list[Tool] | None = None, metadata: dict | None = None, title: str | None = None,
                       model: str | None = None, backend: str = "local", tools_only: bool = False,
                       retention_days: float | None = None, end_user: str | None = None) -> Session:
        """`tools_only=True` starts an App-tools-only session: the model gets only `tools` (no workspace, project,
        built-in or CLI tools). It takes no project; backends that can't do it refuse with
        app_tools_only_unsupported. `retention_days` erases the session once it has been idle that long. `end_user`
        runs the session on that person's own Claude or Codex login (see `start_end_user_login`), or is refused with
        end_user_login_required."""
        body = {"prompt": prompt, "project": project if project is not None or tools_only else "scratch",
                "backend": backend, "metadata": metadata or {}, "title": title, "model": model,
                "context": [{"title": k, "content": v} for k, v in (context or {}).items()],
                "tools": [t.spec() for t in tools or []], "tools_only": tools_only, "retention_days": retention_days,
                "end_user": end_user}
        return self._call("POST", "/sessions", json=body)

    # end users' own subscription logins (#365)
    def start_end_user_login(self, end_user: str, backend: str) -> dict:
        """Start the CLI's own sign-in: `{attempt_id, verification_url, user_code?, needs_code}` for a popup."""
        return self._call("POST", f"/end-users/{end_user}/logins/{backend}")

    def submit_end_user_login_code(self, end_user: str, backend: str, attempt_id: str, code: str) -> dict:
        """Claude only: pass the one-time code the person pasted. It is single use."""
        return self._call("POST", f"/end-users/{end_user}/logins/{backend}/{attempt_id}/code", json={"code": code})

    def end_user_login(self, end_user: str, backend: str) -> dict:
        return self._call("GET", f"/end-users/{end_user}/logins/{backend}")

    def unlink_end_user(self, end_user: str, backend: str) -> None:
        self._call("DELETE", f"/end-users/{end_user}/logins/{backend}")

    def session(self, sid: str) -> Session:
        return self._call("GET", f"/sessions/{sid}")

    def sessions(self, limit: int = 50) -> list[Session]:
        return self._call("GET", "/sessions", params={"limit": limit})

    def send(self, sid: str, content: str) -> dict:
        return self._call("POST", f"/sessions/{sid}/messages", json={"content": content})

    def add_context(self, sid: str, context: dict[str, str]) -> dict:
        return self._call("POST", f"/sessions/{sid}/context",
                          json={"context": [{"title": k, "content": v} for k, v in context.items()]})

    def cancel(self, sid: str) -> dict:
        return self._call("POST", f"/sessions/{sid}/cancel")

    def pending_tool_calls(self, sid: str) -> list[dict]:
        return self._call("GET", f"/sessions/{sid}/tool_calls", params={"status": "pending"})

    def submit_tool_result(self, sid: str, call_id: str, output: str, ok: bool = True) -> dict:
        return self._call("POST", f"/sessions/{sid}/tool_calls/{call_id}", json={"output": output, "ok": ok})

    def pending_approvals(self, sid: str) -> list[Approval]:
        return self._call("GET", f"/sessions/{sid}/approvals")

    def decide_approval(self, sid: str, approval_id: str, approve: bool, note: str = "") -> Approval:
        return self._call("POST", f"/sessions/{sid}/approvals/{approval_id}",
                          json={"decision": "approve" if approve else "deny", "note": note})

    @staticmethod
    def _sse_events(resp: httpx.Response) -> Iterator[Event]:
        data = []
        for line in resp.iter_lines():
            if line.startswith("data:"):
                data.append(line[5:].strip())
            elif not line and data:
                event = json.loads("\n".join(data))
                data = []
                yield event

    def events(self, sid: str, after: int = 0, follow: bool = True) -> Iterator[Event]:
        """Server-sent events of a session. Reconnects on network errors, resuming after the last event seen."""
        last = after
        while True:
            try:
                with self.client.stream("GET", f"/api/v1/sessions/{sid}/events",
                                        params={"after": last, "follow": str(follow).lower()},
                                        timeout=httpx.Timeout(60, read=90)) as resp:
                    if resp.status_code >= 400:
                        resp.read()
                        self._raise_response(resp)
                    for event in self._sse_events(resp):
                        if event.get("seq"):
                            last = event["seq"]
                        yield event
                if not follow:
                    return
            except (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ConnectError):
                time.sleep(2)

    def _serve_tool_call(self, sid: str, by_name: dict[str, Tool], handled: set[str], call: dict) -> None:
        if call["call_id"] in handled:
            return
        handled.add(call["call_id"])
        t = by_name.get(call["name"])
        try:
            output, ok = (str(t.fn(**(call.get("args") or {}))), True) if t else (f"unknown tool {call['name']}", False)
        except Exception as e:  # noqa: BLE001 - report the app-side failure to the agent
            output, ok = f"{type(e).__name__}: {e}", False
        try:
            self.submit_tool_result(sid, call["call_id"], output, ok)
        except HarnessError as e:
            if e.status != 409:  # 409: already answered (e.g. after a reconnect)
                raise

    def _drive(self, sid: str, by_name: dict[str, Tool], result: RunResult, on_event: Callable[[dict], None] | None,
               after: int = 0, confirm_replays: bool = False) -> RunResult:
        handled: set[str] = set()
        for call in self.pending_tool_calls(sid):
            self._serve_tool_call(sid, by_name, handled, call)
        for event in self.events(sid, after=after):
            result.events.append(event)
            if on_event:
                on_event(event)
            if event["type"] == "app_tool_call":
                call = {**event["data"]}
                # a replayed event may describe a call that was answered or expired since
                if call["call_id"] not in handled and (
                        not confirm_replays
                        or any(c["call_id"] == call["call_id"] for c in self.pending_tool_calls(sid))):
                    self._serve_tool_call(sid, by_name, handled, call)
            if event["type"] == "run_finished":
                break
        result.session = self.session(sid)
        return result

    def run(self, prompt: str, tools: list[Tool] | None = None, on_event: Callable[[dict], None] | None = None,
            **create_args) -> RunResult:
        """Create a session and serve its tool calls until it ends."""
        by_name = {t.name: t for t in tools or []}
        s = self.create_session(prompt, tools=tools, **create_args)
        return self._drive(s["id"], by_name, RunResult(session=s), on_event)

    def attach(self, sid: str, tools: list[Tool] | None = None,
               on_event: Callable[[dict], None] | None = None) -> RunResult:
        """Attach to an existing session's current run: serve its pending App tool calls and return the result.

        Sends and creates nothing. A finished session is returned as is. Only one driver should own a session."""
        s = self.session(sid)
        result = RunResult(session=s)
        if s.get("status") in ("done", "failed", "cancelled"):
            return result
        return self._drive(sid, {t.name: t for t in tools or []}, result, on_event,
                           after=s.get("last_event_seq") or 0, confirm_replays=True)

    # images
    def generate_image(self, prompt: str, model: str = "fast", aspect_ratio: str = "1:1", wait: bool = True,
                       poll_seconds: float = 3, upscale: str = "none") -> bytes | dict:
        job = self._call("POST", "/images", json={"prompt": prompt, "model": model, "aspect_ratio": aspect_ratio,
                                                 "upscale": upscale})
        if not wait:
            return job
        job = self._wait_image(job, poll_seconds)
        requested = (upscale or "none").strip().lower()
        if requested not in ("", "none"):
            detail = self._call("GET", f"/images/{job['id']}")
            children = detail.get("children") or []
            if children:
                derived = self._wait_image(self._call("GET", f"/images/{children[0]['id']}"), poll_seconds)
                return self._call("GET", f"/images/{derived['id']}.png")
        return self._call("GET", f"/images/{job['id']}.png")

    def upscale_image(self, image_id: str, upscale: str = "2x", wait: bool = True,
                      poll_seconds: float = 3) -> bytes | dict:
        job = self._call("POST", f"/images/{image_id}/upscale", json={"upscale": upscale})
        if not wait:
            return job
        job = self._wait_image(job, poll_seconds)
        return self._call("GET", f"/images/{job['id']}.png")

    def _wait_image(self, job: dict, poll_seconds: float) -> dict:
        while job["status"] not in ("done", "failed"):
            time.sleep(poll_seconds)
            job = self._call("GET", f"/images/{job['id']}")
        if job["status"] == "failed":
            raise HarnessError(500, job.get("error") or "image job failed")
        return job
