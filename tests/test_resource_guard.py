"""Resource guard (#311): lazy model loading, "Load local model now", the RAM check. Fakes only; no GPU needed."""

from __future__ import annotations

import asyncio
import time

import pytest

from harness.config import GpuGuardConfig
from harness.gpu_guard import CLEAR, GIB, PAUSED, MemoryWatch
from harness.llm import Completion
from harness.manager import Manager
from harness.warmup import LOW_MEMORY, PAUSED as MODEL_PAUSED, READY, UNLOADED, WAKING, ModelWarmer

from test_daemon import Script, events, make_cfg, wait_status
from test_phase5 import GAME, FakeControl, FakeDetect, make_guard


@pytest.fixture(autouse=True)
def parked_aware_state(monkeypatch):
    """conftest reports every model as ready; here a parked server must read as unloaded."""
    async def state(self, model):
        if self.blocked():
            return MODEL_PAUSED
        if model.name in self._waking and not self._waking[model.name].done():
            return WAKING
        return UNLOADED if self.parked(model) else READY
    monkeypatch.setattr(ModelWarmer, "state", state)


class FakeRam:
    def __init__(self, available_gb: float = 16):
        self.available = available_gb * GIB

    def __call__(self):
        return {"available": int(self.available), "total": 32 * GIB, "commit": 20 * GIB, "commit_limit": 40 * GIB}


def guarded_manager(tmp_path, steps=None, ram: FakeRam | None = None, **guard_cfg):
    cfg = make_cfg(tmp_path)
    cfg.gpu_guard = GpuGuardConfig(enabled=True, resume_after_seconds=0, poll_seconds=3600, **guard_cfg)
    m = Manager(cfg, chat=Script(steps or [Completion(content="done")]))
    m.guard.detector, m.guard.control = FakeDetect(), FakeControl()
    m.guard.memory = MemoryWatch(cfg.gpu_guard.min_available_ram_gb, read=ram or FakeRam(), ttl=0)
    m.runner.ram = m.guard.memory
    return m


async def hold_and_release(m) -> None:
    m.guard.detector.signals = [GAME]
    await m.guard.check()
    await m.guard.check()
    assert m.guard.state == PAUSED
    m.guard.detector.signals = []
    await m.guard.check()


# lazy resume
def test_hold_ending_with_nothing_queued_leaves_model_unloaded():
    async def body():
        guard, detect, control, scheduler, log = make_guard(lazy_load=True, resume_after_seconds=0)
        detect.signals = [GAME]
        await guard.check()
        assert guard.state == PAUSED and control.flag
        detect.signals = []
        await guard.check()
        assert guard.state == CLEAR
        assert not scheduler.paused
        assert control.starts == 0 and control.flag  # parked: nothing loads the model
        assert guard.status()["parked"]
        assert [k for k, _ in log] == ["pause", "resume"]
    asyncio.run(body())


def test_hold_ending_with_work_waiting_reloads_the_model():
    async def body():
        guard, detect, control, scheduler, _ = make_guard(lazy_load=True, resume_after_seconds=0)
        guard.want_model = lambda: True
        detect.signals = [GAME]
        await guard.check()
        detect.signals = []
        await guard.check()
        await guard.check()
        assert control.starts == 1 and not control.flag
        assert guard.state == CLEAR
    asyncio.run(body())


def test_manual_resume_does_not_load_the_model():
    async def body():
        guard, _, control, scheduler, _ = make_guard(lazy_load=True, resume_after_seconds=999)
        guard.pause()
        await guard.check()
        assert guard.state == PAUSED
        guard.resume()
        await guard.check()
        assert guard.state == CLEAR and control.starts == 0 and control.flag
    asyncio.run(body())


def test_daemon_start_with_parked_model_stays_clear_and_parked():
    async def body():
        guard, _, control, scheduler, log = make_guard(lazy_load=True, poll_seconds=3600)
        control.flag = True
        guard.start()
        await asyncio.sleep(0.05)
        assert guard.state == CLEAR and control.flag and not scheduler.paused
        assert log == []
        await guard.stop()
    asyncio.run(body())


