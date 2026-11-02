"""Resource guard (#311): lazy model loading, "Load local model now", the RAM check. Fakes only; no GPU needed."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.cli_backends import ClaudeSession
from harness.config import BackendConfig, GpuGuardConfig
from harness_modules.local_model import service
from harness_modules.local_model.service import CLEAR, GIB, PAUSED, PAUSING, MemoryWatch
from harness_modules.local_model.service import memory_reading as real_memory_reading  # before conftest's plenty_of_ram patches it
from harness.llm import Completion
from harness.manager import Manager
from harness_modules.local_model.warmup import LOW_MEMORY, PAUSED as MODEL_PAUSED, READY, UNLOADED, WAKING, ModelWarmer

from test_daemon import Script, events, make_cfg, wait_status
from test_phase5 import GAME, FakeControl, FakeDetect, make_guard
from test_phase8 import FAKE_CLAUDE
from waits import scaled

REAL_WARMER_STATE = ModelWarmer.state  # at import, before the autouse fixtures replace it


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
    def __init__(self, available_gb: float = 24):
        self.available = available_gb * GIB

    def __call__(self):
        return {"available": int(self.available), "total": 32 * GIB, "commit": 20 * GIB, "commit_limit": 40 * GIB}


def guarded_manager(tmp_path, steps=None, ram: FakeRam | None = None, backends: dict | None = None, **guard_cfg):
    cfg = make_cfg(tmp_path)
    cfg.backends.update(backends or {})
    cfg.gpu_guard = GpuGuardConfig(enabled=True, resume_after_seconds=0, poll_seconds=3600, **guard_cfg)
    m = Manager(cfg, chat=Script(steps or [Completion(content="done")]))
    m.guard.detector, m.guard.control = FakeDetect(), FakeControl()
    m.guard.memory = MemoryWatch(cfg.gpu_guard.min_available_ram_gb, model_gb=cfg.gpu_guard.model_ram_gb, read=ram or FakeRam(), ttl=0)
    m.runner.ram = m.guard.memory
    return m


async def hold_and_release(m) -> None:
    m.guard.detector.signals = [GAME]
    await m.guard.check()
    await m.guard.check()
    assert m.guard.state == PAUSED
    m.guard.detector.signals = []
    await m.guard.check()


async def _until(predicate, timeout: float = 5) -> None:
    """Poll until `predicate()` holds, for up to `timeout` seconds (scaled for slow runners)."""
    deadline = time.monotonic() + scaled(timeout)
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)


# lazy resume
@pytest.mark.parametrize("ram_gb, want_model, trigger, state, starts, flag", [
    pytest.param(None, False, "hold", CLEAR, 0, True, id="hold-ends-with-nothing-queued-stays-parked"),
    pytest.param(None, True, "hold", CLEAR, 1, False, id="hold-ends-with-work-waiting-reloads"),
    pytest.param(None, False, "manual", CLEAR, 0, True, id="manual-resume-does-not-load"),
    pytest.param(30, False, "manual", CLEAR, 0, True, id="enough-ram-but-no-waiting-request-stays-parked"),
    pytest.param(9, True, "manual", PAUSED, 0, True, id="a-waiting-request-without-enough-ram-keeps-the-pause"),
])
def test_what_a_guard_does_when_the_pause_ends(ram_gb, want_model, trigger, state, starts, flag):
    """A pause ends because the game quit (`hold`) or the owner resumed it (`manual`): the model loads again only when
    a request is waiting for it and (if RAM is watched) enough is free; otherwise it stays parked, flag kept."""
    async def body():
        watched = {"min_available_ram_gb": 4, "model_ram_gb": 14} if ram_gb is not None else {}
        guard, detect, control, scheduler, log = make_guard(
            lazy_load=True, resume_after_seconds=0 if trigger == "hold" else 999, **watched)
        if ram_gb is not None:
            guard.memory = MemoryWatch(4, model_gb=14, read=FakeRam(ram_gb), ttl=0)
        guard.want_model = lambda: want_model
        if trigger == "hold":
            detect.signals = [GAME]
        else:
            guard.pause()
        await guard.check()
        assert guard.state == PAUSED and control.flag
        if trigger == "hold":
            detect.signals = []
        else:
            guard.resume()
        await guard.check()
        await guard.check()
        assert (guard.state, control.starts, control.flag) == (state, starts, flag)
        assert scheduler.paused == (state == PAUSED)
        if trigger == "hold":
            assert [k for k, _ in log] == ["pause", "resume"]
        if not starts:
            assert guard.status()["parked"] or state == PAUSED
    asyncio.run(body())


def _guard_with_ram(gb, want):
    guard, _, control, scheduler, _ = make_guard(lazy_load=True, resume_after_seconds=999, min_available_ram_gb=4,
                                                 model_ram_gb=14)
    ram = FakeRam(gb)
    guard.memory = MemoryWatch(4, model_gb=14, read=ram, ttl=0)
    guard.want_model = lambda: want
    return guard, control, scheduler, ram


def test_resume_with_load_low_keeps_the_flag_and_reports_why():
    """And once RAM frees up, the next check loads the model and clears the flag (the enough-RAM end state)."""
    async def body():
        guard, control, scheduler, ram = _guard_with_ram(9, True)
        guard.pause()
        await guard.check()
        assert guard.state == PAUSED
        guard.resume()
        await guard.check()
        assert guard.state == PAUSED and control.starts == 0 and control.flag and scheduler.paused
        assert "9.0 GB RAM available" in guard.status()["memory_hold"]
        ram.available = 30 * GIB  # RAM frees up: the next check admits the load
        await guard.check()
        assert control.starts == 1 and not control.flag and guard.status()["memory_hold"] == ""
    asyncio.run(body())


def _reason_text(guard) -> str:
    """What the app shows for a pause (Stables' gpuText and the session's gpu_paused event read the reasons' details)."""
    return guard.describe(guard.status()["reasons"])


def test_memory_hold_after_a_manual_resume_names_memory_not_the_app():
    """#873: the guard read `paused from the app` with manual false, which sent us looking for a hold that wasn't there."""
    async def body():
        guard, _, _, ram = _guard_with_ram(9, True)
        guard.pause()
        await guard.check()
        assert _reason_text(guard) == "paused from the app"
        guard.resume()
        await guard.check()
        status = guard.status()
        assert status["state"] == PAUSED and status["manual"] is False
        assert [r["key"] for r in status["reasons"]] == ["memory_hold"]
        text = _reason_text(guard)
        assert "paused from the app" not in text
        assert text.startswith("memory: 9.0 GB RAM available")
        assert "the model load needs 14 GB" in text
        ram.available = 10 * GIB  # still held: the reason follows the reading
        await guard.check()
        assert _reason_text(guard).startswith("memory: 10.0 GB RAM available")
        ram.available = 30 * GIB  # the load is admitted, and the reason goes with the hold
        await guard.check()
        assert all(r["key"] != "memory_hold" for r in guard.status()["reasons"])
    asyncio.run(body())


def test_memory_hold_after_a_trigger_clears_does_not_name_the_trigger():
    async def body():
        guard, _, _, _ = _guard_with_ram(9, True)
        guard.detector.signals = [GAME]
        await guard.check()
        await guard.check()
        assert guard.state == PAUSED and _reason_text(guard) == GAME["detail"]
        guard.detector.signals = []
        guard.resume(override_signals=False)
        await guard.check()
        assert guard.state == PAUSED
        assert [r["key"] for r in guard.status()["reasons"]] == ["memory_hold"]
        assert GAME["detail"] not in _reason_text(guard)
    asyncio.run(body())


def test_manual_pause_or_a_trigger_during_a_memory_hold_replaces_the_memory_reason():
    """A real hold wins: the app must not call a hand pause or a game a memory wait."""
    async def body():
        guard, _, _, _ = _guard_with_ram(9, True)
        guard.pause()
        await guard.check()
        guard.resume()
        await guard.check()
        assert [r["key"] for r in guard.status()["reasons"]] == ["memory_hold"]
        guard.pause()
        await guard.check()
        assert guard.status()["manual"] is True and _reason_text(guard) == "paused from the app"
        assert guard.status()["memory_hold"] == ""
        guard.resume()
        await guard.check()
        assert [r["key"] for r in guard.status()["reasons"]] == ["memory_hold"]
        guard.detector.signals = [GAME]
        await guard.check()
        assert _reason_text(guard) == GAME["detail"] and guard.status()["memory_hold"] == ""
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
        ram.available = 24 * GIB
        assert await m.warmer.warm(model) == UNLOADED
        await m.warmer.ensure_loaded(model)
        assert m.guard.control.starts == 1 and not m.guard.control.flag
    asyncio.run(body())


def test_load_now_pins_and_keeps_alive(tmp_path, monkeypatch):
    import harness_modules.local_model.warmup as warmup
    pings = []

    async def ping(self, model, verb):
        pings.append(verb)
    monkeypatch.setattr(ModelWarmer, "_ping", ping)
    now = [time.time()]
    monkeypatch.setattr(warmup, "time", SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic))  # the pin's clock

    async def body():
        m = guarded_manager(tmp_path)
        model = m.cfg.models[m.cfg.default_model]
        m.guard.control.flag = True
        m.warmer.keepalive_seconds = 0.05
        assert await m.warmer.load_now(model, 300) == UNLOADED
        assert m.warmer.pinned()
        await _until(lambda: m.guard.control.starts == 1 and "kept" in pings)
        assert m.warmer.pinned()
        now[0] += 301  # the pin runs out
        assert not m.warmer.pinned()
        await _until(lambda: m.warmer.pinned_until is None)
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


def test_load_deferred_when_it_would_drop_ram_below_the_threshold():
    """Issue #402: 11.7 GB available passes a plain 4 GB check, but the 14 GB load leaves none."""
    ram = FakeRam(11.7)
    watch = MemoryWatch(4, model_gb=14, read=ram, ttl=0)
    assert not watch.low() and watch.load_low()
    ram.available = 18.5 * GIB
    assert not watch.load_low()
    ram.available = 18 * GIB  # exactly the threshold left: proceeds
    assert not watch.load_low()
    assert not MemoryWatch(0, model_gb=14, read=FakeRam(1), ttl=0).load_low()
    assert not MemoryWatch(4, model_gb=14, read=lambda: None, ttl=0).load_low()


def test_load_samples_min_available_for_the_metric(tmp_path):
    async def body():
        m = guarded_manager(tmp_path, ram=FakeRam(24))
        samples = iter([20 * GIB, 3 * GIB, 9 * GIB, 12 * GIB, 12 * GIB, 12 * GIB])
        m.warmer.read_available = lambda: next(samples, 12 * GIB)
        m.guard.control.flag = True
        await m.warmer.ensure_loaded(m.cfg.models[m.cfg.default_model])
        assert m.warmer.load_min_available == 3 * GIB
    asyncio.run(body())


def test_session_waits_for_memory_before_loading_then_continues(tmp_path, monkeypatch):
    from harness_modules.local_model import service as gpu_guard
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
        ram.available = 24 * GIB
        await wait_status(m, s["id"], "done", timeout=15)
        assert events(m, s["id"], "memory_recovered")
        assert m.guard.control.starts == 1
        await m.stop()
    asyncio.run(body())


