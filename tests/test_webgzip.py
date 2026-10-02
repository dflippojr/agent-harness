from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

from harness.api import WEB
from harness.webgzip import WebGzipMiddleware
from tests.test_api import make_client

GZ = {"Accept-Encoding": "gzip"}
PLAIN = {"Accept-Encoding": "identity"}
SHELL = [("/static/app.js", "app.js"), ("/static/style.css", "style.css"), ("/", "index.html"), ("/sw.js", "sw.js")]


@pytest.mark.parametrize("url,name", SHELL)
def test_shell_gzip_and_identity(tmp_path, url, name):
    client, _, _ = make_client(tmp_path, [])
    raw = (WEB / name).read_bytes()
    with client:
        z = client.get(url, headers=GZ)
        assert z.headers["content-encoding"] == "gzip" and "Accept-Encoding" in z.headers["vary"]
        assert z.headers["etag"].startswith('W/"')
        assert z.content == raw  # httpx transparently decodes; byte-identical after decompression
        assert int(z.headers["content-length"]) < len(raw)
        p = client.get(url, headers=PLAIN)
        assert "content-encoding" not in p.headers and p.content == raw
        assert "Accept-Encoding" in p.headers["vary"]


@pytest.mark.parametrize("hdrs", [GZ, PLAIN])
def test_conditional_304_both_variants(tmp_path, hdrs):
    client, _, _ = make_client(tmp_path, [])
    with client:
        etag = client.get("/static/app.js", headers=hdrs).headers["etag"]
        r = client.get("/static/app.js", headers={**hdrs, "If-None-Match": etag})
        assert r.status_code == 304
        if hdrs is GZ:
            # The validator a gzip client got is also accepted from a client that later negotiates identity.
            alt = client.get("/static/app.js", headers={**PLAIN, "If-None-Match": etag})
            assert alt.status_code == 304


def test_root_catch_all_asset_and_api_json_untouched(tmp_path):
    client, _, _ = make_client(tmp_path, [])
    with client:
        z = client.get("/manifest.webmanifest", headers=GZ)
        assert z.status_code == 200
        assert client.get("/healthz", headers=GZ).headers.get("content-encoding") is None


def test_sse_and_binary_not_compressed():
    app = FastAPI()
    app.add_middleware(WebGzipMiddleware, web=WEB)

    async def events():
        for i in range(3):
            yield f"data: {'x' * 2000}\n\n"

    @app.get("/static/sse")
    async def sse():
        return StreamingResponse(events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.get("/static/pic.png")
    async def png():
        return Response(b"\x89PNG" * 1000, media_type="image/png")

    @app.get("/chats/x/events")
    async def chat_events():
        return JSONResponse({"a": "x" * 5000})

    c = TestClient(app)
    for url in ("/static/sse", "/static/pic.png", "/chats/x/events"):
        r = c.get(url, headers=GZ)
        assert "content-encoding" not in r.headers, url