def test_next_turn_after_a_hold_loads_the_model_and_shows_waking(tmp_path):
    async def body():
        m = guarded_manager(tmp_path)
        await m.start(maintenance=False)
        await hold_and_release(m)
        control = m.guard.control
        assert m.guard.state == CLEAR and control.flag and control.starts == 0
        assert await m.warmer.state(m.cfg.models[m.cfg.default_model]) == UNLOADED
        s = m.create("hello")
        await wait_status(m, s["id"], "done")
        assert control.starts == 1 and not control.flag
        assert events(m, s["id"], "model_waking")
        await m.stop()
    asyncio.run(body())


def test_unload_parks_without_holding_the_queue(tmp_path):
    async def body():
        m = guarded_manager(tmp_path)
        await m.start(maintenance=False)
        assert await m.guard.unload()
        assert m.guard.control.flag and m.guard.state == CLEAR and not m.scheduler.paused
        m.runner.generating.add("busy")
        assert not await m.guard.unload()  # a turn in flight
        m.runner.generating.discard("busy")
        await m.stop()
    asyncio.run(body())


# warmer: explicit selection and "Load local model now"
def test_selection_warm_loads_only_with_headroom(tmp_path):
    async def body():
        ram = FakeRam(2)
        m = guarded_manager(tmp_path, ram=ram)
        model = m.cfg.models[m.cfg.default_model]
        m.guard.control.flag = True
        assert await m.warmer.warm(model) == LOW_MEMORY
        assert m.guard.control.starts == 0
        ram.available = 16 * GIB
        assert await m.warmer.warm(model) == UNLOADED
        await m.warmer.ensure_loaded(model)
        assert m.guard.control.starts == 1 and not m.guard.control.flag
    asyncio.run(body())


def test_load_now_pins_and_keeps_alive(tmp_path, monkeypatch):
    pings = []

    async def ping(self, model, verb):
        pings.append(verb)
    monkeypatch.setattr(ModelWarmer, "_ping", ping)

    async def body():
        m = guarded_manager(tmp_path)
        model = m.cfg.models[m.cfg.default_model]
        m.guard.control.flag = True
        m.warmer.keepalive_seconds = 0.05
        assert await m.warmer.load_now(model, 0.3) == UNLOADED
        assert m.warmer.pinned()
        await asyncio.sleep(0.2)
        assert m.guard.control.starts == 1
        assert "kept" in pings
        await asyncio.sleep(0.3)
        assert not m.warmer.pinned() and m.warmer.pinned_until is None
    asyncio.run(body())


def test_resources_api_load_warns_under_memory_pressure_and_unload(tmp_path):
    from fastapi.testclient import TestClient
    from harness.api import create_app

    ram = FakeRam(2)
    m = guarded_manager(tmp_path, ram=ram)
    with TestClient(create_app(m)) as client:
        m.guard.control.flag = True
        status = client.get("/resources").json()
        assert status["model"]["state"] == "unloaded"
        assert status["memory"]["low"] and status["load_now_default_minutes"] == 60
        assert client.get("/gpu").json()["model"]["state"] == "unloaded"  # the old path is an alias
        refused = client.post("/resources/load", json={"duration_seconds": 600})
        assert refused.status_code == 409
        assert "low memory" in refused.text and "low_memory" in refused.text
        assert m.guard.control.starts == 0
        loaded = client.post("/resources/load", json={"duration_seconds": 600, "force": True})
        assert loaded.status_code == 200
        assert loaded.json()["model"]["pinned_until"] > time.time() + 500
        assert client.post("/resources/load", json={"duration_seconds": 5}).status_code == 400
        unloaded = client.post("/resources/unload")
        assert unloaded.status_code == 200
        assert unloaded.json()["model"]["pinned_until"] is None
        assert m.guard.control.flag
        diag = client.get("/resources/diagnostics").json()
        assert {"as_of", "vram", "ram", "gpu_load", "cpu_load", "model", "guard"} <= set(diag)
        assert diag["ram"]["available_bytes"] == 2 * GIB


# RAM check
def test_memory_watch_threshold():
    ram = FakeRam(3)
    watch = MemoryWatch(4, read=ram, ttl=0)
    assert watch.low()
    ram.available = 5 * GIB
    assert not watch.low()
    assert not MemoryWatch(0, read=FakeRam(0.1), ttl=0).low()  # 0 turns the check off
    assert not MemoryWatch(4, read=lambda: None, ttl=0).low()  # no reading: don't block work