def test_worker_container_start_waits_for_memory(tmp_path, monkeypatch):
    from harness_modules.local_model import service as gpu_guard
    monkeypatch.setattr(gpu_guard, "MEMORY_POLL_SECONDS", 0.02)

    async def body():
        ram = FakeRam(2)
        m = guarded_manager(tmp_path, ram=ram)
        s = m.create("hello")
        gate = asyncio.create_task(m.runner._memory_gate(s["id"], "claude worker container"))
        await asyncio.sleep(0.1)
        assert not gate.done()
        assert events(m, s["id"], "waiting_memory")[0]["waiting_for"] == "claude worker container"
        ram.available = 24 * GIB
        await asyncio.wait_for(gate, 2)
        assert events(m, s["id"], "memory_recovered")
    asyncio.run(body())


def test_a_hosted_cli_run_waits_for_memory_before_starting_its_worker(tmp_path, monkeypatch):
    from harness_modules.local_model import service as gpu_guard
    monkeypatch.setattr(gpu_guard, "MEMORY_POLL_SECONDS", 0.02)
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE, encoding="utf-8")

    async def body():
        ram = FakeRam(2)
        m = guarded_manager(tmp_path, ram=ram,
                            backends={"claude": BackendConfig(enabled=True, model="claude-opus-5", max_sessions=1)})
        made = []

        def factory(**kwargs):
            made.append(kwargs)
            return ClaudeSession(**kwargs, command=[sys.executable, "-u", str(fake), "allow",
                                                    str(tmp_path / "state.jsonl"), "tool-1", "python build.py"])
        m.runner.cli_factory = factory
        await m.start(maintenance=False)
        s = m.create("hello", backend="claude")
        await _until(lambda: events(m, s["id"], "waiting_memory"))
        assert events(m, s["id"], "waiting_memory")[0]["waiting_for"] == "claude worker container"
        await asyncio.sleep(0.1)
        assert made == [] and not events(m, s["id"], "memory_recovered")
        ram.available = 24 * GIB
        await wait_status(m, s["id"], "done", "failed", timeout=15)
        assert events(m, s["id"], "memory_recovered")
        assert len(made) == 1
        await m.stop()
    asyncio.run(body())


def test_image_batch_waits_for_memory_and_stays_parked(tmp_path, monkeypatch):
    from harness_modules.local_model import service as gpu_guard
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
        ram.available = 24 * GIB
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


def test_metrics_export_vram_free_and_nvenc_sessions(tmp_path, monkeypatch):
    """#833: free VRAM and the NVENC session count are exported beside the existing VRAM series."""
    from harness.metrics import render
    from harness_modules.local_model import resources
    monkeypatch.setattr(resources, "_run", lambda args, timeout=5: "1024, 8192, 37, 7168, 2\n")
    monkeypatch.setattr(resources, "_gpu_cache", (-1e9, None))
    text = render(guarded_manager(tmp_path))
    assert f"harness_resource_vram_free_bytes {float(7168 * resources.MIB):g}" in text
    assert "harness_resource_nvenc_sessions 2" in text
    assert f"harness_resource_vram_used_bytes {float(1024 * resources.MIB):g}" in text


def test_metrics_leave_out_only_the_series_nvidia_smi_does_not_report(tmp_path, monkeypatch):
    """#833: [N/A] drops that series alone; with no nvidia-smi reading at all, no GPU series is exported."""
    from harness.metrics import render
    from harness_modules.local_model import resources
    monkeypatch.setattr(resources, "_gpu_cache", (-1e9, None))
    monkeypatch.setattr(resources, "_run", lambda args, timeout=5: "1024, 8192, 37, [N/A], [N/A]\n")
    text = render(guarded_manager(tmp_path))
    assert "harness_resource_vram_used_bytes" in text and "harness_resource_vram_total_bytes" in text
    assert "harness_resource_gpu_load_percent" in text
    assert "harness_resource_vram_free_bytes" not in text and "harness_resource_nvenc_sessions" not in text

    monkeypatch.setattr(resources, "_gpu_cache", (-1e9, None))
    monkeypatch.setattr(resources, "_run", lambda args, timeout=5: None)  # nvidia-smi missing or failing
    text = render(guarded_manager(tmp_path))
    for series in ("vram_used_bytes", "vram_total_bytes", "gpu_load_percent", "vram_free_bytes", "nvenc_sessions"):
        assert f"harness_resource_{series}" not in text


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
        m.guard.resume()
        asyncio.run(m.guard.check())
        assert m.guard.state == CLEAR and m.guard.control.flag  # lazy: still parked
        m.warmer.blocked = lambda: True  # an image batch has the GPU
        images = client.post("/v1/messages/count_tokens", json=body, headers=headers)
        assert images.status_code == 503 and images.headers["Retry-After"] == "60"
        assert "image generation" in images.text


def test_a_hold_stops_a_load_waiting_on_health_without_polling(tmp_path, monkeypatch):
    import harness_modules.local_model.warmup as warmup
    monkeypatch.setattr(warmup, "HEALTH_POLL_SECONDS", 3600)  # only the guard's notify can end the wait

    async def body():
        m = guarded_manager(tmp_path)
        model = m.cfg.models[m.cfg.default_model]
        control = m.guard.control
        control.flag, control.health = True, False  # loading: /health not OK yet
        load = asyncio.create_task(m.warmer.ensure_loaded(model))
        await _until(lambda: control.starts == 1)
        assert not load.done()
        m.guard.detector.signals = [GAME]
        await m.guard.check()  # PAUSING: the warmer hears about it
        await asyncio.wait_for(load, 2)
    asyncio.run(body())


def test_load_gives_up_when_health_never_answers(tmp_path, monkeypatch):
    import harness_modules.local_model.warmup as warmup
    monkeypatch.setattr(warmup, "HEALTH_POLL_SECONDS", 0.01)
    monkeypatch.setattr(warmup, "HEALTH_TIMEOUT_SECONDS", 0.05)

    async def body():
        m = guarded_manager(tmp_path)
        model = m.cfg.models[m.cfg.default_model]
        m.guard.control.flag, m.guard.control.health = True, False
        await asyncio.wait_for(m.warmer.ensure_loaded(model), 2)
        assert m.guard.control.starts == 1
    asyncio.run(body())


# diagnostics probes (harness/resources.py, gpu_guard.memory_reading, doctor): best-effort readings
def test_gpu_reading_parses_nvidia_smi_and_caches(monkeypatch):
    from harness_modules.local_model import resources
    outputs = ["1024, 8192, 37, 7168, 2\n", "not, a, number, x, y\n", None]
    monkeypatch.setattr(resources, "_run", lambda args, timeout=5: outputs.pop(0))
    monkeypatch.setattr(resources, "_gpu_cache", (-1e9, None))
    first = resources.gpu_reading()
    assert first == {"vram_used": 1024 * resources.MIB, "vram_total": 8192 * resources.MIB, "gpu_load": 37.0,
                     "vram_free": 7168 * resources.MIB, "nvenc_sessions": 2}
    assert resources.gpu_reading(cached=True) == first and len(outputs) == 2  # no second nvidia-smi
    assert resources.gpu_reading() is None  # unparseable
    assert resources.gpu_reading() is None  # nvidia-smi missing


@pytest.mark.parametrize(("line", "free", "sessions"), [
    ("1024, 8192, 37, [N/A], 2\n", None, 2),
    ("1024, 8192, 37, 7168, [N/A]\n", 7168, None),
    ("1024, 8192, 37, NaN, 2\n", None, 2),  # float() accepts NaN and inf; they must not escape as an exception
    ("1024, 8192, 37, 7168, inf\n", 7168, None),
])
def test_gpu_reading_drops_only_the_field_that_is_not_reported(monkeypatch, line, free, sessions):
    """#833: a [N/A] free-VRAM or session count leaves that one field out; VRAM used, total and load still read."""
    from harness_modules.local_model import resources
    asked = []
    monkeypatch.setattr(resources, "_run", lambda args, timeout=5: asked.append(args) or line)
    monkeypatch.setattr(resources, "_gpu_cache", (-1e9, None))
    reading = resources.gpu_reading()
    assert reading == {"vram_used": 1024 * resources.MIB, "vram_total": 8192 * resources.MIB, "gpu_load": 37.0,
                       "vram_free": None if free is None else free * resources.MIB, "nvenc_sessions": sessions}
    assert "memory.free,encoder.stats.sessionCount" in asked[0][1]  # the query asks for both new fields


@pytest.mark.parametrize("line", ["NaN, 8192, 37, 7168, 2\n", "1024, inf, 37, 7168, 2\n", "1e308, 8192, 37, 7168, 2\n"])
def test_gpu_reading_with_a_non_finite_core_field_is_no_reading(monkeypatch, line):
    """#833 review: a NaN, inf or absurdly large figure in VRAM used, total or load is None, never an exception."""
    from harness_modules.local_model import resources
    monkeypatch.setattr(resources, "_run", lambda args, timeout=5: line)
    monkeypatch.setattr(resources, "_gpu_cache", (-1e9, None))
    assert resources.gpu_reading() is None


def test_metrics_render_survives_a_non_finite_nvidia_smi_reading(tmp_path, monkeypatch):
    """#833 review: /metrics must still render, with no GPU series, when nvidia-smi reports NaN."""
    from harness.metrics import render
    from harness_modules.local_model import resources
    monkeypatch.setattr(resources, "_run", lambda args, timeout=5: "NaN, 8192, 37, 7168, 2\n")
    monkeypatch.setattr(resources, "_gpu_cache", (-1e9, None))
    text = render(guarded_manager(tmp_path))
    assert "harness_resource_ram_available_bytes" in text
    assert "harness_resource_vram_" not in text and "harness_resource_nvenc_sessions" not in text


COMPUTE_APPS = ("9953, /usr/bin/sunshine, 215\n"
                "1661709, /app/llama-server, 14000\n"
                "4403, /usr/lib/plexmediaserver/Plex Media Server, 12\n"
                "71, /usr/lib/plexmediaserver/Plex Transcoder, 412\n"
                "72, C:\\Program Files\\Plex\\Plex Transcoder.exe, [N/A]\n")


def fake_compute_apps(monkeypatch, *outputs):
    """nvidia-smi answers only the query the probe is meant to send; each output is one poll, None a failed tool."""
    from harness_modules.local_model import resources
    outputs = list(outputs)

    def run(args, timeout=5):
        assert args == ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                        "--format=csv,noheader,nounits"]
        return outputs.pop(0)
    monkeypatch.setattr(resources, "_run", run)


def test_gpu_processes_parses_compute_apps_and_tells_none_running_from_a_failed_tool(monkeypatch):
    from harness_modules.local_model import resources
    fake_compute_apps(monkeypatch, COMPUTE_APPS, "", "No running processes found\ngarbage, x, 1\n", None)
    first = resources.gpu_processes()
    assert first[1] == {"pid": 1661709, "name": "/app/llama-server", "used_bytes": 14000 * resources.MIB}
    assert first[4] == {"pid": 72, "name": "C:\\Program Files\\Plex\\Plex Transcoder.exe", "used_bytes": None}
    assert resources.gpu_processes() == []  # nothing on the GPU
    assert resources.gpu_processes() == []  # lines that aren't rows
    assert resources.gpu_processes() is None  # nvidia-smi missing or failed


def test_plex_without_a_token_counts_transcoders_from_nvidia_smi(monkeypatch):
    """#830: no Plex credential on Linux. One signal per Plex Transcoder; Plex Media Server and others don't count."""
    from harness_modules.local_model import service
    monkeypatch.setattr(service, "plex_token", lambda: "")
    fake_compute_apps(monkeypatch, COMPUTE_APPS)
    detector = service.Detector(GpuGuardConfig())
    signals = asyncio.run(detector._plex())
    assert [(s["key"], s["kind"]) for s in signals] == [("plex:pid71", "plex"), ("plex:pid72", "plex")]
    assert "412 MiB" in signals[0]["detail"] and detector.plex_error == ""


