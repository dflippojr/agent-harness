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
Everything else (pair, capabilities, backends, create_session, events, send, add_context, approvals, cancel,
submit_tool_result, generate_image, upscale_image) is a thin wrapper over the HTTP API described in docs/app-api.md. Run
`Harness.validate_openapi()` in an integration check to detect client/server contract drift.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, TypedDict

import httpx

TERMINAL = ("done", "failed", "cancelled")
SDK_API_MAJOR = "1"
JSON_MEDIA_TYPE = "application/json"

# Used by validate_openapi() and CI. Paths use the server's OpenAPI templates, not formatted runtime ids.
SDK_OPERATIONS = {
    "info": ("get", "/api/v1"), "pair": ("post", "/api/v1/pair"),
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
    SDK_OPERATIONS["create_session"]: {"prompt", "project", "backend", "model", "title", "context", "tools",
                                       "metadata"},
    SDK_OPERATIONS["send"]: {"content"},
    SDK_OPERATIONS["add_context"]: {"context"},
    SDK_OPERATIONS["submit_tool_result"]: {"output", "ok"},
    SDK_OPERATIONS["decide_approval"]: {"decision", "note"},
    SDK_OPERATIONS["generate_image"]: {"prompt", "model", "aspect_ratio", "upscale"},
    SDK_OPERATIONS["upscale_image"]: {"upscale"},
}


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
}


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
        """Redeem a one-time browser pairing code and return an origin-bound client."""
        with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, headers={"Origin": origin}) as client:
            resp = client.post("/api/v1/pair", json={"code": code})
            if resp.status_code >= 400:
                cls._raise_response(resp)
            paired = resp.json()
        return cls(base_url, paired["token"], timeout=timeout, origin=origin)

    # plumbing
    @staticmethod
    def _raise_response(resp: httpx.Response) -> None:
        try:
            payload = resp.json()
            detail = payload.get("detail", resp.text)
            code = (payload.get("error") or {}).get("code", "")
        except ValueError:
            detail, code = resp.text, ""
        raise HarnessError(resp.status_code, str(detail), str(code))

    def _call(self, method: str, path: str, **kwargs) -> Any:
        resp = self.client.request(method, f"/api/v1{path}", **kwargs)
        if resp.status_code >= 400:
            self._raise_response(resp)
        return resp.json() if resp.headers.get("content-type", "").startswith(JSON_MEDIA_TYPE) else resp.content

    def info(self) -> dict:
        return self._call("GET", "")

    def capabilities(self) -> Capabilities:
        return self.info()["capabilities"]

    def backends(self) -> list[BackendStatus]:
        return self._call("GET", "/backends")

    @staticmethod
    def _resolve_schema(schema: dict, value: dict) -> dict:
        while "$ref" in value:
            value = schema["components"]["schemas"][value["$ref"].rsplit("/", 1)[-1]]
        return value

    def validate_openapi(self, schema: dict | None = None) -> None:
        """Fail if this SDK's operations or JSON body fields drift from the daemon OpenAPI document."""
        fetched = schema is None
        schema = schema or self.client.get("/openapi.json").json()
        errors: list[str] = []
        for name, (method, path) in SDK_OPERATIONS.items():
            operation = (schema.get("paths", {}).get(path) or {}).get(method)
            if operation is None:
                errors.append(f"{name}: missing {method.upper()} {path}")
                continue
            expected = SDK_REQUEST_FIELDS.get((method, path))
            if expected is None:
                continue
            body = (((operation.get("requestBody") or {}).get("content") or {}).get(JSON_MEDIA_TYPE) or {}).get(
                "schema")
            if not body:
                errors.append(f"{name}: OpenAPI has no JSON request schema")
                continue
            body = self._resolve_schema(schema, body)
            actual = set((body.get("properties") or {}).keys())
            if expected != actual:
                errors.append(f"{name}: SDK fields {sorted(expected)} != OpenAPI fields {sorted(actual)}")
        for (method, path), (status, response_type, is_list) in SDK_RESPONSE_TYPES.items():
            operation = (schema.get("paths", {}).get(path) or {}).get(method) or {}
            response = (((operation.get("responses") or {}).get(status) or {}).get("content") or {}).get(
                JSON_MEDIA_TYPE, {}).get("schema")
            if not response:
                errors.append(f"{method.upper()} {path}: OpenAPI has no JSON response schema")
                continue
            if is_list:
                response = response.get("items") or {}
            response = self._resolve_schema(schema, response)
            missing = set(response_type.__annotations__) - set((response.get("properties") or {}).keys())
            if missing:
                errors.append(f"{method.upper()} {path}: SDK response fields missing from OpenAPI: {sorted(missing)}")
        version = str((self.info() if fetched else {}).get("api_version") or "")
        if version and version.split(".", 1)[0] != SDK_API_MAJOR:
            errors.append(f"SDK supports API major {SDK_API_MAJOR}, daemon reports {version}")
        if errors:
            raise ContractError("; ".join(errors))

    # sessions
    def create_session(self, prompt: str, project: str = "scratch", context: dict[str, str] | None = None,
                       tools: list[Tool] | None = None, metadata: dict | None = None, title: str | None = None,
                       model: str | None = None, backend: str = "local") -> Session:
        body = {"prompt": prompt, "project": project, "backend": backend, "metadata": metadata or {},
                "title": title, "model": model,
                "context": [{"title": k, "content": v} for k, v in (context or {}).items()],
                "tools": [t.spec() for t in tools or []]}
        return self._call("POST", "/sessions", json=body)

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
                    data = []
                    for line in resp.iter_lines():
                        if line.startswith("data:"):
                            data.append(line[5:].strip())
                        elif not line and data:
                            event = json.loads("\n".join(data))
                            data = []
                            if event.get("seq"):
                                last = event["seq"]
                            yield event
                if not follow:
                    return
            except (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ConnectError):
                time.sleep(2)

    def run(self, prompt: str, tools: list[Tool] | None = None, on_event: Callable[[dict], None] | None = None,
            **create_args) -> RunResult:
        """Create a session and serve its tool calls until it ends."""
        by_name = {t.name: t for t in tools or []}
        s = self.create_session(prompt, tools=tools, **create_args)
        result = RunResult(session=s)
        handled: set[str] = set()

        def serve(call: dict) -> None:
            if call["call_id"] in handled:
                return
            handled.add(call["call_id"])
            t = by_name.get(call["name"])
            try:
                output, ok = (str(t.fn(**(call.get("args") or {}))), True) if t else (f"unknown tool {call['name']}", False)
            except Exception as e:  # noqa: BLE001 - report the app-side failure to the agent
                output, ok = f"{type(e).__name__}: {e}", False
            try:
                self.submit_tool_result(s["id"], call["call_id"], output, ok)
            except HarnessError as e:
                if e.status != 409:  # 409: already answered (e.g. after a reconnect)
                    raise

        for call in self.pending_tool_calls(s["id"]):
            serve(call)
        for event in self.events(s["id"]):
            result.events.append(event)
            if on_event:
                on_event(event)
            if event["type"] == "app_tool_call":
                serve({**event["data"]})
            if event["type"] == "run_finished" or (event["type"] == "status" and event["data"]["status"] in TERMINAL):
                if event["type"] == "run_finished":
                    break
        result.session = self.session(s["id"])
        return result

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
