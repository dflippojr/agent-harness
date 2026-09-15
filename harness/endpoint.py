"""Inference endpoint: the tower's model for other tools, OpenAI- and Anthropic-compatible.

llama-server already speaks both APIs (/v1/chat/completions, /v1/completions, /v1/responses, /v1/messages,
/v1/messages/count_tokens), so this is a thin proxy inside the daemon rather than a separate gateway such as LiteLLM.
Living in the daemon is the point: requests share the GPU with agent sessions through the InferenceGate (endpoint
requests go ahead of the next agent turn, user decision), honor the GPU guard (a game or Plex transcode means 503),
and are logged per key.

- Auth: a per-device key (`Authorization: Bearer hk-...` or Anthropic's `x-api-key`). Keys are created and revoked
  from Settings or POST /keys; only their sha256 is stored. Published on the tailnet by the same `tailscale serve`
  as the web app, so a tailnet login is also required from other devices.
- Model names: whatever a client asks for ("gpt-4o", "claude-sonnet-...") is mapped to a configured model through
  `endpoint.model_aliases` (fnmatch patterns), falling back to the default model. Responses name the real model.
- Discovery: GET /v1/models (shaped for both OpenAI and Anthropic clients) and GET /v1/capabilities.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import re
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .scheduler import GpuExclusive, QueueFull

log = logging.getLogger("harness.endpoint")

ROUTES = {
    "/v1/chat/completions": "openai",
    "/v1/completions": "openai",
    "/v1/responses": "openai",
    "/v1/messages": "anthropic",
    "/v1/messages/count_tokens": "anthropic",
}
NO_GPU = {"/v1/messages/count_tokens"}  # tokenizer only
MAX_BODY = 32 * 2**20
# usage fields, or llama-server's `timings` (always in the last streamed chunk, even without include_usage)
USAGE_PATTERNS = {
    "prompt": re.compile(rb'"(?:prompt_tokens|input_tokens|prompt_n)"\s*:\s*(\d+)'),
    "completion": re.compile(rb'"(?:completion_tokens|output_tokens|predicted_n)"\s*:\s*(\d+)'),
}


def error(flavor: str, status: int, kind: str, message: str, headers: dict | None = None) -> JSONResponse:
    if flavor == "anthropic":
        body = {"type": "error", "error": {"type": kind, "message": message}}
    else:
        body = {"error": {"message": message, "type": kind, "code": status}}
    return JSONResponse(body, status_code=status, headers=headers)


def usage_from(head: bytes, tail: bytes) -> tuple[int, int]:
    def last(kind: str) -> int:
        found = USAGE_PATTERNS[kind].findall(head + b"\n" + tail)
        return int(found[-1]) if found else 0
    prompt = last("prompt")
    if not prompt:
        found = USAGE_PATTERNS["prompt"].findall(head)
        prompt = int(found[0]) if found else 0
    return prompt, last("completion")


def register(app: FastAPI, mgr) -> None:
    def resolve_model(m, requested: str):
        cfg = m.cfg
        if requested in cfg.models:
            return cfg.models[requested]
        for pattern, target in cfg.endpoint.model_aliases.items():
            if fnmatch.fnmatch(requested or "", pattern) and target in cfg.models:
                return cfg.models[target]
        return cfg.models[cfg.endpoint.default_model or cfg.default_model]

    def authenticate(m, request: Request) -> dict | None:
        auth = request.headers.get("authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "").strip()
        row = m.db.api_key_by_secret(key)
        return row if row and "inference" in (row.get("scopes") or "").split() else None

    def flavor_of(request: Request) -> str:
        return "anthropic" if request.headers.get("anthropic-version") or request.headers.get("x-api-key") else "openai"

    @app.get("/v1/models")
    async def v1_models(request: Request):
        m = mgr(request)
        if not m.cfg.endpoint.enabled:
            return error(flavor_of(request), 404, "not_found_error", "the inference endpoint is disabled")
        if authenticate(m, request) is None:
            return error(flavor_of(request), 401, "authentication_error", "missing or invalid API key")
        data = [{"id": mc.name, "object": "model", "type": "model", "display_name": mc.name, "owned_by": "tower",
                 "created": 0, "created_at": "2026-01-01T00:00:00Z", "context_length": mc.context_tokens}
                for mc in m.cfg.models.values()]
        return {"object": "list", "data": data, "has_more": False,
                "first_id": data[0]["id"] if data else None, "last_id": data[-1]["id"] if data else None}

    @app.get("/v1/capabilities")
    async def v1_capabilities(request: Request):
        m = mgr(request)
        if not m.cfg.endpoint.enabled or authenticate(m, request) is None:
            return error("openai", 401, "authentication_error", "missing or invalid API key")
        return {
            "server": "agent-harness", "api_version": 1,
            "routes": {path: {"method": "POST", "api": api} for path, api in ROUTES.items()}
            | {"/v1/models": {"method": "GET", "api": "both"}},
            "models": [{"id": mc.name, "context_tokens": mc.context_tokens, "max_tokens": mc.max_tokens,
                        "default": mc.name == (m.cfg.endpoint.default_model or m.cfg.default_model)}
                       for mc in m.cfg.models.values()],
            "features": {"streaming": True, "tool_calls": True, "reasoning": True, "embeddings": False,
                         "images": bool(m.images)},
            "model_aliases": m.cfg.endpoint.model_aliases,
            "gpu": {"shared_with_agents": True, "guard_state": m.guard.state if m.guard else "clear"},
        }

    async def proxy(request: Request, path: str):
        m = mgr(request)
        flavor = ROUTES[path]
        ecfg = m.cfg.endpoint
        if not ecfg.enabled:
            return error(flavor, 404, "not_found_error", "the inference endpoint is disabled")
        key = authenticate(m, request)
        if key is None:
            return error(flavor, 401, "authentication_error", "missing or invalid API key")
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return error(flavor, 413, "request_too_large", "request body is too large")
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return error(flavor, 400, "invalid_request_error", "body must be a JSON object")
        model = resolve_model(m, str(body.get("model") or ""))
        body["model"] = model.name
        stream = bool(body.get("stream"))
        record = {"key_id": key["id"], "route": path, "model": model.name, "stream": stream, "status": 0}
        started = time.monotonic()

        def finish(status: int, prompt: int = 0, completion: int = 0, wait_ms: int = 0) -> None:
            record.update(status=status, prompt_tokens=prompt, completion_tokens=completion, wait_ms=wait_ms,
                          total_ms=int((time.monotonic() - started) * 1000))
            try:
                m.db.log_endpoint_request(record)
            except Exception:  # noqa: BLE001 - accounting must not break a response
                log.exception("could not log endpoint request")

        gpu = path not in NO_GPU
        if gpu and m.guard is not None and m.guard.active:
            finish(503)
            return error(flavor, 503, "overloaded_error", "the GPU is in use by a game or a Plex transcode; the "
                         "model is unloaded until it's free", headers={"Retry-After": "180"})
        slot = None
        if gpu:
            try:
                slot = await m.runner.gate.endpoint_request()
            except GpuExclusive:
                finish(503)
                return error(flavor, 503, "overloaded_error", "the GPU is generating images; the language model is "
                             "unloaded for a minute or two", headers={"Retry-After": "60"})
            except QueueFull:
                finish(429)
                return error(flavor, 429, "rate_limit_error", "too many requests are waiting for the GPU",
                             headers={"Retry-After": "10"})
        wait_ms = int((time.monotonic() - started) * 1000)
        client = httpx.AsyncClient(timeout=httpx.Timeout(ecfg.request_timeout_seconds, connect=10), trust_env=False,
                                   transport=getattr(m, "endpoint_transport", None))  # tests inject a fake server
        try:
            upstream = await client.send(client.build_request(
                "POST", f"{model.base_url.rstrip('/')}{path}", content=json.dumps(body).encode(),
                headers={"content-type": "application/json",
                         "accept": "text/event-stream" if stream else "application/json"}), stream=True)
        except httpx.HTTPError as e:
            await client.aclose()
            if slot:
                await slot.release()
            finish(502, wait_ms=wait_ms)
            return error(flavor, 502, "api_error", f"model server unreachable: {type(e).__name__}")

        ctype = upstream.headers.get("content-type", "application/json")
        if not stream or "text/event-stream" not in ctype:
            try:
                data = await upstream.aread()
            finally:
                await upstream.aclose()
                await client.aclose()
                if slot:
                    await slot.release()
            prompt, completion = usage_from(data[:16384], data[-16384:])
            finish(upstream.status_code, prompt, completion, wait_ms)
            return Response(content=data, status_code=upstream.status_code, media_type=ctype.split(";")[0])

        async def relay():
            head, tail = bytearray(), bytearray()
            try:
                async for chunk in upstream.aiter_bytes():  # decoded: content-encoding isn't forwarded
                    if len(head) < 16384:
                        head.extend(chunk[: 16384 - len(head)])
                    tail.extend(chunk)
                    del tail[:-16384]
                    yield chunk
            finally:
                await asyncio.shield(_close(upstream, client, slot))
                prompt, completion = usage_from(bytes(head), bytes(tail))
                finish(upstream.status_code, prompt, completion, wait_ms)

        return StreamingResponse(relay(), status_code=upstream.status_code, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def _close(upstream, client, slot) -> None:
        await upstream.aclose()
        await client.aclose()
        if slot:
            await slot.release()

    def route_handler(path: str):
        async def handler(request: Request):
            return await proxy(request, path)
        return handler

    for path in ROUTES:
        app.add_api_route(path, route_handler(path), methods=["POST"], include_in_schema=False)

    # key management (web app / local CLI; protected like the rest of the daemon, not by API keys)
    @app.get("/keys")
    async def list_keys(request: Request):
        return mgr(request).db.list_api_keys()

    @app.post("/keys", status_code=201)
    async def create_key(request: Request):
        body = await request.json()
        from .apps import SCOPES
        name = str((body or {}).get("name") or "").strip()
        if not name:
            return JSONResponse({"detail": "name is required"}, status_code=400)
        scopes = body.get("scopes") or ["inference"]
        unknown = [s for s in scopes if s not in SCOPES]
        if unknown or not isinstance(scopes, list):
            return JSONResponse({"detail": f"unknown scopes {unknown}; known: {', '.join(SCOPES)}"}, status_code=400)
        kind = "app" if body.get("kind") == "app" else "device"
        row, key = mgr(request).db.create_api_key(name[:60], " ".join(dict.fromkeys(scopes)), kind)
        return {**row, "key": key}

    @app.delete("/keys/{kid}", status_code=204)
    async def revoke_key(kid: str, request: Request):
        if not mgr(request).db.revoke_api_key(kid):
            return JSONResponse({"detail": "no such active key"}, status_code=404)
        return Response(status_code=204)