def test_plex_signal_key_ignores_vram_so_resume_anyway_holds(monkeypatch):
    """The override compares keys across polls: a transcoder whose VRAM grows is still the same trigger."""
    from harness_modules.local_model import service
    monkeypatch.setattr(service, "plex_token", lambda: "")
    fake_compute_apps(monkeypatch, "71, /x/Plex Transcoder, 400\n", "71, /x/Plex Transcoder, 700\n")
    detector = service.Detector(GpuGuardConfig())
    first, second = asyncio.run(detector._plex()), asyncio.run(detector._plex())
    assert [s["key"] for s in first] == [s["key"] for s in second]


def test_plex_with_no_transcoder_is_clear_and_a_failed_nvidia_smi_says_so(monkeypatch):
    from harness_modules.local_model import service
    monkeypatch.setattr(service, "plex_token", lambda: "")
    fake_compute_apps(monkeypatch, None, "1661709, /app/llama-server, 14000\n")
    detector = service.Detector(GpuGuardConfig())
    assert asyncio.run(detector._plex()) == []
    assert "nvidia-smi" in detector.plex_error  # blind, and status/doctor say so
    assert asyncio.run(detector._plex()) == []
    assert detector.plex_error == ""  # a working source with nothing on it is not an error


def test_a_transcoder_on_the_gpu_parks_the_model_through_the_real_detector(monkeypatch):
    """#830's point: with no token, the guard itself holds and clears on what nvidia-smi shows."""
    from harness_modules.local_model import service
    from test_phase5 import make_guard
    monkeypatch.setattr(service, "plex_token", lambda: "")
    fake_compute_apps(monkeypatch, COMPUTE_APPS, "1661709, /app/llama-server, 14000\n")

    async def body():
        guard, _, control, scheduler, _ = make_guard(resume_after_seconds=0)
        guard.detector = service.Detector(GpuGuardConfig())
        await guard.check()
        assert guard.state == PAUSED and scheduler.paused and control.stops == 1
        assert [r["key"] for r in guard.reasons] == ["plex:pid71", "plex:pid72"]
        assert guard.status()["plex_error"] == ""
        await guard.check()
        assert not any(s["kind"] == "plex" for s in guard.signals)
    asyncio.run(body())


def test_plex_with_a_token_asks_plex_and_never_counts_the_same_transcode_twice(monkeypatch):
    """The registry path on Windows stays as it was; the two sources aren't added together."""
    import httpx
    from harness_modules.local_model import resources, service
    monkeypatch.setattr(service, "plex_token", lambda: "token")
    monkeypatch.setattr(resources, "_run", lambda args, timeout=5: pytest.fail("nvidia-smi read beside the Plex API"))
    body = {"MediaContainer": {"TranscodeSession": [
        {"key": "/transcode/sessions/a", "videoDecision": "transcode", "transcodeHwEncoding": "nvenc"}]}}

    class Client:
        def __init__(self, timeout=None): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def get(self, url, headers=None):
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))
    monkeypatch.setattr(service.httpx, "AsyncClient", Client)
    assert [s["key"] for s in asyncio.run(service.Detector(GpuGuardConfig())._plex())] == ["plex:a"]


def test_diagnostics_lists_vram_per_process(tmp_path, monkeypatch):
    from harness_modules.local_model import resources
    processes = [{"pid": 71, "name": "/x/Plex Transcoder", "used_bytes": 412 * resources.MIB}]
    monkeypatch.setattr(resources, "gpu_reading", lambda cached=False: None)
    monkeypatch.setattr(resources, "gpu_processes", lambda: processes)
    monkeypatch.setattr(resources, "cpu_load", lambda: None)
    monkeypatch.setattr(resources, "container_memory", lambda prefix="harness-": None)
    monkeypatch.setattr(resources, "_process_memory", lambda match: None)

    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="done")]))
        m.guard = None
        assert (await resources.diagnostics(m))["vram"]["processes"] == processes
    asyncio.run(body())


def test_doctor_warns_when_the_guard_cannot_see_plex(tmp_path, monkeypatch, capsys):
    import httpx
    from harness import doctor
    from harness_modules.local_model.doctor import check_guard
    cfg = make_cfg(tmp_path)
    cfg.gpu_guard = GpuGuardConfig(enabled=True, pause_flag=str(tmp_path / "llama.paused"))
    gpu = {"enabled": True, "state": "clear", "lazy_load": True, "plex_error": "nvidia-smi could not list GPU processes"}
    monkeypatch.setattr(doctor.httpx, "get",
                        lambda url, timeout=None, headers=None: httpx.Response(200, json=gpu, request=httpx.Request("GET", url)))
    r = doctor.Report()
    check_guard(r, cfg)
    assert r.warned == 1 and "nvidia-smi could not list GPU processes" in capsys.readouterr().out
    gpu["plex_error"] = ""
    check_guard(r, cfg)
    assert r.warned == 1


def test_doctor_reads_plex_error_from_a_real_daemon(tmp_path, monkeypatch, capsys):
    """Real socket, real local-owner check, real Detector: a daemon refuses /gpu to a caller without the token."""
    import socket
    import threading
    import httpx
    import uvicorn
    from harness import doctor, local_owner
    from harness.api import create_app
    from harness_modules.local_model import resources, service
    from harness_modules.local_model.doctor import check_guard

    monkeypatch.setattr(service, "plex_token", lambda: "")
    monkeypatch.setattr(resources, "_run", lambda *_args, **_kwargs: None)  # nvidia-smi can't be read
    m = guarded_manager(tmp_path, pause_flag=str(tmp_path / "llama.paused"))
    m.guard.detector = service.Detector(m.cfg.gpu_guard)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        m.cfg.port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(m), host="127.0.0.1", port=m.cfg.port, log_level="warning",
                                           proxy_headers=False, forwarded_allow_ips=""))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + scaled(10)
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        url = f"http://127.0.0.1:{m.cfg.port}/gpu"
        assert httpx.get(url, timeout=5).status_code == 401
        token_headers = {local_owner.HEADER: local_owner.read_token(m.cfg.data_dir)}
        while (not httpx.get(url, timeout=5, headers=token_headers).json()["plex_error"]
               and time.monotonic() < deadline):
            time.sleep(0.05)
        r = doctor.Report()
        check_guard(r, m.cfg)
    finally:
        server.should_exit = True
        thread.join(scaled(10))
    out = capsys.readouterr().out
    assert "nvidia-smi could not list GPU processes" in out and "not answering" not in out
    assert r.warned == 1


def test_run_returns_none_when_the_tool_is_missing():
    from harness_modules.local_model import resources
    assert resources._run(["definitely-not-a-real-tool-311"]) is None


def test_container_memory_sums_harness_containers(monkeypatch):
    from harness_modules.local_model import resources
    out = "harness-worker-1\t1.5GiB / 31GiB\nharness-sandbox\t512MiB / 31GiB\nother\t9GiB / 31GiB\n"
    monkeypatch.setattr(resources, "_run", lambda args, timeout=5: out)
    assert resources.container_memory() == int(1.5 * GIB) + 512 * resources.MIB
    monkeypatch.setattr(resources, "_run", lambda args, timeout=5: None)
    assert resources.container_memory() is None


def test_parse_size_units():
    from harness_modules.local_model.resources import MIB, parse_size
    assert parse_size("12kB") == 12000 and parse_size("512MiB") == 512 * MIB and parse_size("2GB") == 2 * 1000 ** 3
    assert parse_size("10B") == 10
    assert parse_size("xGiB") == 0 and parse_size("12 parsecs") == 0


def test_process_memory_sums_matching_processes(monkeypatch):
    import psutil
    from harness_modules.local_model import resources

    class Proc:
        def __init__(self, name, rss=None, broken=False):
            self.broken = broken
            self._info = {"name": name, "memory_info": type("M", (), {"rss": rss})() if rss else None}

        @property
        def info(self):
            if self.broken:
                raise psutil.AccessDenied()
            return self._info
    procs = [Proc("llama-server.exe", 3 * GIB), Proc("llama-server", 1 * GIB), Proc("llama-server.exe"),
             Proc("python.exe", 5 * GIB), Proc("x", broken=True)]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter(procs))
    is_llama = lambda n: n.lower().startswith("llama-server")  # noqa: E731
    assert resources._process_memory(is_llama) == 4 * GIB
    assert resources._process_memory(lambda n: n == "none") == 0


def test_cpu_and_daemon_probes_survive_psutil_errors(monkeypatch):
    import psutil
    from harness_modules.local_model import resources

    def boom(*a, **k):
        raise RuntimeError("no counters")
    assert isinstance(resources.daemon_memory(), int)
    monkeypatch.setattr(psutil, "cpu_percent", boom)
    monkeypatch.setattr(psutil, "Process", boom)
    assert resources.cpu_load() is None and resources.cpu_percent_since_last() is None
    assert resources.daemon_memory() is None


def test_memory_reading_without_psutil_or_off_windows(monkeypatch):
    import psutil
    from harness_modules.local_model import service as gpu_guard
    monkeypatch.setattr(gpu_guard.sys, "platform", "linux")
    reading = real_memory_reading()
    assert reading["available"] > 0 and reading["total"] >= reading["available"] and reading["commit"] is None

    def boom():
        raise RuntimeError("no counters")
    monkeypatch.setattr(psutil, "virtual_memory", boom)
    assert real_memory_reading() is None
    assert MemoryWatch(4, read=lambda: None, ttl=0).status()["available_bytes"] is None
    from harness_modules.local_model.service import describe_memory
    assert describe_memory({"available_bytes": None}) == "low memory"


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="GlobalMemoryStatusEx is Windows-only")
def test_memory_reading_reports_commit_on_windows(monkeypatch):
    import ctypes

    def memory_status(pointer):
        status = pointer._obj
        status.ullTotalPageFile = 160 * GIB
        status.ullAvailPageFile = 128 * GIB
        return 1

    from types import SimpleNamespace
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_: SimpleNamespace(GlobalMemoryStatusEx=memory_status))
    reading = real_memory_reading()
    assert reading["commit_limit"] == 160 * GIB and reading["commit"] == 32 * GIB


def test_diagnostics_without_a_guard_and_with_images_holding_the_gpu(tmp_path, monkeypatch):
    from harness_modules.local_model import resources
    monkeypatch.setattr(resources, "gpu_reading", lambda cached=False: None)
    monkeypatch.setattr(resources, "cpu_load", lambda: None)
    monkeypatch.setattr(resources, "container_memory", lambda prefix="harness-": None)
    monkeypatch.setattr(resources, "_process_memory", lambda match: None)

    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="done")]))
        m.guard = None
        m.modules.get("images").service = type("Images", (), {"gpu_taken": True})()
        async def ready(model):
            return "ready"
        m.warmer.state = ready
        diag = await resources.diagnostics(m)
        assert diag["guard"] == {"enabled": False, "state": "clear"}
        assert diag["vram"]["holders"] == ["llama-server", "ComfyUI"] and diag["vram"]["used_bytes"] is None
        assert diag["ram"]["threshold_bytes"] == 0
        m.cfg.modules.local_model = False
        assert await resources.model_status(m) == {"state": "disabled"}
    asyncio.run(body())