def test_session_waits_for_memory_before_loading_then_continues(tmp_path, monkeypatch):
    from harness import gpu_guard
    monkeypatch.setattr(gpu_guard, "MEMORY_POLL_SECONDS", 0.02)

    async def body():
        ram = FakeRam(2)
        m = guarded_manager(tmp_path, ram=ram)
        await m.start(maintenance=False)
        m.guard.control.flag = True  # parked
        s = m.create("hello")
        for _ in range(200):
            if events(m, s["id"], "waiting_memory"):
                break
            await asyncio.sleep(0.01)
        waiting = events(m, s["id"], "waiting_memory")[0]
        assert "RAM available" in waiting["reason"] and waiting["waiting_for"] == "local model"
        assert m.guard.control.starts == 0
        note = next(m.notifier.build(e) for e in m.db.events(s["id"]) if e["type"] == "waiting_memory")
        assert note["title"].startswith("Waiting for memory") and "Actions → Resources" in note["message"]
        ram.available = 16 * GIB
        await wait_status(m, s["id"], "done", timeout=15)
        assert events(m, s["id"], "memory_recovered")
        assert m.guard.control.starts == 1
        await m.stop()
    asyncio.run(body())


def test_worker_container_start_waits_for_memory(tmp_path, monkeypatch):
    from harness import gpu_guard
    monkeypatch.setattr(gpu_guard, "MEMORY_POLL_SECONDS", 0.02)

    async def body():
        ram = FakeRam(2)
        m = guarded_manager(tmp_path, ram=ram)
        s = m.create("hello")
        gate = asyncio.create_task(m.runner._memory_gate(s["id"], "claude worker container"))
        await asyncio.sleep(0.1)
        assert not gate.done()
        assert events(m, s["id"], "waiting_memory")[0]["waiting_for"] == "claude worker container"
        ram.available = 16 * GIB
        await asyncio.wait_for(gate, 2)
        assert events(m, s["id"], "memory_recovered")
    asyncio.run(body())


def test_cli_run_checks_memory_before_starting_a_container():
    import inspect
    from harness.runner import Runner
    source = inspect.getsource(Runner._run_cli)
    assert source.index("_memory_gate") < source.index("_start_cli")


def test_image_batch_waits_for_memory_and_stays_parked(tmp_path, monkeypatch):
    from harness import gpu_guard
    from test_phase6 import image_manager
    monkeypatch.setattr(gpu_guard, "MEMORY_POLL_SECONDS", 0.02)

    async def body():
        m, server, _ = image_manager(tmp_path)
        low = {"v": True}
        m.images.memory_low = lambda: low["v"]
        m.images.want_model = lambda: False
        started = []

        async def comfy_start():
            started.append(True)
        m.images.comfy.start = comfy_start
        await m.start(maintenance=False)
        job = m.images.submit("a lighthouse at dusk", model="fast")
        for _ in range(200):
            if m.images.phase == "waiting_memory":
                break
            await asyncio.sleep(0.01)
        assert m.images.phase == "waiting_memory" and not started
        low["v"] = False
        done = await m.images.wait(job["id"])
        assert done["status"] == "done" and started
        assert server.calls == ["stop"]  # lazy: Qwen stays parked after the batch
        await m.stop()
    asyncio.run(body())


def test_endpoint_loads_parked_model_and_refuses_under_memory_pressure(tmp_path):
    import httpx
    from fastapi.testclient import TestClient
    from harness.api import create_app
    from harness.config import EndpointConfig

    ram = FakeRam(2)
    m = guarded_manager(tmp_path, ram=ram)
    m.cfg.endpoint = EndpointConfig(enabled=True)
    m.endpoint_transport = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "stop",
                                                              "message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
    with TestClient(create_app(m)) as client:
        key = client.post("/keys", json={"name": "script"}).json()["key"]
        m.guard.control.flag = True
        headers = {"Authorization": f"Bearer {key}"}
        body = {"messages": [{"role": "user", "content": "hi"}]}
        refused = client.post("/v1/chat/completions", json=body, headers=headers)
        assert refused.status_code == 503 and "low on memory" in refused.text
        assert m.guard.control.starts == 0
        ram.available = 16 * GIB
        ok = client.post("/v1/chat/completions", json=body, headers=headers)
        assert ok.status_code == 200
        assert m.guard.control.starts == 1 and not m.guard.control.flag


