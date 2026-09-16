"""Phase 5 tests: GPU contention guard, metrics, backups, memory library, rebuild_service. No GPU or Docker needed."""

from __future__ import annotations

import asyncio

from harness.config import GpuGuardConfig
from harness.gpu_guard import CLEAR, PAUSED, PAUSING, RESUMING, GpuGuard, find_games, hw_transcodes
from harness.llm import Completion
from harness.manager import Manager
from harness.scheduler import GpuScheduler

from test_daemon import Script, call, events, make_cfg, wait_status


# detection
def test_find_games_ignores_tools_and_matches_titles():
    cfg = GpuGuardConfig(game_processes=["Custom.exe"])
    paths = [
        r"C:\Program Files (x86)\Steam\steamapps\common\wallpaper_engine\wallpaper64.exe",
        r"C:\Program Files (x86)\Steam\steamapps\common\Hades\x64\Hades.exe",
        r"C:\Program Files (x86)\Steam\steam.exe",
        r"D:\Stuff\Custom.exe",
    ]
    found = {s["key"] for s in find_games(cfg, paths, ["Steam Big Picture Mode", "Untitled - Notepad"])}
    assert found == {"game:hades.exe", "game:custom.exe", "window:steam big picture mode"}


def test_plex_only_hardware_video_transcodes_count():
    body = {"MediaContainer": {"TranscodeSession": [
        {"key": "/transcode/sessions/a", "videoDecision": "transcode", "transcodeHwEncoding": "nvenc"},
        {"key": "/transcode/sessions/b", "videoDecision": "copy", "transcodeHwRequested": True},
        {"key": "/transcode/sessions/c", "videoDecision": "transcode"},
    ]}}
    assert [s["key"] for s in hw_transcodes(body)] == ["plex:a"]
    assert hw_transcodes({"MediaContainer": {"size": 0}}) == []


# scheduler
def test_scheduler_pause_holds_grants_and_front_requeue():
    async def body():
        s = GpuScheduler()
        await s.acquire("a")
        s.set_paused(True)
        waiter_b = asyncio.create_task(s.acquire("b"))
        await asyncio.sleep(0)
        s.release("a")
        await asyncio.sleep(0)
        assert s.holder is None and not waiter_b.done()
        waiter_a = asyncio.create_task(s.acquire("a", front=True))
        await asyncio.sleep(0)
        assert s.positions() == {"a": 1, "b": 2}
        s.set_paused(False)
        await asyncio.wait_for(waiter_a, 1)
        assert s.holder == "a" and not waiter_b.done()
        s.release("a")
        await asyncio.wait_for(waiter_b, 1)
    asyncio.run(body())


def test_scheduler_waits_for_snapshot_to_drain():
    async def body():
        s = GpuScheduler()
        s.set_paused(True)
        a = asyncio.create_task(s.acquire("a"))
        b = asyncio.create_task(s.acquire("b"))
        await asyncio.sleep(0)
        snapshot = set(s.positions())
        drained = asyncio.create_task(s.wait_for_drain(snapshot))
        s.set_paused(False)
        await a
        assert not drained.done()
        s.release("a")
        await b
        assert not drained.done()
        s.release("b")
        await asyncio.wait_for(drained, 1)
    asyncio.run(body())


# guard state machine
class FakeControl:
    def __init__(self):
        self.running, self.flag, self.health = True, False, True
        self.stops = self.starts = 0

    def flagged(self):
        return self.flag

    async def stop(self):
        self.flag, self.running = True, False
        self.stops += 1

    async def start(self):
        self.flag = False
        self.starts += 1

    async def healthy(self):
        return self.health


class FakeDetect:
    def __init__(self):
        self.signals = []

    async def __call__(self):
        return list(self.signals)


GAME = {"key": "game:hades.exe", "kind": "game", "detail": "Hades.exe"}