def test_doctor_reports_the_resource_guard(tmp_path, monkeypatch, capsys):
    import httpx
    from harness import doctor
    cfg = make_cfg(tmp_path)
    cfg.gpu_guard = GpuGuardConfig(enabled=True, pause_flag=str(tmp_path / "llama.paused"))
    gpu = {"enabled": True, "state": "clear", "lazy_load": True, "memory": {"low": True}}

    def get(url, timeout=None, headers=None):
        payload = {"/health": {"profile": cfg.profile}, "/models/status": [{"state": "ready"}], "/gpu": gpu,
                   "/maintenance": {"backup": {}}}["/" + url.split("/", 3)[3]]
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))
    monkeypatch.setattr(doctor.httpx, "get", get)
    (tmp_path / "llama.paused").write_text("x")
    r = doctor.Report()
    from harness_modules.local_model.doctor import check_guard
    check_guard(r, cfg)
    assert "model parked until needed; RAM low, new work waits" in capsys.readouterr().out
    gpu["lazy_load"] = False
    from harness_modules.local_model.doctor import check_guard
    check_guard(r, cfg)
    assert r.warned == 1 and "exists but the guard is clear" in capsys.readouterr().out


def test_load_now_refuses_during_a_hold_or_without_a_local_model(tmp_path):
    from fastapi.testclient import TestClient
    from harness.api import create_app

    m = guarded_manager(tmp_path)
    with TestClient(create_app(m)) as client:
        m.guard.pause()
        held = client.post("/resources/load", json={"duration_seconds": 600})
        assert held.status_code == 409 and "turn the hold off first" in held.text
        m.guard.resume()
        m.cfg.modules.local_model = False
        assert client.post("/resources/load", json={"duration_seconds": 600}).status_code == 400
        assert m.guard.control.starts == 0 and m.warmer.pinned_until is None


def test_memory_recovered_notification(tmp_path):
    async def body():
        m = guarded_manager(tmp_path)
        s = m.create("hello")
        m.db.insert_event(s["id"], "memory_recovered", {"seconds": 150})
        m.db.insert_event(s["id"], "memory_recovered", {"seconds": 20})
        notes = [m.notifier.build(e) for e in m.db.events(s["id"]) if e["type"] == "memory_recovered"]
        assert [n["message"] for n in notes] == ["Memory recovered after 2 min; the task continues.",
                                                 "Memory recovered after 20 s; the task continues."]
        assert notes[0]["title"].startswith("Resumed:") and notes[0]["sequence_id"] == f"mem-{s['id']}"
    asyncio.run(body())


def test_model_call_fails_rather_than_spins_when_the_parked_model_cannot_load(tmp_path):
    from harness.llm import LLMError
    calls = []

    async def body():
        m = guarded_manager(tmp_path)
        control = m.guard.control

        def answer(messages):
            calls.append(1)
            control.flag = True  # parked while an image batch has the GPU: nothing may load it
            m.warmer.blocked = lambda: True
            return LLMError("model server unreachable: ConnectError")
        m.runner.chat = Script([answer])
        await m.start(maintenance=False)
        s = m.create("hello")
        await wait_status(m, s["id"], "failed", timeout=10)
        assert len(calls) == 1 and control.starts == 0
        await m.stop()
    asyncio.run(body())


# a GPU taker during a load (#342 round 2): the pause flag stays in place whatever the load was doing
class SlowUnlink(FakeControl):
    """The flag's unlink (a thread in ServerControl.start) lands only after `go`: a taker has started meanwhile."""

    def __init__(self):
        super().__init__()
        self.flag, self.health = True, False  # parked; once started, llama-server takes a while to load
        self.go = asyncio.Event()

    async def start(self):
        self.starts += 1
        await self.go.wait()
        self.flag = False

    async def stop(self):
        await super().stop()
        self.go.set()


def guarded_image_manager(tmp_path, control):
    from harness.config import ImagesConfig
    from test_phase6 import fake_comfy
    import httpx
    cfg = make_cfg(tmp_path)
    cfg.gpu_guard = GpuGuardConfig(enabled=True, resume_after_seconds=0, poll_seconds=3600)
    cfg.images = ImagesConfig(enabled=True, work_dir=str(tmp_path / "img"), linger_seconds=0.2,
                              comfy_dir=str(tmp_path / "comfy"), models_dir=str(tmp_path / "models"),
                              upscale_dir=str(tmp_path / "upscale"))
    m = Manager(cfg, chat=Script([Completion(content="done")]))
    m.guard.detector = FakeDetect()
    m.guard.control = m.images.control = control  # two ServerControls on one pause flag in the daemon
    m.images.transport = httpx.MockTransport(fake_comfy()[0])
    flags_while_comfy = []

    async def comfy_start():
        flags_while_comfy.append(control.flag)

    async def comfy_stop():
        flags_while_comfy.append(control.flag)
    m.images.comfy.start, m.images.comfy.stop = comfy_start, comfy_stop
    return m, flags_while_comfy


def test_image_takeover_during_a_load_keeps_the_pause_flag(tmp_path, monkeypatch):
    import harness_modules.local_model.warmup as warmup
    monkeypatch.setattr(warmup, "HEALTH_POLL_SECONDS", 3600)  # only a notify ends the /health wait

    async def body():
        control = SlowUnlink()
        m, flags_while_comfy = guarded_image_manager(tmp_path, control)
        model = m.cfg.models[m.cfg.default_model]
        await m.start(maintenance=False)
        load = asyncio.create_task(m.warmer.ensure_loaded(model))
        for _ in range(100):
            if control.starts:
                break
            await asyncio.sleep(0.01)
        assert control.starts == 1 and not load.done()  # removing the flag
        job = m.images.submit("a lighthouse at dusk", model="fast")
        for _ in range(500):
            if m.images.phase == "switching" or control.go.is_set():
                break
            await asyncio.sleep(0.01)
        control.go.set()  # the unlink lands after the takeover began
        done = await asyncio.wait_for(m.images.wait(job["id"]), 5)
        assert done["status"] == "done"
        await asyncio.wait_for(load, 2)  # the load stopped
        assert control.flag and flags_while_comfy == [True, True]  # run-qwen.ps1 never saw the flag gone
        control.health = True  # the hold is over: the next model call loads normally
        await asyncio.wait_for(m.warmer.ensure_loaded(model), 2)
        assert control.starts == 2 and not control.flag
        await m.stop()
    asyncio.run(body())


def test_a_load_queued_before_an_image_takeover_leaves_the_flag(tmp_path):
    async def body():
        control = FakeControl()
        control.flag = True
        m, _ = guarded_image_manager(tmp_path, control)
        model = m.cfg.models[m.cfg.default_model]
        wake = m.warmer._start_wake(model)  # e.g. selecting the local model, just before Generate
        m.images.phase = "switching"         # the batch took the GPU before the load task ran
        await asyncio.wait_for(wake, 2)
        assert control.flag and control.starts == 0
    asyncio.run(body())


def test_guard_hold_during_a_load_keeps_the_pause_flag(tmp_path, monkeypatch):
    import harness_modules.local_model.warmup as warmup
    monkeypatch.setattr(warmup, "HEALTH_POLL_SECONDS", 3600)

    async def body():
        m = guarded_manager(tmp_path)
        control = m.guard.control = SlowUnlink()
        model = m.cfg.models[m.cfg.default_model]
        load = asyncio.create_task(m.warmer.ensure_loaded(model))
        await _until(lambda: control.starts == 1)
        m.guard.detector.signals = [GAME]
        check = asyncio.create_task(m.guard.check())  # PAUSING, then (nothing running) stop
        await _until(lambda: m.guard.state != CLEAR)
        control.go.set()
        await asyncio.wait_for(check, 2)
        await asyncio.wait_for(load, 2)
        assert m.guard.state == PAUSED and control.flag and control.stops == 1
    asyncio.run(body())


def test_unload_during_a_load_now_aborts_it_and_keeps_the_model_unloaded(tmp_path, monkeypatch):
    """Load local model now, then Unload now while the flag's unlink is still landing: the unload goes through
    park, which aborts the load, so the flag stays, the pin is gone and nothing restarts the server."""
    import harness_modules.local_model.warmup as warmup
    monkeypatch.setattr(warmup, "HEALTH_POLL_SECONDS", 3600)

    async def body():
        m = guarded_manager(tmp_path)
        await m.start(maintenance=False)
        control = m.guard.control = SlowUnlink()
        model = m.cfg.models[m.cfg.default_model]
        m.warmer.keepalive_seconds = 0.05
        assert await m.warmer.load_now(model, 3600) == UNLOADED
        await _until(lambda: control.starts == 1)
        unload = asyncio.create_task(m.guard.unload(before_stop=m.warmer.unpin))
        await asyncio.sleep(0.02)
        control.go.set()  # the unlink lands after the unload began
        assert await asyncio.wait_for(unload, 2)
        await asyncio.sleep(0.2)  # several keepalive periods
        assert control.flag and control.stops == 1 and control.starts == 1
        assert not m.warmer.pinned() and m.warmer.waking_for(model) is None
        await m.stop()
    asyncio.run(body())


def test_unload_during_the_health_wait_stops_the_load(tmp_path, monkeypatch):
    import harness_modules.local_model.warmup as warmup
    monkeypatch.setattr(warmup, "HEALTH_POLL_SECONDS", 3600)

    async def body():
        m = guarded_manager(tmp_path)
        await m.start(maintenance=False)
        control = m.guard.control
        control.flag, control.health = True, False
        model = m.cfg.models[m.cfg.default_model]
        await m.warmer.load_now(model, 3600)
        await _until(lambda: control.starts == 1)
        assert not control.flag  # waiting on /health
        assert await asyncio.wait_for(m.guard.unload(before_stop=m.warmer.unpin), 2)
        await asyncio.sleep(0.05)
        assert control.flag and control.starts == 1 and not m.warmer.pinned()
        assert m.warmer.waking_for(model) is None
        await m.stop()
    asyncio.run(body())


@pytest.mark.parametrize("end", ["cancel", "delete"])
def test_hold_ending_after_the_paused_session_ended_leaves_model_unloaded(tmp_path, end):
    """A session held by the pause that is cancelled or deleted before the hold ends no longer wants the model."""
    async def body():
        m = guarded_manager(tmp_path)
        await m.start(maintenance=False)
        m.guard.detector.signals = [GAME]
        await m.guard.check()
        await m.guard.check()
        assert m.guard.state == PAUSED
        s = m.create("hello")
        await wait_status(m, s["id"], "queued")
        deadline = time.monotonic() + scaled(10)  # the run registers as held a moment after "queued" (slow on CI)
        while s["id"] not in m.runner.gpu_paused_sessions and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert s["id"] in m.runner.gpu_paused_sessions, f"never held: {m.runner.gpu_paused_sessions}"
        assert m.guard.want_model()
        if end == "cancel":
            await m.cancel(s["id"])
            await wait_status(m, s["id"], "cancelled")
        else:
            await m.cancel(s["id"])
            await wait_status(m, s["id"], "cancelled")
            m.db.delete_session(s["id"])
        assert not m.guard.want_model()
        m.guard.detector.signals = []
        await m.guard.check()
        await m.guard.check()
        assert m.guard.state == CLEAR and m.guard.control.flag and m.guard.control.starts == 0
        await m.stop()
    asyncio.run(body())


# a llama-server that has started but isn't listening yet (#344)
def _fake_llama_processes(monkeypatch, procs):
    """`procs`: (pid, name, cmdline) tuples as psutil would report them; taskkill calls land in the returned list."""
    import psutil
    import harness_modules.local_model.service as gpu_guard
    alive = {pid: (name, cmd) for pid, name, cmd in procs}
    killed = []

    class Proc:
        def __init__(self, pid):
            self.info = {"pid": pid, "name": alive[pid][0], "cmdline": alive[pid][1]}
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter([Proc(p) for p in list(alive)]))

    async def fake_run(args, timeout=60, **kw):
        if args[0] == "netstat":
            return 0, "  Proto  Local Address  Foreign Address  State  PID\n", ""  # nothing listening yet
        killed.append(int(args[2]))
        alive.pop(int(args[2]), None)
        return 0, "", ""
    monkeypatch.setattr(gpu_guard, "run_cmd", fake_run)
    return alive, killed


