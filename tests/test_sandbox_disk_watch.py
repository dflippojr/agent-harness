"""Issue #525: the disk watchdog stops a sandbox command while it runs once it writes past the workspace quota or eats
into the data drive's free-space floor, and leaves commands under the quota alone."""

from __future__ import annotations

import asyncio
import time
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

    def __init__(self, workspace: Path, chunks: int = 0, pace: float = 0.01, fail: tuple = ()):
        self.workspace = workspace
        self.chunks = chunks
        self.pace = pace
        self.fail = fail        # docker verbs that fail without stopping anything
        self.state = "running"  # what inspect reports
        self.calls: list[str] = []
        self.restarted = asyncio.Event()
        self.written = 0
        self.other = 0          # bytes something else used on the drive

    async def __call__(self, args, timeout=60, input_=None, env=None):
        verb = args[1]
        self.calls.append(verb)
        if verb in self.fail:
            return 1, "", f"{verb} failed"
        if verb == "inspect":
            return 0, f"{self.state} owner", ""
        if verb in ("restart", "kill"):
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
    assert "past its 10 MB quota" in str(err.value) and "background processes are gone" in str(err.value)
    assert "restart" in fake.calls
    assert fake.written < 20                   # stopped while it ran, not after all 200 MB
    assert events and events[0][0] == "sandbox_disk_limit" and events[0][1]["stopped"]


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


def test_member_growth_is_capped_by_the_account_quota(monkeypatch, tmp_path):
    def usage() -> int:
        return 95 * MB + sum(p.stat().st_size for p in tmp_path.iterdir())
    box, fake, _ = make(monkeypatch, tmp_path, sandbox.DiskLimits(500 * MB, 100 * MB, 100 * MB, usage), chunks=200)
    with pytest.raises(sandbox.DiskLimitExceeded, match="past its 100 MB disk quota"):
        asyncio.run(box.exec("make huge"))
    assert fake.written < 15


def test_concurrent_member_commands_share_the_account_quota(monkeypatch, tmp_path):
    # Two sessions of one member start with 100 MB of account quota left and each writes 150 MB. A fixed budget per
    # command would let each write 100 MB; measuring the account while they run stops them near the quota.
    roots = [tmp_path / "a", tmp_path / "b"]
    fakes = {}
    for name, root in zip("ab", roots):
        root.mkdir()
        fakes[f"harness-{name}"] = FakeDocker(root, chunks=150, pace=0.02)

    async def docker(args, timeout=60, input_=None, env=None):
        return await next(f for n, f in fakes.items() if n in args)(args, timeout, input_, env)

    def used() -> int:
        return sum(p.stat().st_size for r in roots for p in r.iterdir())
    monkeypatch.setattr(sandbox, "run_cmd", docker)
    monkeypatch.setattr(sandbox.DiskWatch, "_free", lambda self: TOTAL - used())
    limits = sandbox.DiskLimits(500 * MB, 100 * MB, 100 * MB, used)

    async def disk_limits():
        return limits

    async def both():
        boxes = [sandbox.Sandbox(n, r, SandboxConfig(), disk_limits=disk_limits) for n, r in zip("ab", roots)]
        return await asyncio.gather(*(b.exec("make") for b in boxes), return_exceptions=True)
    results = asyncio.run(both())
    assert any(isinstance(r, sandbox.DiskLimitExceeded) for r in results)
    assert used() < 140 * MB   # a fixed 100 MB budget per command would allow 200 MB


def test_floor_starts_below_free_space_when_the_drive_is_already_low(tmp_path, monkeypatch):
    # Already under the minimum: a cleanup command still runs, and the watchdog only allows a little more use.
    watch = sandbox.DiskWatch(tmp_path, sandbox.DiskLimits(10 * MB, 900 * MB))
    monkeypatch.setattr(sandbox.DiskWatch, "_free", lambda self: 500 * MB)
    watch.start()
    assert watch.floor == 500 * MB - sandbox.FREE_SLACK_BYTES
    assert watch._floor_reason(500 * MB) == ""


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

    monkeypatch.setattr(storage, "account_usage_bytes", lambda cfg, uid: 70 * MB if uid == "u1" else 0)
    sessions = {"own": {"id": "own", "owner_id": None}, "mem": {"id": "mem", "owner_id": "u1"},
                "gone": {"id": "gone", "owner_id": "u2"}}
    accounts = {"u1": {"disk_quota_bytes": 100 * MB}}
    fake = SimpleNamespace(
        db=SimpleNamespace(get_session=sessions.get, account_by_id=accounts.get),
        cfg=SimpleNamespace(cleanup=SimpleNamespace(min_free_gb=2)), quota_mb=lambda s: 300)
    own = asyncio.run(runner.Runner._disk_limits(fake, "own"))
    assert own == sandbox.DiskLimits(300 * MB, 2 * 2**30)
    member = asyncio.run(runner.Runner._disk_limits(fake, "mem"))
    assert member.account_quota_bytes == 100 * MB and member.account_usage() == 70 * MB   # measured live
    assert asyncio.run(runner.Runner._disk_limits(fake, "gone")).account_quota_bytes == 0  # no account: no growth