def make_guard(busy=lambda: False, **cfg):
    detect, control, scheduler = FakeDetect(), FakeControl(), GpuScheduler()
    log = []
    guard = GpuGuard(GpuGuardConfig(enabled=True, **cfg), None, scheduler, busy, detect=detect, control=control,
                     on_pause=lambda r: log.append(("pause", r)), on_resume=lambda s: log.append(("resume", s)))
    return guard, detect, control, scheduler, log


def test_guard_waits_for_turn_then_stops_and_resumes_after_quiet_period():
    async def body():
        busy = {"v": True}
        guard, detect, control, scheduler, log = make_guard(busy=lambda: busy["v"], resume_after_seconds=0.2)
        await guard.check()
        assert guard.state == CLEAR
        detect.signals = [GAME]
        await guard.check()
        assert guard.state == PAUSING and scheduler.paused and control.stops == 0  # the turn is still running
        busy["v"] = False
        await guard.check()
        assert guard.state == PAUSED and control.stops == 1 and control.flag
        detect.signals = []
        await guard.check()
        assert guard.state == PAUSED  # not clear for long enough yet
        await asyncio.sleep(0.25)
        control.health = False
        await guard.check()
        assert guard.state == RESUMING and not control.flag and scheduler.paused
        control.health = True
        await guard.check()
        assert guard.state == CLEAR and not scheduler.paused
        assert [k for k, _ in log] == ["pause", "resume"]
    asyncio.run(body())


def test_guard_drain_timeout_and_short_trigger():
    async def body():
        guard, detect, control, scheduler, log = make_guard(busy=lambda: True, drain_timeout_seconds=0.1)
        detect.signals = [GAME]
        await guard.check()
        detect.signals = []
        await guard.check()  # the game closed before the turn finished: no stop at all
        assert guard.state == CLEAR and control.stops == 0 and not scheduler.paused
        detect.signals = [GAME]
        await guard.check()
        await asyncio.sleep(0.15)
        await guard.check()
        assert guard.state == PAUSED and control.stops == 1  # the turn outlasted the drain timeout
    asyncio.run(body())


def test_guard_manual_pause_and_override_until_triggers_change():
    async def body():
        guard, detect, control, scheduler, log = make_guard(resume_after_seconds=999)
        guard.pause()
        await guard.check()
        assert guard.state == PAUSED and guard.reasons[0]["kind"] == "manual"
        guard.resume()
        await guard.check()
        await guard.check()
        assert guard.state == CLEAR
        detect.signals = [GAME]
        await guard.check()
        await guard.check()
        assert guard.state == PAUSED
        guard.resume()  # "resume anyway" while the game is still running
        await guard.check()
        await guard.check()
        assert guard.state == CLEAR
        await guard.check()
        assert guard.state == CLEAR  # same trigger: override holds
        detect.signals = [GAME, {"key": "plex:x", "kind": "plex", "detail": "Plex transcode"}]
        await guard.check()
        assert guard.state in (PAUSING, PAUSED)  # a new trigger ends the override
    asyncio.run(body())


def test_guard_timed_manual_hold_expires_like_resume():
    async def body():
        guard, _, control, scheduler, log = make_guard(resume_after_seconds=999)
        guard.pause(duration_seconds=1)
        assert guard.state == PAUSING and scheduler.paused
        await guard.check()
        status = guard.status()
        assert status["manual"] and 0 <= status["manual_remaining_seconds"] <= 1
        assert guard.state == PAUSED and scheduler.paused
        guard.manual_until = 0
        await guard.check()
        await guard.check()
        assert guard.state == CLEAR and not guard.manual and not scheduler.paused
        assert control.starts == 1 and [kind for kind, _ in log] == ["pause", "resume"]
    asyncio.run(body())


def test_guard_does_not_restart_model_during_image_exclusive():
    async def body():
        busy = {"value": False}
        guard, _, control, scheduler, _ = make_guard(busy=lambda: busy["value"], resume_after_seconds=0)
        guard.pause()
        await guard.check()
        assert guard.state == PAUSED
        busy["value"] = True
        guard.resume()
        await guard.check()
        assert guard.state == PAUSED and control.starts == 0 and scheduler.paused
        busy["value"] = False
        await guard.check()
        await guard.check()
        assert guard.state == CLEAR and control.starts == 1
    asyncio.run(body())