def test_server_port_reads_the_command_line():
    from harness_modules.local_model.service import server_port
    assert server_port(["llama-server.exe", "-m", "x.gguf", "--port", "8090"]) == 8090
    assert server_port(["llama-server", "--port=8091"]) == 8091
    # -p is --prompt in llama-server, never the port, and a prompt never hides a later --port
    assert server_port(["llama-server", "-p", "8092"]) == 8080
    assert server_port(["llama-server.exe", "-p", "YouAreQwen", "-m", "q.gguf", "--port", "8090"]) == 8090
    assert server_port(["llama-server", "--port", "8090", "--port=8093"]) == 8093
    assert server_port(["llama-server", "-m", "x.gguf"]) == 8080
    assert server_port(["llama-server", "--port", "abc"]) == -1
    assert server_port(None) == 8080


def test_park_stops_a_llama_server_that_is_not_listening_yet(tmp_path, monkeypatch):
    from harness_modules.local_model.service import ServerControl
    cfg = make_cfg(tmp_path)
    cfg.gpu_guard = GpuGuardConfig(enabled=True, pause_flag=str(tmp_path / "llama-server.paused"))
    model = cfg.models[cfg.default_model]
    port = ServerControl(cfg.gpu_guard, model).port
    import harness_modules.local_model.service as gpu_guard
    monkeypatch.setattr(gpu_guard, "sys", SimpleNamespace(platform="win32"))  # taskkill and netstat; asyncio keeps its loop
    alive, killed = _fake_llama_processes(monkeypatch, [
        (4242, "llama-server.exe", ["C:\AI\llama-server.exe", "-m", "q.gguf", "--port", str(port)]),
        (4343, "llama-server.exe", ["C:\AI\llama-server.exe", "--port", str(port + 1)]),  # someone else's server
        (4444, "python.exe", ["python", "--port", str(port)]),
    ])

    async def body():
        control = ServerControl(cfg.gpu_guard, model)
        warmer = ModelWarmer()
        await warmer.park(control.stop)
        assert killed == [4242] and set(alive) == {4343, 4444}
        assert control.flagged()  # the supervisor won't start another
    asyncio.run(body())


def test_windows_listening_pids_parse_netstat(tmp_path, monkeypatch):
    import harness_modules.local_model.service as gpu_guard
    from harness_modules.local_model.service import ServerControl
    cfg = make_cfg(tmp_path)
    control = ServerControl(GpuGuardConfig(pause_flag=str(tmp_path / "paused")), cfg.models[cfg.default_model])
    port = control.port
    monkeypatch.setattr(gpu_guard, "sys", SimpleNamespace(platform="win32"))
    netstat = (f"  Proto  Local Address          Foreign Address        State           PID\n"
               f"  TCP    127.0.0.1:{port}         0.0.0.0:0              LISTENING       4242\n"
               f"  TCP    127.0.0.1:{port}1        0.0.0.0:0              LISTENING       4343\n"
               f"  TCP    127.0.0.1:{port}         127.0.0.1:50000        ESTABLISHED     4444\n"
               f"  TCP    0.0.0.0:{port}           0.0.0.0:0              LISTENING       4\n")
    calls = []

    async def fake_run(args, timeout=60, **kw):
        calls.append(args)
        return 0, netstat, ""
    monkeypatch.setattr(gpu_guard, "run_cmd", fake_run)
    assert asyncio.run(control._listening_pids()) == {4242}
    assert calls == [["netstat", "-ano", "-p", "TCP"]]


# Linux page-out (#835): no taskkill or netstat; psutil finds and stops a native llama-server
def _fake_linux_server(monkeypatch, port):
    """A native llama-server (pid 5151) listening on `port`, as psutil reports it. Returns the commands run through
    run_cmd and the pids psutil terminated."""
    import psutil
    import harness_modules.local_model.service as gpu_guard
    monkeypatch.setattr(gpu_guard, "sys", SimpleNamespace(platform="linux"))
    addr = SimpleNamespace
    conns = [
        SimpleNamespace(laddr=addr(ip="127.0.0.1", port=port), status=psutil.CONN_LISTEN, pid=5151),
        SimpleNamespace(laddr=addr(ip="127.0.0.1", port=port + 1), status=psutil.CONN_LISTEN, pid=5252),
        SimpleNamespace(laddr=addr(ip="127.0.0.1", port=port), status=psutil.CONN_ESTABLISHED, pid=5353),
        SimpleNamespace(laddr=addr(ip="127.0.0.1", port=port), status=psutil.CONN_LISTEN, pid=None),  # another user's
    ]
    monkeypatch.setattr(psutil, "net_connections", lambda kind="inet": list(conns) if kind == "tcp" else [])
    commands, terminated = [], []

    async def fake_run(args, timeout=60, **kw):
        commands.append(args)
        return 0, "", ""
    monkeypatch.setattr(gpu_guard, "run_cmd", fake_run)

    class Proc:
        def __init__(self, pid):
            self.pid = pid

        def terminate(self):
            terminated.append(self.pid)

        def wait(self, timeout=None):
            return 0

        def kill(self):
            raise AssertionError("terminate was enough")
    monkeypatch.setattr(psutil, "Process", Proc)
    return commands, terminated


def test_linux_listening_pids_come_from_psutil(tmp_path, monkeypatch):
    from harness_modules.local_model.service import ServerControl
    cfg = make_cfg(tmp_path)
    control = ServerControl(GpuGuardConfig(pause_flag=str(tmp_path / "paused")), cfg.models[cfg.default_model])
    commands, _ = _fake_linux_server(monkeypatch, control.port)
    assert asyncio.run(control._listening_pids()) == {5151}
    assert commands == []  # no netstat -ano on Linux


def test_linux_park_stops_the_server_without_taskkill(tmp_path, monkeypatch):
    from harness_modules.local_model.service import ServerControl
    cfg = make_cfg(tmp_path)
    control = ServerControl(GpuGuardConfig(pause_flag=str(tmp_path / "paused")), cfg.models[cfg.default_model])
    commands, terminated = _fake_linux_server(monkeypatch, control.port)

    async def body():
        await control.stop()
        assert control.flagged()
        assert terminated == [5151]
        assert not [c for c in commands if c[0] in ("taskkill", "netstat")], commands
        await control.start()
        assert not control.flagged()
    asyncio.run(body())


def test_linux_stop_kills_a_server_that_ignores_sigterm_and_leaves_one_it_cannot_signal(tmp_path, monkeypatch):
    import psutil
    from harness_modules.local_model.service import ServerControl
    cfg = make_cfg(tmp_path)
    control = ServerControl(GpuGuardConfig(pause_flag=str(tmp_path / "paused")), cfg.models[cfg.default_model])
    _fake_linux_server(monkeypatch, control.port)
    signals = []

    class Proc:
        def __init__(self, pid):
            if pid == 6060:
                raise psutil.AccessDenied(pid)  # a container's root process
            self.pid = pid

        def terminate(self):
            signals.append(("TERM", self.pid))

        def wait(self, timeout=None):
            raise psutil.TimeoutExpired(timeout, self.pid)

        def kill(self):
            signals.append(("KILL", self.pid))
    monkeypatch.setattr(psutil, "Process", Proc)
    assert ServerControl._terminate(5151) == "killed"
    assert signals == [("TERM", 5151), ("KILL", 5151)]
    assert ServerControl._terminate(6060) == "not stopped (AccessDenied)"


def test_linux_listening_pids_survive_a_denied_socket_listing(tmp_path, monkeypatch):
    import psutil
    from harness_modules.local_model.service import ServerControl
    cfg = make_cfg(tmp_path)
    control = ServerControl(GpuGuardConfig(pause_flag=str(tmp_path / "paused")), cfg.models[cfg.default_model])
    _fake_linux_server(monkeypatch, control.port)

    def denied(kind="inet"):
        raise psutil.AccessDenied()  # macOS without root
    monkeypatch.setattr(psutil, "net_connections", denied)
    assert asyncio.run(control._listening_pids()) == set()


RUN_SERVER = Path(__file__).resolve().parents[1] / "install" / "run-server.sh"
FAKE_DOCKER = """#!/usr/bin/env bash
# Records each call; `run` blocks like a running container until `stop` (or `rm -f`) is called. With $DOCKER_HEALTH_CMD
# set, `run` also writes the value of its --health-cmd there.
echo "$*" >>"$DOCKER_LOG"
case "$1" in
    rm) rm -f "$DOCKER_STOPPED" ;;
    stop) touch "$DOCKER_STOPPED" ;;
    run)
        prev=
        for arg in "$@"; do
            if [[ $prev == --health-cmd && -n ${DOCKER_HEALTH_CMD:-} ]]; then printf '%s' "$arg" >"$DOCKER_HEALTH_CMD"; fi
            prev=$arg
        done
        while [[ ! -e "$DOCKER_STOPPED" ]]; do sleep 0.05; done ;;
esac
exit 0
"""


