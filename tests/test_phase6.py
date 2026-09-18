"""Phase 6 tests: web tools (6b). No network: SearXNG and web servers are httpx mock transports."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time

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
        with pytest.raises(ToolError, match="couldn't open the PDF"):  # a broken PDF (real ones: test_phase7)
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


# 6c: inference endpoint
def endpoint_client(tmp_path, handler, **endpoint):
    from fastapi.testclient import TestClient
    from harness.api import create_app
    from harness.config import EndpointConfig

    cfg = make_cfg(tmp_path)
    cfg.endpoint = EndpointConfig(enabled=True, model_aliases={"claude-*": "fake"}, **endpoint)
    m = Manager(cfg, chat=Script([Completion(content="done")]))
    m.endpoint_transport = httpx.MockTransport(handler)
    return TestClient(create_app(m)), m


def test_endpoint_auth_models_and_passthrough(tmp_path):
    seen = []

    def llama(request: httpx.Request):
        body = request.read()
        seen.append((request.url.path, body))
        if request.url.path == "/v1/messages" and json.loads(body).get("stream"):
            sse = (b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":21}}}\n\n'
                   b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
                   b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\n\n')
            return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"id": "x", "choices": [{"message": {"content": "hello"}}],
                                         "usage": {"prompt_tokens": 12, "completion_tokens": 3}})

    client, m = endpoint_client(tmp_path, llama)
    with client:
        assert client.post("/v1/chat/completions", json={"model": "x"}).status_code == 401
        created = client.post("/keys", json={"name": "macbook-zed"}).json()
        key = created["key"]
        assert key.startswith("hk-") and "key" not in client.get("/keys").json()[0]
        auth = {"Authorization": f"Bearer {key}"}

        models = client.get("/v1/models", headers=auth).json()
        assert models["data"][0]["id"] == "fake" and models["data"][0]["type"] == "model"
        assert client.get("/v1/capabilities", headers=auth).json()["features"]["tool_calls"] is True

        r = client.post("/v1/chat/completions", headers=auth, json={"model": "gpt-4o", "messages": []})
        assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hello"
        assert json.loads(seen[-1][1])["model"] == "fake"  # unknown names go to the default model

        with client.stream("POST", "/v1/messages", headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                           json={"model": "claude-sonnet-5", "stream": True, "messages": []}) as s:
            text = b"".join(s.iter_bytes())
        assert b"message_delta" in text and seen[-1][0] == "/v1/messages"

        rows = m.db.conn.execute("SELECT route, model, stream, status, prompt_tokens, completion_tokens "
                                 "FROM endpoint_requests ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [("/v1/chat/completions", "fake", 0, 200, 12, 3),
                                            ("/v1/messages", "fake", 1, 200, 21, 7)]
        from harness.metrics import render
        assert 'harness_endpoint_tokens_total{key="macbook-zed",kind="completion"} 10' in render(m)

        assert client.delete(f"/keys/{created['id']}").status_code == 204
        r = client.post("/v1/messages", headers={"x-api-key": key}, json={"model": "x", "messages": []})
        assert r.status_code == 401 and r.json()["type"] == "error"  # Anthropic error shape


def test_endpoint_refuses_while_gpu_guard_paused(tmp_path):
    client, m = endpoint_client(tmp_path, lambda r: httpx.Response(200, json={"input_tokens": 3}))

    class Paused:
        active, state = True, "paused"

        async def stop(self):
            pass

        def start(self):
            pass

    with client:
        key = client.post("/keys", json={"name": "script"}).json()["key"]
        m.guard = Paused()
        r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={"messages": []})
        assert r.status_code == 503 and r.headers["retry-after"] == "180"
        assert client.post("/v1/messages/count_tokens", headers={"x-api-key": key}, json={}).status_code == 200
        m.guard = None


def test_inference_gate_endpoint_first_with_fairness():
    from harness.scheduler import InferenceGate, QueueFull

    async def body():
        gate = InferenceGate(max_waiting=2, fair_seconds=0.3)
        order = []
        agent = await gate.agent_turn()  # a turn in flight

        async def endpoint(name):
            slot = await gate.endpoint_request()
            order.append(name)
            await asyncio.sleep(0.05)
            await slot.release()

        async def next_agent():
            slot = await gate.agent_turn()
            order.append("agent")
            await slot.release()

        e1 = asyncio.create_task(endpoint("e1"))
        await asyncio.sleep(0.01)
        a2 = asyncio.create_task(next_agent())
        await asyncio.sleep(0.01)
        e2 = asyncio.create_task(endpoint("e2"))
        await asyncio.sleep(0.01)
        with pytest.raises(QueueFull):
            await gate.endpoint_request()
        await agent.release()
        await asyncio.wait_for(asyncio.gather(e1, e2, a2), 10)
        assert order[:2] == ["e1", "e2"] and order[-1] == "agent"  # endpoint requests jumped the waiting agent

        # fairness: an agent call that has waited fair_seconds goes before newly arriving endpoint requests
        order.clear()
        busy = await gate.endpoint_request()
        a3 = asyncio.create_task(next_agent())
        await asyncio.sleep(0.35)
        e3 = asyncio.create_task(endpoint("e3"))
        await asyncio.sleep(0.01)
        await busy.release()
        await asyncio.wait_for(asyncio.gather(a3, e3), 10)
        assert order == ["agent", "e3"]
    asyncio.run(body())


# 6d: image generation
PNG = b"\x89PNG\r\n\x1a\nfake"


class FakeServer:
    """Stands in for gpu_guard.ServerControl (the language model server)."""

    def __init__(self):
        self.calls = []

    async def stop(self):
        self.calls.append("stop")

    async def start(self):
        self.calls.append("start")

    async def healthy(self):
        return True


def fake_comfy(fail_prompts=(), fail_upscale=False):
    state = {"graphs": []}

    def handler(request: httpx.Request):
        if request.url.path == "/prompt":
            graph = json.loads(request.read())["prompt"]
            state["graphs"].append(graph)
            return httpx.Response(200, json={"prompt_id": f"p{len(state['graphs'])}"})
        if request.url.path.startswith("/history/"):
            pid = request.url.path.rsplit("/", 1)[1]
            graph = state["graphs"][int(pid[1:]) - 1]
            if any(n.get("class_type") == "ImageUpscaleWithModel" for n in graph.values()):
                if fail_upscale:
                    return httpx.Response(200, json={pid: {"status": {"status_str": "error", "completed": False,
                        "messages": [["execution_error", {"exception_message": "CUDA out of memory"}]]}}})
                return httpx.Response(200, json={pid: {"status": {"status_str": "success", "completed": True},
                                                       "outputs": {"5": {"images": [{"filename": "up.png",
                                                                                     "subfolder": "harness",
                                                                                     "type": "output"}]}}}})
            text = next(n["inputs"]["text"] for n in graph.values() if n["class_type"] == "CLIPTextEncode")
            if text in fail_prompts:
                return httpx.Response(200, json={pid: {"status": {"status_str": "error", "completed": False, "messages": [
                    ["execution_error", {"exception_message": "CUDA out of memory"}]]}}})
            return httpx.Response(200, json={pid: {"status": {"status_str": "success", "completed": True},
                                                   "outputs": {"9": {"images": [{"filename": "x.png", "subfolder": "harness",
                                                                                  "type": "output"}]}}}})
        if request.url.path == "/view":
            if request.url.params.get("filename") == "up.png":
                graph = state["graphs"][-1]
                node = next(n for n in graph.values() if n.get("class_type") == "ImageScale")
                w, h = int(node["inputs"]["width"]), int(node["inputs"]["height"])
                from PIL import Image
                import io
                buf = io.BytesIO()
                Image.new("RGB", (w, h), (20, 40, 60)).save(buf, format="PNG")
                return httpx.Response(200, content=buf.getvalue())
            return httpx.Response(200, content=PNG)
        return httpx.Response(404)
    return handler, state


def image_manager(tmp_path, steps=None, fail_prompts=(), fail_upscale=False, weights=False):
    from harness.config import ImagesConfig
    cfg = make_cfg(tmp_path)
    upscale_dir = tmp_path / "upscale-weights"
    upscale_dir.mkdir(parents=True, exist_ok=True)
    cfg.images = ImagesConfig(enabled=True, work_dir=str(tmp_path / "img"), linger_seconds=0.2,
                              comfy_dir=str(tmp_path / "comfy"), upscale_dir=str(upscale_dir))
    if weights:
        from harness.upscale import MODELS
        for spec in MODELS.values():
            (upscale_dir / spec.filename).write_bytes(b"fake-weight")
    m = Manager(cfg, chat=Script(steps or [Completion(content="done")]))
    server = FakeServer()
    m.images.control = server
    handler, state = fake_comfy(fail_prompts, fail_upscale=fail_upscale)
    m.images.transport = httpx.MockTransport(handler)

    async def no_process():
        return None
    m.images.comfy.start = no_process
    m.images.comfy.stop = no_process
    return m, server, state


def test_image_batch_takes_gpu_and_gives_it_back(tmp_path):
    from harness.scheduler import GpuExclusive

    async def body():
        m, server, state = image_manager(tmp_path, fail_prompts=("broken",))
        await m.start(maintenance=False)
        a = m.images.submit("a lighthouse at dusk", model="fast", aspect_ratio="16:9")
        b = m.images.submit("broken", model="quality")
        c = m.images.submit("a red bicycle", model="quality", aspect_ratio="3:4", resolution="standard")
        for _ in range(100):
            if m.images.gpu_taken and m.runner.gate.exclusive_active:
                break
            await asyncio.sleep(0.01)
        with pytest.raises(GpuExclusive):
            await m.runner.gate.endpoint_request()
        done = [await m.images.wait(j["id"]) for j in (a, b, c)]
        assert [j["status"] for j in done] == ["done", "failed", "done"]
        assert "CUDA out of memory" in done[1]["error"]
        assert (done[0]["width"], done[0]["height"]) == (1344, 768) and (done[2]["width"], done[2]["height"]) == (864, 1152)
        assert done[1]["resolution"] == "high" and done[2]["resolution"] == "standard"
        assert m.images.path(done[0]).read_bytes() == PNG
        steps = [next(n["inputs"]["steps"] for n in g.values() if n["class_type"] == "KSampler") for g in state["graphs"]]
        assert steps == [8, 50, 50]  # fast = Z-Image-Turbo, quality = Qwen-Image-2512
        assert all(not any(n.get("class_type") == "ImageUpscaleWithModel" for n in g.values()) for g in state["graphs"])
        for _ in range(100):
            if m.images.phase == "idle":
                break
            await asyncio.sleep(0.02)
        assert server.calls == ["stop", "start"]  # one hand-over for the whole batch
        assert not m.runner.gate.exclusive and await m.warmer.state(m.cfg.models["fake"]) != "paused"
        slot = await m.runner.gate.endpoint_request()
        await slot.release()
        await m.stop()
    asyncio.run(body())


def test_agent_generate_image_tool_saves_into_workspace(tmp_path):
    steps = [Completion(tool_calls=[call("generate_image", 0, prompt="app icon", filename="assets/icon")]),
             Completion(content="made the icon")]

    async def body():
        m, server, _ = image_manager(tmp_path, steps=steps)
        await m.start(maintenance=False)
        s = m.create("make an icon")
        await wait_status(m, s["id"], "done", timeout=20)
        result = events(m, s["id"], "tool_result")[0]
        assert result["ok"] and "assets/icon.png" in result["output"]
        assert (tmp_path / "data" / "workspaces" / s["id"] / "assets" / "icon.png").read_bytes() == PNG
        job = m.db.list_images()[0]
        assert job["source"] == "agent" and job["session_id"] == s["id"]
        bad = await m.runner.images.call("generate_image", {"prompt": "x", "filename": "../../escape.png"},
                                         workspace_root=tmp_path / "data" / "workspaces" / s["id"])
        await m.stop()
    with pytest.raises(ToolError, match="escapes the workspace"):
        asyncio.run(body())


def test_images_api_and_generate_image_for_tower_and_mac(tmp_path):
    from fastapi.testclient import TestClient
    from harness.api import create_app
    from harness.config import Project

    m, server, _ = image_manager(tmp_path)
    m.cfg.projects["mac"] = Project(name="mac", target="macbook")
    with TestClient(create_app(m)) as client:
        assert client.post("/images", json={"prompt": "x", "model": "huge"}).status_code == 400
        assert client.post("/images", json={"prompt": "x", "resolution": "poster"}).status_code == 400
        job = client.post("/images", json={"prompt": "a cat", "aspect_ratio": "1:1"}).json()
        for _ in range(200):
            if client.get(f"/images/{job['id']}").json()["status"] == "done":
                break
            time.sleep(0.02)
        r = client.get(f"/images/{job['id']}.png")
        assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
        listing = client.get("/images").json()
        assert listing["images"][0]["id"] == job["id"] and "fast" in listing["status"]["models"]
        assert listing["status"]["resolutions"]["high"]["sizes"]["16:9"] == [1664, 928]
        tower = {"id": "t", "project": "scratch", "target": "tower", "model": "fake", "workspace": str(tmp_path)}
        mac = {**tower, "project": "mac", "target": "macbook"}
        assert "generate_image" in {k.tool_names[0] for k in m.runner.daemon_toolkits(tower)}
        assert "generate_image" in {k.tool_names[0] for k in m.runner.daemon_toolkits(mac)}


def test_image_warmup_holds_gpu_until_cooldown(tmp_path):
    async def body():
        m, server, _ = image_manager(tmp_path)
        await m.start(maintenance=False)
        await m.images.warmup()
        for _ in range(200):
            if m.images.phase == "warm":
                break
            await asyncio.sleep(0.01)
        assert m.images.phase == "warm" and m.images.gpu_taken
        assert server.calls == ["stop"]
        assert m.images.status()["phase"] == "warm"
        m.images.cooldown()
        for _ in range(200):
            if m.images.phase == "idle":
                break
            await asyncio.sleep(0.02)
        assert m.images.phase == "idle" and not m.images.gpu_taken
        assert server.calls == ["stop", "start"]
        await m.stop()
    asyncio.run(body())


def test_gpu_hold_prevents_comfyui_warmup(tmp_path):
    async def body():
        from types import SimpleNamespace

        m, server, _ = image_manager(tmp_path)
        m.runner.guard = SimpleNamespace(active=False, manual=True)
        await m.start(maintenance=False)
        await m.images.warmup()
        for _ in range(20):
            if m.images.phase == "waiting":
                break
            await asyncio.sleep(0.01)
        assert m.images.phase == "waiting"
        assert server.calls == []
        await m.stop()
    asyncio.run(body())


def test_resume_without_pending_images_does_not_leave_a_drain_barrier(tmp_path):
    m, _, _ = image_manager(tmp_path)
    m.images.drain_after_sessions({"old-session"})
    assert m.images._drain_sessions == set()


def test_held_images_drain_after_waiting_sessions(tmp_path):
    async def body():
        from types import SimpleNamespace

        m, server, _ = image_manager(tmp_path)
        guard = SimpleNamespace(active=False, manual=True)
        m.runner.guard = guard
        m.scheduler.set_paused(True)
        a = asyncio.create_task(m.scheduler.acquire("a"))
        b = asyncio.create_task(m.scheduler.acquire("b"))
        await asyncio.sleep(0)
        await m.start(maintenance=False)
        job = m.images.submit("queued landscape")
        for _ in range(100):
            if m.images.phase == "waiting":
                break
            await asyncio.sleep(0.01)
        assert server.calls == []

        guard.manual = False
        m.images.drain_after_sessions(m.scheduler.positions())
        m.scheduler.set_paused(False)
        await a
        await asyncio.sleep(0.05)
        assert server.calls == []
        m.scheduler.release("a")
        await b
        await asyncio.sleep(0.05)
        assert server.calls == []
        m.scheduler.release("b")
        done = await m.images.wait(job["id"])
        assert done["status"] == "done"
        assert server.calls == ["stop", "start"]
        await m.stop()
    asyncio.run(body())


def test_image_batch_does_not_linger_after_last_job(tmp_path):
    async def body():
        m, server, _ = image_manager(tmp_path)
        await m.start(maintenance=False)
        job = m.images.submit("a red cube")
        done = await m.images.wait(job["id"])
        assert done["status"] == "done"
        for _ in range(200):
            if m.images.phase == "idle":
                break
            await asyncio.sleep(0.01)
        assert m.images.phase == "idle"
        assert "lingering" not in server.calls
        assert server.calls == ["stop", "start"]
        await m.stop()
    asyncio.run(body())


def test_comfy_progress_event_sets_value_and_max(tmp_path):
    m, _, _ = image_manager(tmp_path)
    m.images.apply_comfy_progress("abc", time.time() - 2, {"type": "status", "data": {}})
    assert m.images.progress == {}
    m.images.apply_comfy_progress("abc", time.time() - 2, {"type": "progress", "data": {"value": 3, "max": 8}})
    progress = m.images.status()["progress"]
    assert progress["value"] == 3 and progress["max"] == 8 and progress["job"] == "abc"


def test_images_warmup_api(tmp_path):
    from fastapi.testclient import TestClient
    from harness.api import create_app

    m, server, _ = image_manager(tmp_path)
    with TestClient(create_app(m)) as client:
        assert client.post("/images/warmup").status_code == 200
        for _ in range(200):
            if client.get("/images").json()["status"]["phase"] == "warm":
                break
            time.sleep(0.02)
        assert client.get("/images").json()["status"]["phase"] == "warm"
        assert server.calls == ["stop"]
        assert client.post("/images/cooldown").status_code == 200
        for _ in range(200):
            if client.get("/images").json()["status"]["phase"] == "idle":
                break
            time.sleep(0.02)
        assert client.get("/images").json()["status"]["phase"] == "idle"
        assert server.calls == ["stop", "start"]


def test_agent_generate_image_saves_into_mac_workspace(tmp_path):
    from harness.config import ImagesConfig
    from test_phase4 import FakeRunner, executor, mac_cfg

    steps = [Completion(tool_calls=[call("generate_image", 0, prompt="app icon", filename="assets/icon")]),
             Completion(content="made the icon")]
    cfg = mac_cfg(tmp_path)
    cfg.images = ImagesConfig(enabled=True, work_dir=str(tmp_path / "img"), linger_seconds=0.2)
    m = Manager(cfg, chat=Script(steps))
    m.images.control = FakeServer()
    handler, _ = fake_comfy()
    m.images.transport = httpx.MockTransport(handler)

    async def no_process():
        return None
    m.images.comfy.start = no_process
    m.images.comfy.stop = no_process

    async def body():
        runner = FakeRunner(m.hub, executor(tmp_path, [tmp_path])).start()
        await m.start(maintenance=False)
        s = m.create("make an icon", project="mac-scratch")
        await wait_status(m, s["id"], "done", timeout=20)
        result = events(m, s["id"], "tool_result")[0]
        assert result["ok"] and "assets/icon.png" in result["output"]
        assert (tmp_path / "mac-workspaces" / s["id"] / "assets" / "icon.png").read_bytes() == PNG
        assert not (tmp_path / "data" / "workspaces").exists() or not any((tmp_path / "data" / "workspaces").rglob("*.png"))
        job = m.db.list_images()[0]
        assert job["source"] == "agent" and job["session_id"] == s["id"]
        assert "put_file" in runner.seen_ops
        await runner.stop()
        await m.stop()
    asyncio.run(body())


# 6e: app API
def app_client(tmp_path, steps):
    from fastapi.testclient import TestClient
    from harness.api import create_app
    cfg = make_cfg(tmp_path)
    m = Manager(cfg, chat=Script(steps))
    return TestClient(create_app(m)), m


def wait_for(fn, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("condition not met in time")


def test_app_session_with_context_and_app_tool(tmp_path):
    steps = [Completion(tool_calls=[call("lookup_order", 0, order_id="A-17")]),
             Completion(content="Order A-17 ships Friday.")]
    client, m = app_client(tmp_path, steps)
    with client:
        app_key = client.post("/keys", json={"name": "shop-bot", "kind": "app", "scopes": ["sessions"]}).json()
        assert app_key["key"].startswith("ha-") and app_key["scopes"] == "sessions"
        auth = {"Authorization": f"Bearer {app_key['key']}"}
        assert client.get("/api/v1").json()["features"]["app_tools"] is True

        bad = client.post("/api/v1/sessions", headers=auth, json={"prompt": "x", "tools": [
            {"name": "run_shell", "description": "clash"}]})
        assert bad.status_code == 400 and "already taken" in bad.json()["detail"]

        s = client.post("/api/v1/sessions", headers=auth, json={
            "prompt": "When does order A-17 ship?", "metadata": {"ticket": 991},
            "context": [{"title": "Customer", "content": "Dana, premium plan"}],
            "tools": [{"name": "lookup_order", "description": "Look up an order by id",
                       "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}},
                                      "required": ["order_id"]}}]}).json()
        sid = s["id"]
        assert s["app_tools"] == ["lookup_order"] and s["metadata"] == {"ticket": 991}
        system = m.db.get_session(sid)["context"][0]["content"]
        assert 'Context from the app "shop-bot"' in system and "Dana, premium plan" in system

        pending = wait_for(lambda: client.get(f"/api/v1/sessions/{sid}/tool_calls", headers=auth).json())
        assert pending[0]["name"] == "lookup_order" and pending[0]["args"] == {"order_id": "A-17"}
        wait_for(lambda: client.get(f"/api/v1/sessions/{sid}", headers=auth).json()["status"] == "waiting_app", 15)
        assert m.scheduler.holder is None  # the GPU slot is free while the app works
        r = client.post(f"/api/v1/sessions/{sid}/tool_calls/{pending[0]['call_id']}", headers=auth,
                        json={"output": "ships Friday"})
        assert r.status_code == 200
        assert client.post(f"/api/v1/sessions/{sid}/tool_calls/{pending[0]['call_id']}", headers=auth,
                           json={"output": "again"}).status_code == 409
        done = wait_for(lambda: (lambda d: d if d["status"] == "done" else None)(
            client.get(f"/api/v1/sessions/{sid}", headers=auth).json()))
        assert done["answer"] == "Order A-17 ships Friday."
        tool_msg = [c for c in m.db.get_session(sid)["context"] if c["role"] == "tool"][0]
        assert tool_msg["content"] == "ships Friday"

        text = client.get(f"/api/v1/sessions/{sid}/events?follow=false", headers=auth).text
        assert "event: app_tool_call" in text and "event: app_tool_result" in text

        # context mid-session, delivered as its own event
        client.post(f"/api/v1/sessions/{sid}/context", headers=auth,
                    json={"context": [{"title": "Update", "content": "carrier changed"}]})
        assert any(e["type"] == "app_context" for e in m.db.events(sid))


def test_app_scopes_and_isolation(tmp_path):
    client, m = app_client(tmp_path, [Completion(content="hi")])
    with client:
        a = client.post("/keys", json={"name": "a", "kind": "app", "scopes": ["sessions"]}).json()["key"]
        b = client.post("/keys", json={"name": "b", "kind": "app", "scopes": ["sessions"]}).json()["key"]
        device = client.post("/keys", json={"name": "zed"}).json()["key"]  # inference only
        reader = client.post("/keys", json={"name": "dash", "kind": "app", "scopes": ["sessions:all"]}).json()["key"]
        assert client.post("/keys", json={"name": "x", "scopes": ["root"]}).status_code == 400
        H = lambda k: {"Authorization": f"Bearer {k}"}  # noqa: E731

        sid = client.post("/api/v1/sessions", headers=H(a), json={"prompt": "hello"}).json()["id"]
        own = client.post("/sessions", json={"prompt": "the user's own task"}).json()
        assert client.post("/api/v1/sessions", headers=H(device), json={"prompt": "x"}).status_code == 403
        assert client.get(f"/api/v1/sessions/{sid}", headers=H(b)).status_code == 404
        assert client.get(f"/api/v1/sessions/{own['id']}", headers=H(a)).status_code == 404
        assert [s["id"] for s in client.get("/api/v1/sessions", headers=H(a)).json()] == [sid]
        assert client.get(f"/api/v1/sessions/{own['id']}", headers=H(reader)).status_code == 200
        assert client.post("/api/v1/sessions", headers=H(reader), json={"prompt": "x"}).status_code == 403
        assert client.post(f"/api/v1/sessions/{sid}/approvals/pending", headers=H(a),
                           json={"decision": "approve"}).status_code == 403
        assert client.get("/api/v1/sessions", headers={"Authorization": "Bearer nope"}).status_code == 401
        # app tokens without the inference scope can't use the model endpoint
        m.cfg.endpoint.enabled = True
        assert client.post("/v1/chat/completions", headers=H(a), json={"messages": []}).status_code == 401


def test_sessions_all_read_vs_mutation_matrix(tmp_path):
    client, m = app_client(tmp_path, [Completion(content="ok")])
    with client:
        a = client.post("/keys", json={"name": "app-a", "kind": "app",
                                       "scopes": ["sessions", "approvals"]}).json()["key"]
        b = client.post("/keys", json={"name": "app-b", "kind": "app", "scopes": ["sessions"]}).json()["key"]
        reader = client.post("/keys", json={"name": "reader", "kind": "app",
                                            "scopes": ["sessions:all"]}).json()["key"]
        broad = client.post("/keys", json={"name": "broad", "kind": "app",
                                           "scopes": ["sessions", "sessions:all"]}).json()["key"]
        wide = client.post("/keys", json={"name": "wide", "kind": "app",
                                          "scopes": ["sessions", "sessions:all", "approvals"]}).json()["key"]
        owner = client.post("/keys", json={"name": "control-center", "kind": "owner",
                                           "scopes": ["admin"]}).json()["key"]
        H = lambda k: {"Authorization": f"Bearer {k}"}  # noqa: E731

        owner_sid = client.post("/sessions", json={"prompt": "owner task"}).json()["id"]
        a_sid = client.post("/api/v1/sessions", headers=H(a), json={"prompt": "a task"}).json()["id"]
        b_sid = client.post("/api/v1/sessions", headers=H(b), json={"prompt": "b task"}).json()["id"]
        sessions = {"owner": owner_sid, "a": a_sid, "b": b_sid}
        for sid in sessions.values():
            wait_for(lambda sid=sid: client.get(f"/sessions/{sid}").json()["status"] == "done")

        def event_count(sid, type_):
            return sum(1 for e in m.db.events(sid) if e["type"] == type_)

        read_rows = [
            ("a", a, {"owner": 404, "a": 200, "b": 404}, {a_sid}),
            ("b", b, {"owner": 404, "a": 404, "b": 200}, {b_sid}),
            ("reader", reader, {"owner": 200, "a": 200, "b": 200}, {owner_sid, a_sid, b_sid}),
            ("broad", broad, {"owner": 200, "a": 200, "b": 200}, {owner_sid, a_sid, b_sid}),
            ("owner", owner, {"owner": 200, "a": 200, "b": 200}, {owner_sid, a_sid, b_sid}),
        ]
        read_paths = (
            "/api/v1/sessions/{sid}",
            "/api/v1/sessions/{sid}/events?follow=false",
            "/api/v1/sessions/{sid}/tool_calls",
            "/api/v1/sessions/{sid}/approvals",
        )
        for name, token, expected, listed in read_rows:
            for label, sid in sessions.items():
                for path in read_paths:
                    r = client.get(path.format(sid=sid), headers=H(token))
                    assert r.status_code == expected[label], (name, path, label, r.status_code, r.text)
            assert {s["id"] for s in client.get("/api/v1/sessions", headers=H(token)).json()} == listed

        mutate_rows = ((a, {a_sid}), (b, {b_sid}), (broad, set()), (owner, {owner_sid, a_sid, b_sid}))
        for token, allowed_sids in mutate_rows:
            for label, sid in sessions.items():
                payload = f"injected-{token[-8:]}-{label}"
                msg, ctx = {"content": payload}, {"context": [{"title": "Injected", "content": payload}]}
                before_msg, before_ctx = event_count(sid, "user_message"), event_count(sid, "app_context")
                cancel = client.post(f"/api/v1/sessions/{sid}/cancel", headers=H(token))
                send = client.post(f"/api/v1/sessions/{sid}/messages", headers=H(token), json=msg)
                added = client.post(f"/api/v1/sessions/{sid}/context", headers=H(token), json=ctx)
                if sid in allowed_sids:
                    assert cancel.status_code in (200, 409), (label, cancel.status_code, cancel.text)
                    assert send.status_code == 200, (label, send.status_code, send.text)
                    assert added.status_code == 200, (label, added.status_code, added.text)
                    assert event_count(sid, "user_message") == before_msg + 1
                    assert event_count(sid, "app_context") == before_ctx + 1
                else:
                    assert send.status_code == 404, (label, send.status_code, send.text)
                    assert added.status_code == 404
                    assert cancel.status_code == 404
                    assert event_count(sid, "user_message") == before_msg
                    assert event_count(sid, "app_context") == before_ctx
                    assert not any(payload in (e["data"].get("content") or "") for e in m.db.events(sid)
                                   if e["type"] in ("user_message", "app_context"))

        reader_msg, reader_ctx = {"content": "reader-inject"}, {"context": [{"title": "x", "content": "y"}]}
        for sid in sessions.values():
            before = event_count(sid, "user_message")
            assert client.post(f"/api/v1/sessions/{sid}/messages", headers=H(reader),
                               json=reader_msg).status_code == 403
            assert client.post(f"/api/v1/sessions/{sid}/context", headers=H(reader),
                               json=reader_ctx).status_code == 403
            assert client.post(f"/api/v1/sessions/{sid}/cancel", headers=H(reader)).status_code == 403
            assert event_count(sid, "user_message") == before

        m.db.insert_app_tool_call(a_sid, "call-a", "lookup", {"x": 1})
        assert client.get(f"/api/v1/sessions/{a_sid}/tool_calls", headers=H(broad)).status_code == 200
        assert client.post(f"/api/v1/sessions/{a_sid}/tool_calls/call-a", headers=H(broad),
                           json={"output": "nope"}).status_code == 404
        assert client.post(f"/api/v1/sessions/{a_sid}/tool_calls/call-a", headers=H(b),
                           json={"output": "nope"}).status_code == 404
        owner_tool = client.post(f"/api/v1/sessions/{a_sid}/tool_calls/call-a", headers=H(owner),
                                 json={"output": "nope"})
        assert owner_tool.status_code == 403 and "only the app that registered" in owner_tool.json()["detail"]
        assert client.post(f"/api/v1/sessions/{a_sid}/tool_calls/call-a", headers=H(a),
                           json={"output": "yes"}).status_code == 200

        m.db.insert_approval({"id": "appr-a", "session_id": a_sid, "tool_call_id": "native-1",
                              "tool": "run_shell", "args": {"command": "echo"}, "reason": "ask"})
        m.db.insert_approval({"id": "appr-o", "session_id": owner_sid, "tool_call_id": "native-2",
                              "tool": "run_shell", "args": {"command": "echo"}, "reason": "ask"})
        assert client.get(f"/api/v1/sessions/{a_sid}/approvals", headers=H(broad)).status_code == 200
        assert client.post(f"/api/v1/sessions/{a_sid}/approvals/appr-a", headers=H(wide),
                           json={"decision": "approve"}).status_code == 404
        assert client.post(f"/api/v1/sessions/{owner_sid}/approvals/appr-o", headers=H(wide),
                           json={"decision": "approve"}).status_code == 404
        assert client.post(f"/api/v1/sessions/{a_sid}/approvals/appr-a", headers=H(a),
                           json={"decision": "approve", "note": "mine"}).status_code == 200
        decided = client.post(f"/api/v1/sessions/{owner_sid}/approvals/appr-o", headers=H(owner),
                              json={"decision": "deny", "note": "owner"})
        assert decided.status_code == 200 and decided.json()["status"] == "denied"

        bundled = client.post(f"/api/v1/sessions/{owner_sid}/messages", json={"content": "from bundled"})
        assert bundled.status_code == 200


def test_setup_config_writes_a_loadable_config(tmp_path, capsys):
    from harness import config, setup_config
    args = ["--config-dir", str(tmp_path / "cfg"), "--data-dir", str(tmp_path / "data"), "--model", "gpt-oss",
            "--port", "8200", "--pause-flag", str(tmp_path / "paused")]
    assert setup_config.main(args) == 0
    cfg = config.load(tmp_path / "cfg")
    assert cfg.default_model == "gpt-oss-20b" and cfg.port == 8200 and cfg.endpoint.enabled and not cfg.web.enabled
    assert cfg.models["gpt-oss-20b"].context_tokens == 32768 and list(cfg.projects) == ["scratch"]
    (tmp_path / "cfg" / "projects.yaml").write_text("projects:\n  mine: {}\n")
    setup_config.main(args)  # keeps edited files unless --force
    assert "mine" in config.load(tmp_path / "cfg").projects
    setup_config.main(args + ["--force"])
    assert "mine" not in config.load(tmp_path / "cfg").projects