def test_metrics_export_resources(tmp_path):
    from harness.metrics import render
    m = guarded_manager(tmp_path, ram=FakeRam(2))
    m.guard.control.flag = True
    text = render(m)
    assert f"harness_resource_ram_available_bytes {float(2 * GIB):g}" in text
    assert "harness_resource_memory_low 1" in text
    assert "harness_model_parked 1" in text
    assert "harness_model_pinned_until_seconds 0" in text


# review round 1 (PR #342): races between Unload now, lazy loading, and model calls
def test_refused_unload_keeps_the_pin_and_keepalive(tmp_path):
    from fastapi.testclient import TestClient
    from harness.api import create_app

    m = guarded_manager(tmp_path)
    m.warmer.keepalive_seconds = 3600
    with TestClient(create_app(m)) as client:
        assert client.post("/resources/load", json={"duration_seconds": 3600}).status_code == 200
        pinned_until, keepalive = m.warmer.pinned_until, m.warmer._keepalive
        assert keepalive is not None and not keepalive.done()
        m.runner.generating.add("busy")  # a local turn is generating
        refused = client.post("/resources/unload")
        assert refused.status_code == 409
        assert m.warmer.pinned_until == pinned_until and m.warmer._keepalive is keepalive
        assert not keepalive.done() and m.guard.control.stops == 0
        m.runner.generating.discard("busy")
        assert client.post("/resources/unload").status_code == 200
        assert m.warmer.pinned_until is None and m.guard.control.flag


def test_unload_while_a_turn_loads_the_model_is_refused(tmp_path):
    from harness.llm import LLMError
    unloads = []

    async def body():
        m = guarded_manager(tmp_path)
        control = m.guard.control

        def answer(messages):
            if control.flag:  # llama-server is stopped
                return LLMError("model server unreachable: ConnectError")
            return Completion(content="done")
        m.runner.chat = Script([answer])
        healthy = control.healthy

        async def healthy_then_unload():
            unloads.append(await m.guard.unload())  # Unload now, between unparking and generating
            return await healthy()
        control.healthy = healthy_then_unload
        await m.start(maintenance=False)
        control.flag = True
        s = m.create("hello")
        await wait_status(m, s["id"], "done")
        assert unloads and not any(unloads)
        assert control.stops == 0
        await m.stop()
    asyncio.run(body())


def test_model_call_retries_when_the_server_was_parked_under_it(tmp_path):
    from harness.llm import LLMError
    calls = []

    async def body():
        m = guarded_manager(tmp_path)
        control = m.guard.control

        def answer(messages):
            calls.append(control.flag)
            if len(calls) == 1:
                control.flag = True  # parked outside the guard (the logon park, a manual flag) mid-call
                return LLMError("model server unreachable: ConnectError")
            return Completion(content="done")
        m.runner.chat = Script([answer])
        await m.start(maintenance=False)
        s = m.create("hello")
        await wait_status(m, s["id"], "done")
        assert calls == [False, False] and control.starts == 1
        assert events(m, s["id"], "llm_retry")
        await m.stop()
    asyncio.run(body())


def test_count_tokens_wakes_a_parked_model_and_gets_503_during_a_hold(tmp_path):
    import httpx
    from fastapi.testclient import TestClient
    from harness.api import create_app
    from harness.config import EndpointConfig

    m = guarded_manager(tmp_path)
    m.cfg.endpoint = EndpointConfig(enabled=True)

    def upstream(request):
        if m.guard.control.flag:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"input_tokens": 3})
    m.endpoint_transport = httpx.MockTransport(upstream)
    with TestClient(create_app(m)) as client:
        key = client.post("/keys", json={"name": "script"}).json()["key"]
        headers = {"Authorization": f"Bearer {key}"}
        body = {"messages": [{"role": "user", "content": "hi"}]}
        m.guard.control.flag = True  # parked: the logon park, or a hold that ended with nothing queued
        ok = client.post("/v1/messages/count_tokens", json=body, headers=headers)
        assert ok.status_code == 200 and ok.json()["input_tokens"] == 3
        assert m.guard.control.starts == 1 and not m.guard.control.flag
        m.guard.pause()
        asyncio.run(m.guard.check())
        asyncio.run(m.guard.check())
        held = client.post("/v1/messages/count_tokens", json=body, headers=headers)
        assert held.status_code == 503 and held.headers["Retry-After"] == "180"