def test_linux_supervisor_stops_the_container_when_the_flag_appears(tmp_path):
    """install/run-server.sh stops its running container once the pause flag is written, stays paused while the flag
    exists, and starts the container again once ServerControl.start() removes it (#835)."""
    from tests.installer_support import BASH
    assert BASH, "the Linux supervisor test needs bash (Git Bash on Windows)"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8", newline="\n")  # bash rejects CRLF
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    flag = tmp_path / "llama-server.paused"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "DOCKER_LOG": log.as_posix(),
           "DOCKER_STOPPED": (tmp_path / "stopped").as_posix(), "HARNESS_LOG_DIR": (tmp_path / "logs").as_posix(),
           "HARNESS_PAUSE_POLL_SECONDS": "0.2"}

    def calls(verb):
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return [line for line in lines if line.split()[0] == verb]

    def wait_for(cond, seconds):
        deadline = time.monotonic() + scaled(seconds)
        while time.monotonic() < deadline:
            if cond():
                return True
            time.sleep(0.05)
        return cond()

    proc = subprocess.Popen([BASH, RUN_SERVER.as_posix(), "Main", "llama:test", (tmp_path / "m.gguf").as_posix(), "qwen",
                             "8099", "4096", flag.as_posix()], env=env, stdin=subprocess.DEVNULL)
    try:
        assert wait_for(lambda: len(calls("run")) == 1, 10), "the supervisor never started the container"
        assert "--name agent-harness-main-llama" in calls("run")[0]
        flag.write_text("paused by test\n", encoding="utf-8")
        assert wait_for(lambda: calls("stop"), 15), "the pause flag did not stop the running container"
        assert calls("stop")[0] == "stop -t 10 agent-harness-main-llama"
        time.sleep(1)
        assert len(calls("run")) == 1, "the supervisor restarted the container while the flag exists"
        assert proc.poll() is None
        from harness_modules.local_model.service import ServerControl
        control = ServerControl(GpuGuardConfig(pause_flag=str(flag)), SimpleNamespace(base_url="http://127.0.0.1:8099"))
        asyncio.run(control.start())
        assert wait_for(lambda: len(calls("run")) == 2, 10), "removing the flag did not resume the container"
    finally:
        # Park before ending it: on Windows terminate() skips bash's TERM trap, so the supervisor must already have
        # reaped its fake container and flag watcher, leaving only its paused wait loop.
        supervisor_log = tmp_path / "logs" / "llama-server-supervisor.log"

        def parked():
            text = supervisor_log.read_text(encoding="utf-8") if supervisor_log.exists() else ""
            return text.count("stopped for the pause flag") >= len(calls("run"))
        flag.write_text("paused by test\n", encoding="utf-8")
        (tmp_path / "stopped").touch()
        wait_for(parked, 10)
        proc.terminate()
        try:
            proc.wait(timeout=scaled(15))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _end_supervisor(proc, flag, log_dir):
    """Park a running supervisor, then end it. On Windows terminate() skips bash's TERM trap, so the supervisor must
    already have reaped its fake container and flag watcher, leaving only its paused wait loop."""
    flag.write_text("paused by test\n", encoding="utf-8")
    supervisor_log = log_dir / "llama-server-supervisor.log"
    deadline = time.monotonic() + scaled(10)
    while time.monotonic() < deadline:
        text = supervisor_log.read_text(encoding="utf-8") if supervisor_log.exists() else ""
        if "paused by GPU guard" in text:
            break
        time.sleep(0.05)
    proc.terminate()
    try:
        proc.wait(timeout=scaled(15))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def test_linux_supervisor_refuses_a_port_in_the_llama_args(tmp_path):
    """A --port in the llama args would move llama-server off the port the health check probes, so the supervisor
    refuses to start and starts no container (#858). llama-server has no short port flag: -p is its prompt."""
    from tests.installer_support import BASH
    assert BASH, "the Linux supervisor test needs bash (Git Bash on Windows)"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8", newline="\n")  # bash rejects CRLF
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "DOCKER_LOG": log.as_posix(),
           "DOCKER_STOPPED": (tmp_path / "stopped").as_posix(), "HARNESS_LOG_DIR": (tmp_path / "logs").as_posix()}
    for override in (["--port", "8099"], ["--port=8099"]):
        result = subprocess.run([BASH, RUN_SERVER.as_posix(), "Main", "llama:test", (tmp_path / "m.gguf").as_posix(),
                                 "qwen", "8097", "4096", (tmp_path / "llama-server.paused").as_posix(), *override],
                                env=env, capture_output=True, text=True, timeout=scaled(15))
        assert result.returncode == 64, (override, result.stderr)
    assert not log.exists(), "the supervisor called docker despite a port override"
    # -p is llama-server's prompt flag, not a port: the supervisor still starts the container with it.
    flag = tmp_path / "llama-server.paused"
    proc = subprocess.Popen([BASH, RUN_SERVER.as_posix(), "Main", "llama:test", (tmp_path / "m.gguf").as_posix(), "qwen",
                             "8097", "4096", flag.as_posix(), "-p", "warmup"], env=env, stdin=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + scaled(10)
        while time.monotonic() < deadline and not (log.exists() and "run" in log.read_text(encoding="utf-8")):
            time.sleep(0.05)
        assert log.exists() and "run" in log.read_text(encoding="utf-8"), "the supervisor refused -p warmup"
    finally:
        _end_supervisor(proc, flag, tmp_path / "logs")


SERVE_STUDY = Path(__file__).resolve().parents[1] / "docs" / "speculative-decoding-study" / "serve.sh"
RECORD_DOCKER = """#!/usr/bin/env bash
# Records the arguments of `run`, one per line, and exits, so serve.sh's exec returns at once.
if [[ $1 == run ]]; then printf '%s\\n' "$@" >"$DOCKER_ARGS"; fi
exit 0
"""


def test_speculative_study_serve_probes_the_port_it_serves(tmp_path):
    """docs/speculative-decoding-study/serve.sh runs the image on :8081, so its health check must probe :8081, not the
    image's :8080 (#858). A --port in its extra args is refused, since the probe would no longer match the server."""
    from tests.installer_support import BASH
    assert BASH, "the study launcher test needs bash (Git Bash on Windows)"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(RECORD_DOCKER, encoding="utf-8", newline="\n")  # bash rejects CRLF
    docker.chmod(0o755)
    args_file = tmp_path / "run-args.txt"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "DOCKER_ARGS": args_file.as_posix(),
           "MODELS_DIR": tmp_path.as_posix(), "DRAFTERS_DIR": tmp_path.as_posix()}
    result = subprocess.run([BASH, SERVE_STUDY.as_posix(), "probe", (tmp_path / "probe.log").as_posix(), "--ctx-size",
                             "4096"], env=env, capture_output=True, text=True, timeout=scaled(15))
    assert result.returncode == 0, result.stderr
    argv = args_file.read_text(encoding="utf-8").splitlines()
    port = argv[argv.index("--port") + 1]
    assert port == "8081", port
    assert argv[argv.index("--health-cmd") + 1] == f"curl -f http://127.0.0.1:{port}/health"
    args_file.unlink()
    refused = subprocess.run([BASH, SERVE_STUDY.as_posix(), "probe", (tmp_path / "probe.log").as_posix(), "--port", "9000"],
                             env=env, capture_output=True, text=True, timeout=scaled(15))
    assert refused.returncode == 64, refused.stderr
    assert not args_file.exists(), "serve.sh started a container despite a port override"


def test_linux_supervisor_healthcheck_probes_the_port_the_server_listens_on(tmp_path):
    """The container's healthcheck must probe the port llama-server listens on (#858). The image's own HEALTHCHECK
    probes :8080, so a server on any other port was reported unhealthy while it answered. Run the health command the
    supervisor hands docker: it passes while the server answers /health on that port and fails once the server stops."""
    import http.server
    import shutil
    import threading
    from tests.installer_support import BASH
    assert BASH, "the Linux supervisor test needs bash (Git Bash on Windows)"
    assert shutil.which("curl"), "the healthcheck test runs the health command with curl, as the image does"

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
    port = httpd.server_address[1]
    serving = threading.Thread(target=httpd.serve_forever, daemon=True)
    serving.start()

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8", newline="\n")  # bash rejects CRLF
    docker.chmod(0o755)
    health_file = tmp_path / "health-cmd.txt"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "DOCKER_LOG": (tmp_path / "docker.log").as_posix(),
           "DOCKER_STOPPED": (tmp_path / "stopped").as_posix(), "DOCKER_HEALTH_CMD": health_file.as_posix(),
           "HARNESS_LOG_DIR": (tmp_path / "logs").as_posix(), "HARNESS_PAUSE_POLL_SECONDS": "0.2"}
    flag = tmp_path / "llama-server.paused"
    proc = subprocess.Popen([BASH, RUN_SERVER.as_posix(), "Main", "llama:test", (tmp_path / "m.gguf").as_posix(), "qwen",
                             str(port), "4096", flag.as_posix()], env=env, stdin=subprocess.DEVNULL)
    try:
        def health_cmd():
            return health_file.read_text(encoding="utf-8").strip() if health_file.exists() else ""
        deadline = time.monotonic() + scaled(10)
        while not health_cmd() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert health_cmd(), "the supervisor passed no --health-cmd to docker run"
        assert f"127.0.0.1:{port}/health" in health_cmd(), health_cmd()

        def probe():
            return subprocess.run([BASH, "-c", health_cmd()], capture_output=True, text=True,
                                  timeout=scaled(15)).returncode
        assert probe() == 0, "the healthcheck fails while the server answers on its port"
        httpd.shutdown()
        httpd.server_close()
        assert probe() != 0, "the healthcheck still passes after the server stopped answering"
    finally:
        if serving.is_alive():
            httpd.shutdown()
        httpd.server_close()
        _end_supervisor(proc, flag, tmp_path / "logs")


# the free-VRAM floor (#829): gpu_guard.min_free_vram_mib, on fake readings; no GPU needed
class FakeVram:
    """gpu_reading on rails: each poll takes the next free-VRAM figure in MiB. None is a failed reading, NA a reading
    whose free-VRAM field the driver reported as [N/A]."""
    NA = "n/a"

    def __init__(self, *free_mib):
        self.free_mib = list(free_mib)
        self.reads = 0

    def __call__(self):
        self.reads += 1
        free = self.free_mib.pop(0)
        if free is None:
            return None
        return {"vram_free": None if free == self.NA else free * service.MIB}


def floor_guard(*free_mib, floor=256, **cfg):
    guard, detect, control, scheduler, log = make_guard(min_free_vram_mib=floor, **cfg)
    guard.vram_floor = service.VramFloor(guard.cfg, read=FakeVram(*free_mib))
    return guard, detect, control, scheduler, log


async def _polls(guard, count: int) -> list[list[str]]:
    keys = []
    for _ in range(count):
        await guard.check()
        keys.append([s["key"] for s in guard.signals])
    return keys


def test_a_floor_of_zero_never_asks_nvidia_smi():
    async def body():
        guard, _, _, scheduler, _ = floor_guard(floor=0)  # no figures: a read would raise IndexError
        assert await _polls(guard, 3) == [[], [], []]
        assert guard.vram_floor.read.reads == 0 and guard.state == CLEAR and not scheduler.paused
    asyncio.run(body())


def test_the_floor_is_off_unless_configured():
    assert GpuGuardConfig().min_free_vram_mib == 0
    assert GpuGuardConfig().vram_drain_timeout_seconds == 30


def test_one_low_reading_is_no_signal_and_two_are():
    async def body():
        guard, _, control, _, _ = floor_guard(100, 90, floor=256)
        await guard.check()
        assert guard.signals == [] and guard.state == CLEAR and control.stops == 0
        await guard.check()
        assert [(s["key"], s["kind"]) for s in guard.signals] == [("vram:low", "vram")]
        assert "90 MiB" in guard.signals[0]["detail"] and "256 MiB" in guard.signals[0]["detail"]
        assert guard.state == PAUSED and control.stops == 1
        assert [r["key"] for r in guard.reasons] == ["vram:low"]
    asyncio.run(body())


async def _park_on_the_floor(guard, control, *, later_in_cooldown: bool = False) -> None:
    await _polls(guard, 2)
    assert guard.state == PAUSED and control.flag and control.stops == 1
    if later_in_cooldown:  # the flag has hidden the signal, and only `reasons` still names the floor
        await guard.check()
        assert guard.state == PAUSED and guard.signals == []


async def _reloaded_after_resume_anyway(guard, control, reload: str) -> None:
    """The real resume() and the check() that acts on it. Only the lazy-idle model needs something else to load it."""
    guard.resume()
    await guard.check()
    assert guard.state == CLEAR
    if reload == "lazy-idle":
        assert control.flag and control.starts == 0  # stays parked until a request asks; the warmer removes the flag
        await control.start()
    assert not control.flag and control.starts == 1


@pytest.mark.parametrize("reload, cfg, want", [
    pytest.param("eager", {"lazy_load": False}, False, id="eager-reload"),
    pytest.param("lazy-queued", {"lazy_load": True}, True, id="lazy-reload-with-work-queued"),
    pytest.param("lazy-idle", {"lazy_load": True}, False, id="lazy-reload-on-the-next-request"),
])
@pytest.mark.parametrize("later", [False, True], ids=["right-after-the-park", "later-in-the-cooldown"])
def test_resume_anyway_outlasts_the_park_and_the_debounce_after_the_reload(reload, cfg, want, later):
    """Codex round 1: the flag hides the signal while parked, and that must not read as the trigger having gone away."""
    async def body():
        guard, _, control, scheduler, log = floor_guard(100, 90, 80, 70, 60, 50, floor=256, resume_after_seconds=999, **cfg)
        guard.want_model = lambda: want
        await _park_on_the_floor(guard, control, later_in_cooldown=later)
        await _reloaded_after_resume_anyway(guard, control, reload)
        keys = await _polls(guard, 4)
        assert keys == [[], ["vram:low"], ["vram:low"], ["vram:low"]]  # the count restarts; the signal is simply ignored
        assert guard.state == CLEAR and not scheduler.paused and control.stops == 1
        assert [k for k, _ in log] == ["pause", "resume"]
        assert guard.status()["override"] is True
    asyncio.run(body())


