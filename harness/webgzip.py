"""Runtime gzip for the web shell (static assets, `/`, `/sw.js`) only; never API JSON, SSE or binary routes."""

from __future__ import annotations

import gzip
from pathlib import Path

MIN_SIZE = 1024
TEXT_TYPES = ("text/javascript", "application/javascript", "text/css", "text/html", "image/svg+xml",
              "application/manifest+json", "application/json")


def _in_scope(path: str, web: Path) -> bool:
    if path in ("/", "/sw.js") or path.startswith("/static/"):
        return True
    rel = path.lstrip("/")
    if not rel or ".." in rel.split("/"):
        return False
    try:
        return (web / rel).is_file()
    except OSError:
        return False


def _weak(etag: bytes) -> bytes:
    return etag if etag.startswith(b"W/") else b"W/" + etag


class WebGzipMiddleware:
    def __init__(self, app, web: Path):
        self.app = app
        self.web = web

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("GET", "HEAD") or not _in_scope(scope["path"], self.web):
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        accepts = b"gzip" in headers.get(b"accept-encoding", b"").lower()
        # Starlette's FileResponse may hand the file to the server via pathsend; we need the bytes.
        scope = {**scope, "extensions": {k: v for k, v in scope.get("extensions", {}).items() if k != "http.response.pathsend"}}
        state: dict = {"start": None, "body": b"", "passthrough": False}

        async def wrapped(message):
            if state["passthrough"]:
                return await send(message)
            if message["type"] == "http.response.start":
                h = message["headers"]
                ctype = next((v for k, v in h if k.lower() == b"content-type"), b"").decode("latin-1").split(";")[0].strip().lower()
                if not accepts:
                    state["passthrough"] = True
                    vary = [v for k, v in h if k.lower() == b"vary"]
                    out = [(k, v) for k, v in h if k.lower() != b"vary"]
                    return await send({**message, "headers": out + [(b"vary", b", ".join(vary + [b"Accept-Encoding"]))]}) if ctype in TEXT_TYPES else await send(message)
                if ctype not in TEXT_TYPES or any(k.lower() == b"content-encoding" for k, _ in h) or message["status"] not in (200, 304):
                    state["passthrough"] = True
                    return await send(message)
                if message["status"] == 304:
                    state["passthrough"] = True
                    out = [(k, _weak(v) if k.lower() == b"etag" else v) for k, v in h if k.lower() != b"vary"]
                    return await send({**message, "headers": out + [(b"vary", b"Accept-Encoding")]})
                state["start"] = message
                return
            state["body"] += message.get("body", b"")
            if message.get("more_body"):
                return
            start, body = state["start"], state["body"]
            h = [(k, v) for k, v in start["headers"] if k.lower() not in (b"content-length", b"vary")]
            vary = [v for k, v in start["headers"] if k.lower() == b"vary"]
            h.append((b"vary", b", ".join(vary + [b"Accept-Encoding"])))
            if len(body) >= MIN_SIZE:
                body = gzip.compress(body, 6, mtime=0)
                h = [(k, _weak(v) if k.lower() == b"etag" else v) for k, v in h]
                h.append((b"content-encoding", b"gzip"))
            h.append((b"content-length", str(len(body)).encode()))
            await send({**start, "headers": h})
            await send({"type": "http.response.body", "body": b"" if scope["method"] == "HEAD" else body})

        await self.app(scope, receive, wrapped)
