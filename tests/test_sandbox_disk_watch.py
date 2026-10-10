"""Issue #525: the disk watchdog stops a sandbox command while it runs once it writes past the workspace quota or eats
into the data drive's free-space floor, and leaves commands under the quota alone."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harness import sandbox
from harness.config import SandboxConfig
from harness.fileops import ToolError

MB = 2**20
TOTAL = 1000 * MB   # the simulated data drive


class FakeDocker:
    """A running container whose exec writes `chunks` 1 MB files into the workspace, one every `pace` seconds, until
    it finishes or the container is restarted."""

    def __init__(self, workspace: Path, chunks: int = 0, pace: float = 0.01):
        self.workspace = workspace
        self.chunks = chunks
        self.pace = pace
        self.calls: list[str] = []
        self.restarted = asyncio.Event()
        self.written = 0
        self.other = 0          # bytes something else used on the drive

    async def __call__(self, args, timeout=60, input_=None, env=None):
        verb = args[1]
        self.calls.append(verb)
        if verb == "inspect":
            return 0, "running owner", ""
        if verb == "restart":
            self.restarted.set()
        elif verb == "exec":
            for _ in range(self.chunks):
                if self.restarted.is_set():
                    return 137, "", ""
                (self.workspace / f"blob{self.written}").write_bytes(b"\0" * MB)
                self.written += 1
                await asyncio.sleep(self.pace)
            return 0, "built", ""
        return 0, "", ""

    def free(self) -> int:
        used = sum(p.stat().st_size for p in self.workspace.iterdir())
        return TOTAL - used - self.other


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(sandbox, "FREE_POLL_SECONDS", 0.005)
    monkeypatch.setattr(sandbox, "MIN_SCAN_SECONDS", 0.5)   # long enough that the free-space bound has to trigger scans


def make(monkeypatch, tmp_path, limits, **kw):
    fake = FakeDocker(tmp_path, **kw)
    monkeypatch.setattr(sandbox, "run_cmd", fake)
    monkeypatch.setattr(sandbox.DiskWatch, "_free", lambda self: fake.free())
    events = []

    async def disk_limits():
        return limits
    box = sandbox.Sandbox("s1", tmp_path, SandboxConfig(), disk_limits=disk_limits,
                          on_event=lambda t, d: events.append((t, d)))
    return box, fake, events


def test_command_writing_past_quota_is_stopped_during_the_run(monkeypatch, tmp_path):
    box, fake, events = make(monkeypatch, tmp_path, sandbox.DiskLimits(10 * MB, 100 * MB), chunks=200)
    with pytest.raises(sandbox.DiskLimitExceeded) as err:
        asyncio.run(box.exec("make huge"))
    assert isinstance(err.value, ToolError)    # reaches the model as a tool error
    assert "past its 10 MB quota" in str(err.value) and "restarted" in str(err.value)
    assert "restart" in fake.calls
    assert fake.written < 20                   # stopped while it ran, not after all 200 MB
    assert events and events[0][0] == "sandbox_disk_limit"


def test_free_space_floor_is_kept(monkeypatch, tmp_path):
    # The quota alone would allow 900 MB, but the drive must keep 950 MB free.
    box, fake, _ = make(monkeypatch, tmp_path, sandbox.DiskLimits(900 * MB, 950 * MB), chunks=200)
    with pytest.raises(sandbox.DiskLimitExceeded, match="data drive"):
        asyncio.run(box.exec("make huge"))
    assert fake.written < 60
    assert fake.free() > 930 * MB              # stopped within a couple of polls of the floor


def test_command_under_quota_is_unaffected(monkeypatch, tmp_path):
    box, fake, events = make(monkeypatch, tmp_path, sandbox.DiskLimits(50 * MB, 100 * MB), chunks=20)
    assert asyncio.run(box.exec("make")) == (0, "built")
    assert "restart" not in fake.calls and not events and fake.written == 20


def test_no_limits_runs_unwatched(monkeypatch, tmp_path):
    box, fake, _ = make(monkeypatch, tmp_path, None, chunks=5)
    assert asyncio.run(box.exec("make")) == (0, "built")


def test_workspace_already_over_quota_may_shrink_but_not_grow(monkeypatch, tmp_path):
    for i in range(30):
        (tmp_path / f"old{i}").write_bytes(b"\0" * MB)
    box, fake, _ = make(monkeypatch, tmp_path, sandbox.DiskLimits(10 * MB, 100 * MB))
    assert asyncio.run(box.exec("rm -rf build"))[0] == 0     # no growth: runs
    fake.chunks = 200
    with pytest.raises(sandbox.DiskLimitExceeded):
        asyncio.run(box.exec("make huge"))
    assert fake.written < 10


def test_member_growth_is_capped_by_the_remaining_account_quota(monkeypatch, tmp_path):
    box, fake, _ = make(monkeypatch, tmp_path, sandbox.DiskLimits(500 * MB, 100 * MB, growth_bytes=5 * MB),
                        chunks=200)
    with pytest.raises(sandbox.DiskLimitExceeded, match="account's remaining disk quota"):
        asyncio.run(box.exec("make huge"))
    assert fake.written < 15


def test_floor_starts_below_free_space_when_the_drive_is_already_low(tmp_path, monkeypatch):
    # Already under the minimum: a cleanup command still runs, and the watchdog only allows a little more use.
    watch = sandbox.DiskWatch(tmp_path, sandbox.DiskLimits(10 * MB, 900 * MB))
    monkeypatch.setattr(sandbox.DiskWatch, "_free", lambda self: 500 * MB)
    watch.start()
    assert watch.floor == 500 * MB - sandbox.FREE_SLACK_BYTES
    assert watch.check() == ""


def test_failed_setup_from_the_watchdog_is_reported_not_raised(monkeypatch, tmp_path):
    box, fake, events = make(monkeypatch, tmp_path, sandbox.DiskLimits(5 * MB, 100 * MB), chunks=200)
    box.setup = "npm install"
    assert asyncio.run(box._run_setup()).startswith("failed (the command was stopped")
    assert [t for t, _ in events].count("sandbox_setup") == 1


def test_runner_limits_and_unthrottled_quota_check(monkeypatch):
    from types import SimpleNamespace

    from harness import runner, storage
    emitted = []
    fake = SimpleNamespace(_quota_checked={"s1": 1.0}, bus=SimpleNamespace(emit=lambda *a: emitted.append(a)))
    runner.Runner._sandbox_event(fake, "s1", "sandbox_disk_limit", {"reason": "x"})
    assert "s1" not in fake._quota_checked and emitted == [("s1", "sandbox_disk_limit", {"reason": "x"})]

    monkeypatch.setattr(storage, "account_usage_bytes", lambda cfg, uid: 70 * MB)
    sessions = {"own": {"id": "own"}, "mem": {"id": "mem", "user_id": "u1"}}
    monkeypatch.setattr(runner, "session_user_id", lambda s: s.get("user_id", runner.OWNER_USER_ID))
    fake = SimpleNamespace(
        db=SimpleNamespace(get_session=sessions.get, account_by_id=lambda uid: {"disk_quota_bytes": 100 * MB}),
        cfg=SimpleNamespace(cleanup=SimpleNamespace(min_free_gb=2)), quota_mb=lambda s: 300)
    fake._member_clone_budget = lambda uid: runner.Runner._member_clone_budget(fake, uid)
    own = asyncio.run(runner.Runner._disk_limits(fake, "own"))
    assert own == sandbox.DiskLimits(300 * MB, 2 * 2**30, None)
    member = asyncio.run(runner.Runner._disk_limits(fake, "mem"))
    assert member.growth_bytes == 30 * MB


def test_watchdog_error_is_raised_and_stops_the_command(monkeypatch, tmp_path):
    box, fake, _ = make(monkeypatch, tmp_path, sandbox.DiskLimits(500 * MB, 100 * MB), chunks=200)

    def broken(self):
        raise OSError("drive gone")
    monkeypatch.setattr(sandbox.DiskWatch, "check", broken)
    with pytest.raises(OSError, match="drive gone"):
        asyncio.run(box.exec("make huge"))
    assert "restart" not in fake.calls and fake.written < 20