def test_watchdog_error_stops_the_command_in_the_container(monkeypatch, tmp_path):
    # Cancelling the docker exec client alone would leave the writer running in the container with nothing watching.
    box, fake, events = make(monkeypatch, tmp_path, sandbox.DiskLimits(500 * MB, 100 * MB), chunks=200)

    free = sandbox.DiskWatch._free
    calls = []

    def broken(self):
        calls.append(1)
        if len(calls) > 2:     # measures the start, then fails mid-run
            raise OSError("drive gone")
        return free(self)
    monkeypatch.setattr(sandbox.DiskWatch, "_free", broken)
    with pytest.raises(sandbox.DiskLimitExceeded, match=r"disk watchdog failed \(OSError: drive gone\)"):
        asyncio.run(box.exec("make huge"))
    assert "restart" in fake.calls and fake.written < 20 and events[0][1]["stopped"]


def test_failed_restart_falls_back_to_kill(monkeypatch, tmp_path):
    box, fake, events = make(monkeypatch, tmp_path, sandbox.DiskLimits(10 * MB, 100 * MB), chunks=200,
                             fail=("restart",))
    with pytest.raises(sandbox.DiskLimitExceeded, match="was stopped because"):
        asyncio.run(box.exec("make huge"))
    assert fake.calls[-2:] == ["restart", "kill"] and fake.written < 20 and events[0][1]["stopped"]


def test_container_that_cannot_be_stopped_is_not_reported_stopped(monkeypatch, tmp_path):
    box, fake, events = make(monkeypatch, tmp_path, sandbox.DiskLimits(10 * MB, 100 * MB), chunks=40,
                             fail=("restart", "kill"))
    with pytest.raises(sandbox.DiskLimitExceeded,
                       match=r"could not be stopped \(restart failed; kill failed\)") as err:
        asyncio.run(box.exec("make huge"))
    assert "may still be running" in str(err.value) and "background processes are gone" not in str(err.value)
    assert events[0][1]["stopped"] is False


def test_container_already_stopped_counts_as_stopped(monkeypatch, tmp_path):
    box, fake, events = make(monkeypatch, tmp_path, sandbox.DiskLimits(10 * MB, 100 * MB), chunks=40,
                             fail=("restart", "kill"))
    docker = sandbox.run_cmd

    async def dies_before_the_kill(args, **kw):
        if args[1] == "kill":
            fake.state = "exited"
        return await docker(args, **kw)
    monkeypatch.setattr(sandbox, "run_cmd", dies_before_the_kill)
    with pytest.raises(sandbox.DiskLimitExceeded, match="was stopped because"):
        asyncio.run(box.exec("make huge"))
    assert events[0][1]["stopped"]


def test_free_space_is_polled_while_a_slow_scan_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "MIN_SCAN_SECONDS", 0.05)    # a scan starts early in the run
    box, fake, _ = make(monkeypatch, tmp_path, sandbox.DiskLimits(900 * MB, 950 * MB), chunks=200)
    measure = sandbox.dir_size
    scans = []

    def slow(root):
        scans.append(root)
        if len(scans) > 1:      # every scan after the starting one takes longer than the whole command would
            time.sleep(1.5)
        return measure(root)
    monkeypatch.setattr(sandbox, "dir_size", slow)
    with pytest.raises(sandbox.DiskLimitExceeded, match="data drive"):
        asyncio.run(box.exec("make huge"))
    assert len(scans) > 1 and fake.written < 80   # the floor stopped it while the scan was still running


def test_limit_seen_as_the_command_ends_still_stops_the_container(monkeypatch, tmp_path):
    # The shell exits while a background writer crosses the quota: both tasks finish before the wait resumes.
    box, fake, events = make(monkeypatch, tmp_path, sandbox.DiskLimits(10 * MB, 100 * MB), chunks=0)

    async def at_once(self):
        return "the workspace grew to 11 MB, past its 10 MB quota"
    monkeypatch.setattr(sandbox.DiskWatch, "run", at_once)
    with pytest.raises(sandbox.DiskLimitExceeded, match="past its 10 MB quota"):
        asyncio.run(box.exec("make & exit"))
    assert "restart" in fake.calls and events[0][0] == "sandbox_disk_limit"
