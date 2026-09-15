"""Phase 6 tests: web tools (6b). No network: SearXNG and web servers are httpx mock transports."""

from __future__ import annotations

import asyncio
import ipaddress

import httpx
import pytest

from harness.config import WebConfig
from harness.fileops import ToolError
from harness.llm import Completion
from harness.manager import Manager
from harness.web_tools import WebTools, blocked_reason, strip_base64_images

from test_daemon import Script, call, events, make_cfg, wait_status

PUBLIC = "93.184.216.34"


def resolver(table: dict[str, list[str]]):
    async def resolve(host, port):
        return table.get(host, [PUBLIC])
    return resolve


@pytest.mark.parametrize("ip,blocked", [
    ("127.0.0.1", True), ("10.1.2.3", True), ("172.16.0.1", True), ("192.168.1.10", True),
    ("100.101.102.103", True), ("169.254.169.254", True), ("0.0.0.0", True), ("224.0.0.1", True),
    ("::1", True), ("fd7a:115c:a1e0::1", True), ("fe80::1", True), ("::ffff:127.0.0.1", True),
    ("8.8.8.8", False), ("2606:4700:4700::1111", False), (PUBLIC, False),
])
def test_blocked_reason(ip, blocked):
    assert bool(blocked_reason(ipaddress.ip_address(ip))) is blocked


def test_fetch_pins_ip_keeps_host_and_rechecks_redirects():
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.url.host, request.headers["host"], request.extensions.get("sni_hostname")))
        if request.headers["host"] == "example.com":
            return httpx.Response(302, headers={"location": "http://intranet.example.com/admin"})
        return httpx.Response(200, text="secret")

    web = WebTools(WebConfig(enabled=True), resolver=resolver({"intranet.example.com": ["192.168.1.5"]}),
                   transport=httpx.MockTransport(handler))

    async def body():
        with pytest.raises(ToolError, match="private network"):
            await web.web_fetch("https://example.com/start")
    asyncio.run(body())
    assert seen == [(PUBLIC, "example.com", "example.com")]  # the redirect target was never requested


def test_fetch_refuses_names_schemes_and_mixed_dns():
    web = WebTools(WebConfig(enabled=True), resolver=resolver({"rebind.example": [PUBLIC, "127.0.0.1"]}),
                   transport=httpx.MockTransport(lambda r: httpx.Response(200, text="x")))

    async def body():
        for url, message in [("ftp://example.com/", "http and https"), ("http://localhost/", "local or internal"),
                             ("http://tower.tail1234.ts.net/", "local or internal"),
                             ("http://user:pw@example.com/", "credentials"),
                             ("http://rebind.example/", "loopback")]:
            with pytest.raises(ToolError, match=message):
                await web.web_fetch(url)
    asyncio.run(body())


def test_fetch_extracts_pages_and_strips_images():
    article = "".join(f"<p>Paragraph {i} about GPU queue number {i * 7}: " + f"detail {i} " * 60 + "</p>"
                      for i in range(40))  # unique paragraphs: trafilatura drops repeated ones
    html = (f"<html><head><title>Queue notes</title></head><body><nav>menu</nav><article><h1>Queue notes</h1>"
            f"{article}<p>tail marker</p></article></body></html>")

    def handler(request):
        if request.url.path == "/big":
            return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})
        if request.url.path == "/pdf":
            return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
        if request.url.path == "/huge":
            return httpx.Response(200, content=b"a" * 3000, headers={"content-type": "text/plain"})
        return httpx.Response(200, text="![logo](data:image/png;base64,iVBORw0KGgo=) hello",
                              headers={"content-type": "text/plain"})

    web = WebTools(WebConfig(enabled=True, page_chars=5000), resolver=resolver({}),
                   transport=httpx.MockTransport(handler))

    async def body():
        first = await web.web_fetch("https://example.com/big")
        assert first.startswith("[Untrusted web content") and "# Queue notes" in first
        assert "call web_fetch with start=" in first and "tail marker" not in first
        start = int(first.rsplit("start=", 1)[1].split(" ")[0])
        pages, text = 1, first
        while "call web_fetch with start=" in text and pages < 20:
            text = await web.web_fetch("https://example.com/big", start=start)
            pages += 1
            if "start=" in text.rsplit("\n", 1)[-1]:
                start = int(text.rsplit("start=", 1)[1].split(" ")[0])
        assert "tail marker" in text and pages > 2
        found = await web.web_fetch("https://example.com/big", find="tail marker|queue number 259")
        assert "2 matches" in found and "tail marker" in found and len(found) < 3000
        assert "No matches" in await web.web_fetch("https://example.com/big", find="nothing-like-this")
        assert "[IMAGE: logo] hello" in await web.web_fetch("https://example.com/small")
        with pytest.raises(ToolError, match="only HTML and text"):
            await web.web_fetch("https://example.com/pdf")
        small = WebTools(WebConfig(enabled=True, max_bytes=1000), resolver=resolver({}),
                         transport=httpx.MockTransport(handler))
        with pytest.raises(ToolError, match="larger than"):
            await small.web_fetch("https://example.com/huge")
    asyncio.run(body())
    assert strip_base64_images("x data:image/gif;base64,R0lGOD= y") == "x [IMAGE] y"


def test_search_formats_dedupes_and_caches():
    calls = []

    def handler(request):
        calls.append(request.url.params["q"])
        return httpx.Response(200, json={"results": [
            {"url": "https://b.example/", "title": "B", "content": "second", "score": 1},
            {"url": "https://a.example/", "title": "A", "content": "first   hit", "score": 3},
            {"url": "https://a.example/", "title": "A again", "content": "dup", "score": 2},
        ]})

    web = WebTools(WebConfig(enabled=True), transport=httpx.MockTransport(handler))

    async def body():
        out = await web.web_search("gpu queue", limit=5)
        assert out.index("1. A") < out.index("2. B") and "A again" not in out and "first hit" in out
        await web.web_search("gpu queue", limit=5)
        assert calls == ["gpu queue"]
    asyncio.run(body())


def test_web_tools_reach_sessions_and_projects_can_opt_out(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.web = WebConfig(enabled=True)
    cfg.projects["offline"] = type(cfg.projects["scratch"])(name="offline", web=False)

    async def body():
        m = Manager(cfg, chat=Script([Completion(tool_calls=[call("web_search", 0, query="harness")]),
                                      Completion(content="found it")]))
        m.runner.web.transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"results": [
            {"url": "https://x.example/", "title": "X", "content": "about harnesses"}]}))
        await m.start(maintenance=False)
        s = m.create("search")
        await wait_status(m, s["id"], "done")
        result = events(m, s["id"], "tool_result")[0]
        assert result["ok"] and "https://x.example/" in result["output"]
        assert "web_fetch" in m.db.get_session(s["id"])["context"][0]["content"]
        offline = m.create("no web", project="offline")
        assert "web_fetch" not in m.db.get_session(offline["id"])["context"][0]["content"]
        names = {t["function"]["name"] for t in m.runner.tool_schemas(m.db.get_session(offline["id"]),
                                                                       m.runner.workspace(m.db.get_session(offline["id"])))}
        assert "web_search" not in names
        await m.stop()
    asyncio.run(body())