def test_gpu_hold_api_accepts_optional_duration(tmp_path):
    from fastapi.testclient import TestClient
    from harness.api import create_app

    cfg = make_cfg(tmp_path)
    cfg.gpu_guard = GpuGuardConfig(enabled=True, poll_seconds=3600)
    m = Manager(cfg, chat=Script([Completion(content="done")]))
    m.guard.detector, m.guard.control = FakeDetect(), FakeControl()
    with TestClient(create_app(m)) as client:
        held = client.post("/gpu/pause", json={"duration_seconds": 1800}).json()
        assert held["manual"] and 1790 <= held["manual_remaining_seconds"] <= 1800
        assert held["manual_duration_seconds"] == 1800 and held["state"] == "pausing"
        assert client.post("/gpu/pause", json={"duration_seconds": 0}).status_code == 400
        resumed = client.post("/gpu/resume").json()
        assert not resumed["manual"] and resumed["manual_until"] is None


def test_guard_startup_with_leftover_flag_resumes_when_clear():
    async def body():
        guard, detect, control, scheduler, log = make_guard(resume_after_seconds=999, poll_seconds=0.05)
        control.flag = True
        guard.start()
        assert guard.state in (PAUSED, RESUMING, CLEAR)
        for _ in range(50):
            if guard.state == CLEAR:
                break
            await asyncio.sleep(0.02)
        assert guard.state == CLEAR and not control.flag and not scheduler.paused
        await guard.stop()
    asyncio.run(body())


