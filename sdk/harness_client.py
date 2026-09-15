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
Everything else (create_session, events, send, add_context, cancel, submit_tool_result, generate_image) is a thin
wrapper over the HTTP API described in docs/app-api.md.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import httpx

TERMINAL = ("done", "failed", "cancelled")


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
    session: dict
    events: list[dict] = field(default_factory=list)

    @property
    def answer(self) -> str:
        return self.session.get("answer") or ""

    @property
    def status(self) -> str:
        return self.session.get("status", "")


class HarnessError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


class Harness:
    def __init__(self, base_url: str, token: str, timeout: float = 60):
        self.base = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base, timeout=timeout,
                                   headers={"Authorization": f"Bearer {token}"})

    # plumbing
    def _call(self, method: str, path: str, **kwargs) -> Any:
        resp = self.client.request(method, f"/api/v1{path}", **kwargs)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise HarnessError(resp.status_code, str(detail))
        return resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.content

    def info(self) -> dict:
        return self._call("GET", "")

    # sessions
    def create_session(self, prompt: str, project: str = "scratch", context: dict[str, str] | None = None,
                       tools: list[Tool] | None = None, metadata: dict | None = None, title: str | None = None,
                       model: str | None = None) -> dict:
        body = {"prompt": prompt, "project": project, "metadata": metadata or {}, "title": title, "model": model,
                "context": [{"title": k, "content": v} for k, v in (context or {}).items()],
                "tools": [t.spec() for t in tools or []]}
        return self._call("POST", "/sessions", json=body)

    def session(self, sid: str) -> dict:
        return self._call("GET", f"/sessions/{sid}")

    def sessions(self, limit: int = 50) -> list[dict]:
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

    def events(self, sid: str, after: int = 0, follow: bool = True) -> Iterator[dict]:
        """Server-sent events of a session. Reconnects on network errors, resuming after the last event seen."""
        last = after
        while True:
            try:
                with self.client.stream("GET", f"/api/v1/sessions/{sid}/events",
                                        params={"after": last, "follow": str(follow).lower()},
                                        timeout=httpx.Timeout(60, read=90)) as resp:
                    if resp.status_code >= 400:
                        raise HarnessError(resp.status_code, resp.read().decode("utf-8", "replace"))
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
                       poll_seconds: float = 3) -> bytes | dict:
        job = self._call("POST", "/images", json={"prompt": prompt, "model": model, "aspect_ratio": aspect_ratio})
        if not wait:
            return job
        while job["status"] not in ("done", "failed"):
            time.sleep(poll_seconds)
            job = self._call("GET", f"/images/{job['id']}")
        if job["status"] == "failed":
            raise HarnessError(500, job["error"])
        return self._call("GET", f"/images/{job['id']}.png")