def test_resume_anyway_during_the_drain_holds_across_the_cancelled_pause():
    async def body():
        guard, _, control, scheduler, _ = floor_guard(100, 100, 100, 100, floor=256, busy=lambda: True,
                                                      resume_after_seconds=999)
        await _polls(guard, 2)
        assert guard.state == PAUSING
        guard.resume()
        await _polls(guard, 2)
        assert guard.state == CLEAR and control.stops == 0 and not scheduler.paused
    asyncio.run(body())


def test_resume_anyway_holds_when_a_ram_wait_replaced_the_floor_reason():
    async def body():
        guard, _, control, _, _ = floor_guard(100, 100, 100, 100, floor=256, lazy_load=True, resume_after_seconds=0,
                                              min_available_ram_gb=4, model_ram_gb=14)
        ram = FakeRam(9)
        guard.memory = MemoryWatch(4, model_gb=14, read=ram, ttl=0)
        guard.want_model = lambda: True
        await _polls(guard, 2)
        await guard.check()
        assert guard.memory_hold and [r["key"] for r in guard.reasons] == ["memory_hold"]
        guard.resume()
        ram.available = 30 * GIB
        await guard.check()
        assert guard.state == CLEAR and not control.flag
        await _polls(guard, 2)
        assert guard.state == CLEAR and control.stops == 1
    asyncio.run(body())


def test_a_high_reading_before_the_model_is_back_on_the_card_is_no_recovery():
    """The model is off the card until it has loaded, so free VRAM is high then whatever else holds the rest."""
    async def body():
        guard, _, control, _, _ = floor_guard(100, 100, 4000, 100, 100, 100, floor=256, resume_after_seconds=999)
        await _park_on_the_floor(guard, control)
        control.health = False  # loading: /health and /props aren't answering yet
        guard.resume()
        await guard.check()
        await guard.check()  # 4000 MiB free while it loads
        assert guard.status()["override"] is True and guard.state == "resuming"
        control.health = True
        await _polls(guard, 3)
        assert guard.state == CLEAR and control.stops == 1
    asyncio.run(body())


class GatedVram(FakeVram):
    """FakeVram whose next read can be held open, as a slow nvidia-smi is, while the model changes state around it."""

    def __init__(self, *free_mib):
        super().__init__(*free_mib)
        self.hold_next = False
        self.entered, self.go = threading.Event(), threading.Event()

    def __call__(self):
        if self.hold_next:
            self.hold_next = False
            self.entered.set()
            assert self.go.wait(10)
        return super().__call__()


class FakeLlama:
    """The far edge of the real ModelWarmer: llama-server's /props and chat route, on FakeControl's health."""

    def __init__(self, monkeypatch, control):
        import httpx
        from harness_modules.local_model import warmup
        llama = self
        self.sleeping = False

        class Client:
            def __init__(self, timeout=None): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return False

            async def get(self, url):
                if not control.health:
                    raise httpx.ConnectError("still loading")
                return httpx.Response(200, json={"is_sleeping": llama.sleeping}, request=httpx.Request("GET", url))

            async def post(self, url, json=None):
                llama.sleeping = False
                return httpx.Response(200, json={}, request=httpx.Request("POST", url))
        monkeypatch.setattr(ModelWarmer, "state", REAL_WARMER_STATE)
        monkeypatch.setattr(warmup.httpx, "AsyncClient", Client)


async def _acknowledged_and_parked(tmp_path, monkeypatch, *free_mib):
    """A real manager's guard and warmer, parked on the floor with Resume anyway pressed: lazy, so the next request
    loads it. Fakes only at the edges: nvidia-smi, the control's flag and health, and llama-server's HTTP."""
    m = guarded_manager(tmp_path, min_free_vram_mib=256, lazy_load=True)
    m.guard.cfg.resume_after_seconds = 999
    vram = GatedVram(100, 100, *free_mib)
    m.guard.vram_floor = service.VramFloor(m.guard.cfg, read=vram)
    llama = FakeLlama(monkeypatch, m.guard.control)
    await _park_on_the_floor(m.guard, m.guard.control)
    m.guard.resume()
    await m.guard.check()
    assert m.guard.state == CLEAR and m.guard.control.flag and m.guard.status()["override"] is True
    return m, vram, llama, m.cfg.models[m.cfg.default_model]


async def _poll_with_the_read_held(guard, vram):
    vram.hold_next = True
    poll = asyncio.create_task(guard.check())
    await _until(vram.entered.is_set)
    return poll


async def _then_a_loaded_high_reading_retires_it_and_the_floor_guards_again(m):
    await m.guard.check()
    assert m.guard.status()["override"] is False
    assert await _polls(m.guard, 2) == [[], ["vram:low"]]
    assert m.guard.state == PAUSED and m.guard.control.stops == 2


def test_a_reading_taken_before_a_lazy_reload_finished_is_no_recovery(tmp_path, monkeypatch):
    """Independent review of b48cc4a: the load finishes between the sample and the readiness check."""
    import harness_modules.local_model.warmup as warmup
    monkeypatch.setattr(warmup, "HEALTH_POLL_SECONDS", 3600)

    async def body():
        m, vram, _, model = await _acknowledged_and_parked(tmp_path, monkeypatch, 4000, 4000, 100, 100)
        control = m.guard.control
        control.health = False
        load = asyncio.create_task(m.warmer.ensure_loaded(model))
        await _until(lambda: control.starts == 1 and not control.flag)
        assert await m.warmer.state(model) == WAKING
        poll = await _poll_with_the_read_held(m.guard, vram)
        control.health = True
        m.warmer.notify()
        await asyncio.wait_for(load, 2)
        assert await m.warmer.state(model) == READY  # loaded by the time the reading comes back
        vram.go.set()
        await asyncio.wait_for(poll, 2)
        assert m.guard.status()["override"] is True and m.guard.state == CLEAR
        await _then_a_loaded_high_reading_retires_it_and_the_floor_guards_again(m)
    asyncio.run(body())


def test_a_reading_taken_while_the_model_slept_is_no_recovery_though_it_woke_meanwhile(tmp_path, monkeypatch):
    async def body():
        m, vram, llama, model = await _acknowledged_and_parked(tmp_path, monkeypatch, 4000, 4000, 100, 100)
        await m.warmer.ensure_loaded(model)
        llama.sleeping = True
        poll = await _poll_with_the_read_held(m.guard, vram)
        await m.warmer.warm(model)
        await asyncio.wait_for(m.warmer.ensure_loaded(model), 2)
        assert not llama.sleeping and await m.warmer.state(model) == READY
        vram.go.set()
        await asyncio.wait_for(poll, 2)
        assert m.guard.status()["override"] is True
        await _then_a_loaded_high_reading_retires_it_and_the_floor_guards_again(m)
    asyncio.run(body())


def test_a_reading_spanning_an_unload_and_reload_is_no_recovery_though_ready_both_sides(tmp_path, monkeypatch):
    """Ready before and ready after, with the model off the card in between: only the transition gives it away."""
    async def body():
        m, vram, _, model = await _acknowledged_and_parked(tmp_path, monkeypatch, 4000, 4000, 100, 100)
        await m.warmer.ensure_loaded(model)
        poll = await _poll_with_the_read_held(m.guard, vram)
        assert await m.guard.unload()
        await m.warmer.ensure_loaded(model)
        assert m.guard.control.stops == 2 and await m.warmer.state(model) == READY
        vram.go.set()
        await asyncio.wait_for(poll, 2)
        assert m.guard.status()["override"] is True
        await m.guard.check()
        assert m.guard.status()["override"] is False
    asyncio.run(body())


def test_a_measured_recovery_with_the_model_loaded_retires_it_and_the_floor_guards_again():
    async def body():
        guard, _, control, _, _ = floor_guard(100, 100, 4000, 100, 100, floor=256, resume_after_seconds=999)
        await _park_on_the_floor(guard, control)
        await _reloaded_after_resume_anyway(guard, control, "eager")
        await guard.check()
        assert guard.status()["override"] is False
        assert await _polls(guard, 2) == [[], ["vram:low"]]
        assert guard.state == PAUSED and control.stops == 2
    asyncio.run(body())


@pytest.mark.parametrize("unknown", [None, FakeVram.NA], ids=["failed-reading", "free-vram-not-reported"])
def test_a_reading_that_measures_nothing_does_not_retire_it(unknown):
    async def body():
        guard, _, control, _, _ = floor_guard(100, 100, unknown, 100, 100, floor=256, resume_after_seconds=999)
        await _park_on_the_floor(guard, control)
        await _reloaded_after_resume_anyway(guard, control, "eager")
        await _polls(guard, 3)
        assert guard.status()["override"] is True and guard.state == CLEAR and control.stops == 1
    asyncio.run(body())


def test_a_new_trigger_ends_it_and_the_floor_counts_again_after_that_pause():
    async def body():
        guard, detect, control, _, _ = floor_guard(100, 100, 100, 100, 100, 100, 100, floor=256, resume_after_seconds=0)
        await _park_on_the_floor(guard, control)
        await _reloaded_after_resume_anyway(guard, control, "eager")
        await _polls(guard, 2)
        assert guard.state == CLEAR and control.stops == 1
        detect.signals = [GAME]
        await guard.check()
        assert guard.state == PAUSED and control.stops == 2 and guard.status()["override"] is False
        assert {r["key"] for r in guard.reasons} == {"game:hades.exe", "vram:low"}
        detect.signals = []
        await guard.check()  # the game has gone and the eager path reloads
        assert guard.state == CLEAR and not control.flag
        assert await _polls(guard, 2) == [[], ["vram:low"]]
        assert guard.state == PAUSED and control.stops == 3
    asyncio.run(body())


def test_resuming_a_manual_hold_resets_it():
    async def body():
        guard, _, control, _, _ = floor_guard(100, 100, 100, 100, 100, floor=256, resume_after_seconds=0)
        await _park_on_the_floor(guard, control)
        await _reloaded_after_resume_anyway(guard, control, "eager")
        guard.pause()
        await guard.check()
        assert guard.state == PAUSED
        guard.resume(override_signals=False)  # what the route sends for a manual hold
        assert guard.status()["override"] is False
        await guard.check()
        assert await _polls(guard, 2) == [[], ["vram:low"]]
        assert guard.state == PAUSED and control.stops == 3
    asyncio.run(body())


def test_turning_the_floor_off_resets_it():
    async def body():
        guard, _, control, _, _ = floor_guard(100, 100, 100, 100, floor=256, resume_after_seconds=999)
        await _park_on_the_floor(guard, control)
        await _reloaded_after_resume_anyway(guard, control, "eager")
        guard.cfg.min_free_vram_mib = 0
        await guard.check()
        assert guard.status()["override"] is False
        guard.cfg.min_free_vram_mib = 256
        assert await _polls(guard, 2) == [[], ["vram:low"]]
        assert guard.state == PAUSED
    asyncio.run(body())


