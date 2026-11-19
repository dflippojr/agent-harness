"""Check every real response of a route with a documentation-only response model against that model (#1015).

`install()` wraps `fastapi.routing.APIRoute.get_route_handler`, which FastAPI calls once for each route it builds. A
route whose `responses=` names a model for a status (`harness.response_docs.documented`) gets a handler that, after
the route answers with that status, validates the JSON body against the model in strict mode (no coercion: a number
sent as a string, or an integer flag sent for a boolean, is a mismatch). A mismatch fails the test that made the
request (tests/conftest.py). Error replies come from the exception handlers, outside the route, and are not checked.

Routes with a `response_model=` are skipped: FastAPI already shapes their output to it.

Nothing here changes a response: the wrapper returns what the route returned.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.routing import APIRoute
from pydantic import TypeAdapter, ValidationError

problems: list[str] = []
# (method, route path, status) of every documented reply checked so far in this process.
seen: set[tuple[str, str, int]] = set()
_adapters: dict[int, TypeAdapter] = {}


def documented_models(route: APIRoute) -> dict[int, Any]:
    """The documentation-only models a route declares, by status code."""
    if route.response_model is not None:
        return {}
    out = {}
    for status, spec in (route.responses or {}).items():
        if isinstance(spec, dict) and "model" in spec and str(status).isdigit():
            out[int(status)] = spec["model"]
    return out


def mismatch(model: Any, body: bytes) -> str:
    """Why `body` does not match `model`, or "" when it does."""
    adapter = _adapters.get(id(model))
    if adapter is None:
        adapter = _adapters[id(model)] = TypeAdapter(model)
    try:
        adapter.validate_json(body, strict=True)
    except ValidationError as e:
        return str(e)
    return ""


def check(method: str, path: str, status: int, model: Any, response: Any) -> None:
    seen.add((method, path, status))
    body = getattr(response, "body", None)
    media = (getattr(response, "media_type", None) or "").split(";")[0]
    if not isinstance(body, bytes) or media != "application/json":
        problems.append(f"{method} {path} {status}: documented as JSON, answered {media or type(response).__name__}")
        return
    error = mismatch(model, body)
    if error:
        problems.append(f"{method} {path} {status} does not match {getattr(model, '__name__', model)}: {error}\n"
                        f"  {json.dumps(json.loads(body))[:2000]}")


def install() -> None:
    if getattr(APIRoute, "_response_capture_installed", False):
        return
    original = APIRoute.get_route_handler

    def get_route_handler(self: APIRoute):
        handler = original(self)
        models = documented_models(self)
        if not models:
            return handler
        path = self.path

        async def checked(request):
            response = await handler(request)
            model = models.get(response.status_code)
            if model is not None:
                check(request.method, path, response.status_code, model, response)
            return response
        return checked

    APIRoute.get_route_handler = get_route_handler
    APIRoute._response_capture_installed = True
