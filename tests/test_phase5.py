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