@pytest.mark.parametrize("cause", ["game-only-pause", "no-pause-at-all"])
def test_a_resume_that_did_not_see_the_floor_acknowledges_nothing(cause):
    async def body():
        guard, detect, control, _, _ = floor_guard(100, 100, floor=256, lazy_load=False, resume_after_seconds=0)
        control.flag = True  # parked already, so the floor can't be counting
        if cause == "game-only-pause":
            detect.signals = [GAME]
            await guard.check()
            assert guard.state == PAUSED
            detect.signals = []
        guard.resume()
        await guard.check()
        control.flag = False
        assert await _polls(guard, 2) == [[], ["vram:low"]]
        assert guard.state == PAUSED
    asyncio.run(body())


def test_the_key_stays_the_same_as_the_reading_changes():
    async def body():
        guard, _, _, _, _ = floor_guard(100, 90, 80, floor=256, busy=lambda: True)
        assert await _polls(guard, 3) == [[], ["vram:low"], ["vram:low"]]
        assert guard.signals[0]["detail"].startswith("free VRAM 80 MiB")
    asyncio.run(body())


@pytest.mark.parametrize("between", [
    pytest.param(256, id="exactly-at-the-floor"),
    pytest.param(4000, id="above-the-floor"),
    pytest.param(None, id="failed-reading"),
    pytest.param(FakeVram.NA, id="free-vram-not-reported"),
])
def test_a_reading_at_the_floor_or_a_failed_one_resets_the_count(between):
    async def body():
        guard, _, control, scheduler, _ = floor_guard(100, between, 100, 100, floor=256)
        assert await _polls(guard, 3) == [[], [], []]
        assert guard.state == CLEAR and control.stops == 0 and not scheduler.paused
        assert await _polls(guard, 1) == [["vram:low"]]  # the fourth is the second low in a row
    asyncio.run(body())


def test_a_parked_model_does_not_count_low_vram_and_nvidia_smi_is_not_asked():
    """An Images batch parks the model through the same flag, then ComfyUI fills the card: that's no reason to pause."""
    async def body():
        guard, _, control, scheduler, log = floor_guard(floor=256)
        control.flag = True
        assert await _polls(guard, 3) == [[], [], []]
        assert guard.vram_floor.read.reads == 0
        assert guard.state == CLEAR and not scheduler.paused and log == []
    asyncio.run(body())


def test_parking_resets_the_count_so_an_earlier_low_reading_does_not_carry_over():
    async def body():
        guard, _, control, _, _ = floor_guard(100, 100, 100, floor=256)
        await _polls(guard, 1)
        control.flag = True
        await _polls(guard, 1)
        control.flag = False
        assert await _polls(guard, 1) == [[]]  # one low since the park, not two
        assert await _polls(guard, 1) == [["vram:low"]]
    asyncio.run(body())


def test_a_park_that_lands_during_the_reading_does_not_count_it():
    """nvidia-smi can take seconds. A batch that parks meanwhile may already hold the card when the figure comes back."""
    async def body():
        guard, _, control, scheduler, _ = floor_guard(floor=256)
        figures = [100, 100]

        def read():
            if len(figures) == 1:
                control.flag = True
            return {"vram_free": figures.pop(0) * service.MIB}
        guard.vram_floor = service.VramFloor(guard.cfg, read=read)
        assert await _polls(guard, 2) == [[], []]
        assert guard.state == CLEAR and not scheduler.paused
    asyncio.run(body())


def test_the_floor_reads_live_so_a_settings_write_applies_at_the_next_poll():
    async def body():
        guard, _, _, _, _ = floor_guard(100, 100, floor=0)
        assert await _polls(guard, 1) == [[]]
        guard.cfg.min_free_vram_mib = 256
        assert await _polls(guard, 2) == [[], ["vram:low"]]
    asyncio.run(body())


def test_two_polls_take_two_readings_from_a_fake_nvidia_smi_not_one_cached(monkeypatch):
    """gpu_reading(cached=True) serves /metrics for 10 s; counting that would turn a single reading into two polls."""
    from harness_modules.local_model import resources
    asked = []
    monkeypatch.setattr(resources, "_gpu_cache", (-1e9, None))
    monkeypatch.setattr(resources, "_run", lambda args, timeout=5: asked.append(args) or "15800, 16000, 37, 100, 0\n")

    async def body():
        guard, _, control, _, _ = make_guard(min_free_vram_mib=256)
        assert await _polls(guard, 2) == [[], ["vram:low"]]
        assert control.stops == 1
    asyncio.run(body())
    assert len(asked) == 2


def _low_then(guard_cfg: dict, signals: list[dict]):
    busy = {"v": True}
    guard, detect, control, _, _ = floor_guard(100, 100, 100, 100, floor=256, busy=lambda: busy["v"], **guard_cfg)
    detect.signals = signals
    return guard, control


def test_a_pause_this_signal_starts_drains_for_the_shorter_timeout():
    async def body():
        guard, control = _low_then({"vram_drain_timeout_seconds": 0.05, "drain_timeout_seconds": 999}, [])
        await _polls(guard, 2)
        assert guard.state == PAUSING and control.stops == 0  # a turn is running
        await asyncio.sleep(0.1)
        await guard.check()
        assert guard.state == PAUSED and control.stops == 1
    asyncio.run(body())


def test_a_pause_with_another_trigger_present_keeps_the_longer_drain():
    async def body():
        guard, control = _low_then({"vram_drain_timeout_seconds": 0.05, "drain_timeout_seconds": 999}, [GAME])
        await _polls(guard, 2)
        assert {s["key"] for s in guard.signals} == {"game:hades.exe", "vram:low"}
        await asyncio.sleep(0.1)
        await guard.check()
        assert guard.state == PAUSING and control.stops == 0
    asyncio.run(body())


def test_vram_joining_a_game_pause_already_draining_keeps_the_longer_drain():
    async def body():
        guard, control = _low_then({"vram_drain_timeout_seconds": 0.05, "drain_timeout_seconds": 999}, [GAME])
        await guard.check()
        assert guard.state == PAUSING
        guard.detector.signals = []  # the game is gone and only the floor is left, but the game began this pause
        await guard.check()
        assert [s["key"] for s in guard.signals] == ["vram:low"]
        await asyncio.sleep(0.1)
        await guard.check()
        assert guard.state == PAUSING and control.stops == 0
    asyncio.run(body())


def test_a_manual_hold_drains_as_before_even_with_the_floor_breached():
    async def body():
        guard, control = _low_then({"vram_drain_timeout_seconds": 0.05, "drain_timeout_seconds": 999}, [])
        guard.pause()
        await _polls(guard, 2)
        assert [s["key"] for s in guard.signals] == ["vram:low"]
        await asyncio.sleep(0.1)
        await guard.check()
        assert guard.state == PAUSING and control.stops == 0 and guard.manual
    asyncio.run(body())


def test_after_the_park_the_guard_clears_with_the_model_still_parked_and_nothing_queued():
    async def body():
        guard, _, control, scheduler, log = floor_guard(100, 100, floor=256, lazy_load=True, resume_after_seconds=0)
        guard.want_model = lambda: False
        await _polls(guard, 2)
        assert guard.state == PAUSED and control.flag and control.stops == 1
        await guard.check()  # flag present: nothing read, nothing to hold for, and resume_after_seconds has passed
        assert guard.vram_floor.read.reads == 2
        assert (guard.state, control.flag, control.starts) == (CLEAR, True, 0)
        assert not scheduler.paused and [k for k, _ in log] == ["pause", "resume"]
        assert guard.status()["parked"] is True
    asyncio.run(body())


def test_the_park_waits_out_resume_after_seconds_like_any_other_trigger():
    async def body():
        guard, _, control, scheduler, _ = floor_guard(100, 100, floor=256, lazy_load=True, resume_after_seconds=999)
        await _polls(guard, 3)
        assert guard.state == PAUSED and scheduler.paused and control.flag
    asyncio.run(body())


def test_work_queued_during_the_hold_reloads_the_model_as_with_any_other_trigger():
    async def body():
        guard, _, control, _, _ = floor_guard(100, 100, floor=256, lazy_load=True, resume_after_seconds=0)
        guard.want_model = lambda: True
        await _polls(guard, 3)
        assert control.starts == 1 and not control.flag and guard.state == CLEAR
    asyncio.run(body())


def test_metrics_count_the_vram_trigger(tmp_path):
    from harness.metrics import render
    m = guarded_manager(tmp_path)
    m.guard.signals = [{"key": "vram:low", "kind": "vram", "detail": "x"}]
    assert 'harness_gpu_guard_triggers{kind="vram"} 1' in render(m)


@pytest.mark.parametrize("key, value", [
    ("min_free_vram_mib", -1), ("min_free_vram_mib", 25.5), ("min_free_vram_mib", "256"), ("min_free_vram_mib", True),
    ("vram_drain_timeout_seconds", -1), ("vram_drain_timeout_seconds", "soon"),
    ("vram_drain_timeout_seconds", float("inf")),
])
def test_config_refuses_a_bad_floor_or_drain_timeout(key, value):
    from harness.config import validate_gpu_guard
    with pytest.raises(ValueError, match=key):
        validate_gpu_guard(GpuGuardConfig(**{key: value}))


def test_config_accepts_the_tower_floor_and_the_defaults():
    from harness.config import validate_gpu_guard
    validate_gpu_guard(GpuGuardConfig())
    validate_gpu_guard(GpuGuardConfig(min_free_vram_mib=256, vram_drain_timeout_seconds=30))


def test_settings_registry_lists_the_floor_and_its_drain_timeout():
    from harness_modules.local_model.settings import specs
    by_key = {s.key: s for s in specs()}
    floor, drain = by_key["gpu_guard.min_free_vram_mib"], by_key["gpu_guard.vram_drain_timeout_seconds"]
    assert (floor.value_type, floor.default, floor.bounds.minimum) == ("int", 0, 0)
    assert (drain.value_type, drain.default) == ("float", 30)
    assert floor.yaml_path == ("gpu_guard", "min_free_vram_mib")
    assert drain.yaml_path == ("gpu_guard", "vram_drain_timeout_seconds")


@pytest.mark.parametrize("value", [-1, 25.5, 65537])
def test_settings_refuse_a_floor_that_is_negative_fractional_or_out_of_range(value):
    from harness.settings import parse_value
    from harness_modules.local_model.settings import specs
    floor = next(s for s in specs() if s.key == "gpu_guard.min_free_vram_mib")
    with pytest.raises(ValueError):
        parse_value(floor, value)
    assert parse_value(floor, 256) == 256 and parse_value(floor, 0) == 0


def test_loading_a_config_with_a_negative_floor_fails_and_the_shipped_one_is_off(tmp_path):
    import yaml
    from harness.config import load
    from test_config_registry import _write_loadable_config
    cfg_dir, data_dir = _write_loadable_config(tmp_path, "gpu_guard: {min_free_vram_mib: -1}\n")
    with pytest.raises(ValueError, match="min_free_vram_mib"):
        load(cfg_dir, data_dir)
    shipped = yaml.safe_load((Path(__file__).parents[1] / "config" / "harness.yaml").read_text(encoding="utf-8"))
    assert shipped["gpu_guard"]["min_free_vram_mib"] == 0
    assert shipped["gpu_guard"]["vram_drain_timeout_seconds"] == 30


@pytest.mark.parametrize("state, loaded", [(READY, True), (WAKING, False), (UNLOADED, False), ("sleeping", False)])
def test_the_daemon_counts_a_recovery_only_with_the_model_ready_not_asleep(tmp_path, monkeypatch, state, loaded):
    """An idle-unloaded llama-server still answers /health, with the model off the card."""
    m = guarded_manager(tmp_path)

    async def fixed(self, model):
        return state
    monkeypatch.setattr(ModelWarmer, "state", fixed)
    assert asyncio.run(m.guard.model_loaded()) is loaded
