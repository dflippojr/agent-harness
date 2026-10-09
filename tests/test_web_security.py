"""Host admission and framing policy use synthetic clients and temporary daemon data only."""

from types import SimpleNamespace

import pytest
from fastapi.responses import Response, StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.routing import Route

from harness.api import _host_allowed
from test_api import LOGIN, PUBLIC, make_client


def assert_framing(response):
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert response.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("port,public_url,host,allowed", [
    (8100, PUBLIC, "127.0.0.1:8100", True),
    (8100, PUBLIC, "LOCALHOST:8100", True),
    (8100, PUBLIC, "[::1]:8100", True),
    (8100, PUBLIC, "tower.example.ts.net", True),
    (8100, PUBLIC, "TOWER.EXAMPLE.TS.NET:443", True),
    (80, "", "localhost", True),
    (80, "", "127.0.0.1:80", True),
    (80, "", "[::1]", True),
    (8100, PUBLIC + ":8443", "tower.example.ts.net:8443", True),
    (8100, PUBLIC + ":8443", "tower.example.ts.net", False),
    (8100, PUBLIC + ":8443", "tower.example.ts.net:443", False),
    (8100, "", "tower.example.ts.net", False),
    (8100, "not an origin", "tower.example.ts.net", False),
    (8100, "not an origin", "localhost:8100", True),
    (8100, PUBLIC, "tower.example.ts.net:8100", False),
    (8100, PUBLIC, "localhost", False),
    (8100, PUBLIC, "127.0.0.1:80", False),
    (8100, PUBLIC, "[::1]:8101", False),
])
def test_host_authorities(port, public_url, host, allowed):
    request = Request({"type": "http", "headers": [(b"host", host.encode())]})
    assert _host_allowed(request, SimpleNamespace(port=port, public_url=public_url)) is allowed


def test_allowed_hosts_reach_reads_and_writes(tmp_path):
    client, manager, _ = make_client(tmp_path, [])
    manager.cfg.port = 8100
    with client:
        for host in ("127.0.0.1:8100", "localhost:8100", "[::1]:8100",
                     "tower.example.ts.net", "tower.example.ts.net:443"):
            read = client.get("/health", headers={"Host": host})
            assert read.status_code == 200, read.text
            write = client.put("/profile", headers={"Host": host}, json={"emoji": "🚀"})
            assert write.status_code == 200, write.text
            assert_framing(read)
            assert_framing(write)


def test_foreign_and_malformed_hosts_refused_before_auth_or_routes(tmp_path, monkeypatch):
    from harness import api

    client, manager, _ = make_client(tmp_path, [])
    manager.cfg.port = 8100
    client.local_owner = False

    async def unexpected_auth(*_args):
        raise AssertionError("a refused Host must not reach authentication")

    monkeypatch.setattr(api, "_identity_from_tailscaled", unexpected_auth)
    bad_hosts = ["evil.example", "testserver", "other.tailnet.ts.net", "localhost.evil.example:8100",
                 "tower.example.ts.net.evil.example", "localhost:8101", "127.0.0.2:8100", "0.0.0.0:8100",
                 "::1:8100", "localhost", "localhost:8100/", "user@localhost:8100",
                 "http://localhost:8100", "localhost:8100?x", "localhost:8100#x", " localhost:8100",
                 "localhost:8100 ", "localhost:8100,evil.example", "", "localhost:bad"]
    with client:
        for host in bad_hosts:
            for method, path in (("GET", "/"), ("GET", "/health"), ("POST", "/sessions"),
                                 ("HEAD", "/sw.js"), ("OPTIONS", "/api/v1"),
                                 ("GET", "/api/admin/v1/profile"),
                                 ("POST", "/api/admin/v1/remote-control/discovery/scans")):
                response = client.request(method, path, headers={
                    "Host": host, "Forwarded": "host=localhost:8100", "X-Forwarded-Host": "localhost:8100",
                    "Tailscale-User-Login": LOGIN,
                })
                assert response.status_code == 421, (method, path, host, response.text)
                assert_framing(response)
        duplicate = client.get("/", headers=[("Host", "localhost:8100"), ("Host", "evil.example")])
        assert duplicate.status_code == 421
        assert_framing(duplicate)
        # TestClient synthesizes Host when it is absent; inspect a raw ASGI request for that case.
        assert not _host_allowed(Request({"type": "http", "headers": []}), manager.cfg)
    assert manager.db.list_sessions() == []


@pytest.mark.parametrize("encoding", ["gzip", "identity"])
def test_shell_api_and_error_responses_forbid_framing(tmp_path, encoding):
    client, _, _ = make_client(tmp_path, [])
    with client:
        for path in ("/", "/sw.js", "/static/app.js", "/health", "/api/v1", "/api/admin/v1"):
            response = client.get(path, headers={"Accept-Encoding": encoding})
            assert response.status_code == 200, response.text
            assert_framing(response)
        asset = client.get("/static/app.js", headers={"Accept-Encoding": encoding})
        cached = client.get("/static/app.js", headers={"Accept-Encoding": encoding,
                                                      "If-None-Match": asset.headers["etag"]})
        assert cached.status_code == 304
        assert_framing(cached)
        missing = client.get("/static/missing.js")
        assert missing.status_code == 404
        assert_framing(missing)
        denied = client.get("/health", headers={"Tailscale-User-Login": "intruder@example.com"})
        assert denied.status_code == 403
        assert_framing(denied)
        cross_site = client.post("/sessions", json={"prompt": "refused"}, headers={"Origin": "https://evil.example"})
        assert cross_site.status_code == 403
        assert_framing(cross_site)
        invalid = client.post("/sessions", json={})
        assert invalid.status_code == 422
        assert_framing(invalid)
        client.local_owner = False
        anonymous = client.get("/")
        assert anonymous.status_code == 401
        assert_framing(anonymous)


def test_framing_covers_streams_redirects_and_unhandled_errors(tmp_path):
    client, _, _ = make_client(tmp_path, [])
    client = TestClient(client.app, raise_server_exceptions=False)

    async def events(_request):
        async def stream():
            yield "data: synthetic\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")

    async def failure(_request):
        raise RuntimeError("synthetic route failure")

    async def redirect(_request):
        return Response(status_code=302, headers={"Location": "/"})

    client.app.router.routes[:0] = [Route("/test-events", events), Route("/test-failure", failure),
                                  Route("/test-redirect", redirect)]
    with client:
        streamed = client.get("/test-events")
        assert streamed.status_code == 200 and streamed.text == "data: synthetic\n\n"
        assert_framing(streamed)
        redirected = client.get("/test-redirect", follow_redirects=False)
        assert redirected.status_code == 302
        assert_framing(redirected)
        failed = client.get("/test-failure")
        assert failed.status_code == 500
        assert_framing(failed)