# guard with sessions
def test_session_pauses_before_next_turn_and_continues(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.gpu_guard = GpuGuardConfig(enabled=True, resume_after_seconds=0, poll_seconds=3600)
    gate = asyncio.Event()

    async def body():
        steps = Script([Completion(tool_calls=[call("write_file", 0, path="a.txt", content="x")]),
                        Completion(content="done")])
        detect, control = FakeDetect(), FakeControl()

        async def chat(*args, **kwargs):
            if sum(1 for m in args[1] if m["role"] == "assistant") == 1:
                await gate.wait()  # second turn: wait until the test has paused the GPU
            return await steps(*args, **kwargs)

        m = Manager(cfg, chat=chat)
        m.guard.detector, m.guard.control = detect, control
        await m.start(maintenance=False)
        s = m.create("write a file")
        sid = s["id"]
        for _ in range(200):
            if sid in m.runner.generating and events(m, sid, "tool_result"):
                break
            await asyncio.sleep(0.01)
        detect.signals = [GAME]
        await m.guard.check()  # the second turn is already waiting on `gate`, so it counts as busy
        assert m.guard.state == PAUSING
        gate.set()
        await wait_status(m, sid, "done")  # the turn in flight finishes
        await m.guard.check()
        assert m.guard.state == PAUSED and control.stops == 1
        assert events(m, sid, "gpu_paused")[0]["reason"] == "Hades.exe"
        assert any((m.notifier.build(e) or {}).get("title", "").startswith("Paused for the GPU") for e in m.db.events(sid))

        # a follow-up while paused waits in the queue, then runs once the GPU is clear
        await m.send(sid, "again")
        await wait_status(m, sid, "queued")
        await asyncio.sleep(0.1)
        assert m.db.get_session(sid)["status"] == "queued"
        detect.signals = []
        await m.guard.check()
        await m.guard.check()
        await wait_status(m, sid, "done")
        assert events(m, sid, "gpu_resumed")
        await m.stop()
    asyncio.run(body())


# metrics and backups
def test_metrics_and_backup(tmp_path):
    import sqlite3
    import zipfile
    from harness.config import BackupConfig
    from harness.metrics import render

    cfg = make_cfg(tmp_path)
    cfg.backup = BackupConfig(enabled=False, dir=str(tmp_path / "backups"), keep_days=14)

    async def body():
        m = Manager(cfg, chat=Script([Completion(tool_calls=[call("write_file", 0, path="a.txt", content="x")],
                                                 prompt_tokens=50, completion_tokens=5, gen_tps=70.0),
                                      Completion(content="done", prompt_tokens=60, completion_tokens=6)]))
        await m.start(maintenance=False)
        s = m.create("write")
        await wait_status(m, s["id"], "done")
        await asyncio.gather(*list(m.tasks.values()), return_exceptions=True)
        text = render(m)
        assert 'harness_sessions{status="done"} 1' in text
        assert 'harness_tokens_total{model="fake",kind="completion"} 11' in text
        assert 'harness_tool_calls_total{tool="write_file",ok="true"} 1' in text
        assert "harness_queue_depth 0" in text

        old = tmp_path / "backups" / "2020-01-01"
        old.mkdir(parents=True)
        result = await m.maintenance.backup()
        dest = tmp_path / "backups" / result["path"].replace("\\", "/").rsplit("/", 1)[-1]
        assert not old.exists() and result["removed"] == ["2020-01-01"]
        with sqlite3.connect(dest / "harness.sqlite3") as conn:
            assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert zipfile.ZipFile(dest / "transcripts.zip").namelist()
        assert "harness_backup_last_success_timestamp_seconds" in render(m)
        await m.stop()
    asyncio.run(body())


# memory library
def test_memory_library_only_exposes_allowed_categories(tmp_path):
    import pytest
    from harness.config import MemoryLibraryConfig
    from harness.memory_library import MemoryLibrary
    from harness.tools import ToolError

    root = tmp_path / "lib"
    for rel, text in {
        "00-index.md": "# Index\n- categories/health/capsules/secret.md\n",
        "categories/work/memory.md": "# Work\n### 2026-09-01\n- Prefers Python\n",
        "categories/project-ideas/capsules/harness.md": "# Harness\nQwen default\n",
        "categories/health/memory.md": "# Health\nPrefers Python secretly\n",
        "memory-capsules/cross.md": "# Cross\nPython\n",
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (root / ".git").mkdir()
    lib = MemoryLibrary(MemoryLibraryConfig(enabled=True, clone_dir=str(root), categories=["work", "project-ideas"]))
    lib._refreshed = 1e18  # skip git

    index = lib.memory_index()
    assert "categories/work/memory.md — Work" in index and "health" not in index and "Index" not in index
    assert lib.memory_search("python").splitlines() == ["categories/work/memory.md:3: - Prefers Python"]
    assert "Qwen default" in lib.memory_read("categories/project-ideas/capsules/harness.md")
    for bad in ("categories/health/memory.md", "00-index.md", "memory-capsules/cross.md",
                "categories/work/../health/memory.md", "../lib/categories/health/memory.md"):
        with pytest.raises(ToolError):
            lib.memory_read(bad)


def test_memory_tools_reach_sessions_and_rebuild_asks(tmp_path):
    from harness.config import MemoryLibraryConfig
    from harness.policy import ASK, Policy

    assert Policy().decide("rebuild_service", {"service": "plex-webhook"}).action == ASK
    root = tmp_path / "lib" / "categories" / "work"
    root.mkdir(parents=True)
    (root / "memory.md").write_text("# Work\nThe user's editor is Helix.\n")
    (tmp_path / "lib" / ".git").mkdir()
    cfg = make_cfg(tmp_path)
    cfg.memory_library = MemoryLibraryConfig(enabled=True, clone_dir=str(tmp_path / "lib"), categories=["work"],
                                             refresh_minutes=1e9)

    async def body():
        script = Script([Completion(tool_calls=[call("memory_search", 0, pattern="editor")]),
                         Completion(content="Helix")])
        m = Manager(cfg, chat=script)
        m.runner.memory._refreshed = 1e18
        await m.start(maintenance=False)
        s = m.create("which editor?")
        await wait_status(m, s["id"], "done")
        result = events(m, s["id"], "tool_result")[0]
        assert result["ok"] and "Helix" in result["output"]
        assert "memory_index" in m.db.get_session(s["id"])["context"][0]["content"]
        await m.stop()
    asyncio.run(body())
