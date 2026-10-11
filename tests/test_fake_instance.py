"""The disposable fake-model instance (#588, docs/ui-testing.md): start, readiness, the handle and teardown."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from harness import fake_instance, fake_model
from waits import scaled


def run(coro):
    return asyncio.run(coro)


def test_stop_is_safe_to_repeat_and_only_deletes_instance_dirs(tmp_path):
    assert fake_instance.stop(tmp_path / "absent") == 0
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    (stranger / "notes.txt").write_text("keep me", encoding="utf-8")
    assert fake_instance.stop(stranger) == 2 and (stranger / "notes.txt").exists()
    instance = tmp_path / "instance"
    fake_instance.write_instance(instance, 0)
    frozen = instance / "data" / "checkpoints" / "object"   # git objects are read-only on Windows
    frozen.parent.mkdir(parents=True)
    frozen.write_text("x", encoding="utf-8")
    frozen.chmod(0o444)
    # A handle whose process is gone (or isn't an instance) is not killed; the directory still goes.
    fake_instance.write_handle(instance, {"pid": 0})
    assert fake_instance.stop(instance) == 0 and not instance.exists()
    assert fake_instance.stop(instance) == 0
    lookalike = tmp_path / "lookalike"   # an instance.json alone is no licence to delete a directory
    lookalike.mkdir()
    (lookalike / fake_instance.HANDLE).write_text("{}", encoding="utf-8")
    (lookalike / "notes.txt").write_text("keep me", encoding="utf-8")
    assert fake_instance.stop(lookalike) == 2 and (lookalike / "notes.txt").exists()
    partial = tmp_path / "partial"
    partial.mkdir()   # a start that failed before writing anything
    assert fake_instance.stop(partial) == 0 and not partial.exists()


def test_start_refuses_an_occupied_port_or_a_used_dir(tmp_path, capsys):
    import socket
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        assert fake_instance.available_port(port) is None and fake_instance.available_port(0)
        with pytest.raises(SystemExit):
            fake_instance.main(["--port", str(port), "--dir", str(tmp_path / "new")])
        assert "is in use" in capsys.readouterr().err and not (tmp_path / "new").exists()
    used = tmp_path / "used"
    used.mkdir()
    (used / "x").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        fake_instance.main(["--dir", str(used)])
    assert "not empty" in capsys.readouterr().err


def test_serve_seeds_settles_and_shuts_down(monkeypatch, tmp_path):
    """The instance end to end in this process: READY only once the seeded runs settled, then a clean stop."""
    import argparse
    import httpx
    monkeypatch.setenv(fake_model.ENV, "")  # serve sets it for its own process; restored after the test
    root = tmp_path / "instance"
    root.mkdir()
    args = argparse.Namespace(port=0, web_port=0, fixtures=fake_model.DEFAULT_FIXTURES, no_seed=False, keep=False)

    async def body():
        task = asyncio.create_task(fake_instance.serve(args, root))
        deadline = time.monotonic() + scaled(120)
        state = {}
        while state.get("status") != "ready":
            assert time.monotonic() < deadline and not task.done(), "the instance never got ready"
            await asyncio.sleep(0.2)
            try:
                state = json.loads((root / fake_instance.HANDLE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        async with httpx.AsyncClient() as client:
            assert (await client.get(state["web"] + "/ready")).json()["key_id"] == state["key_id"]
            assert "harness.ownerToken" in (await client.get(state["connect"])).text
            assert (await client.get(state["server"] + "/health")).status_code == 200
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return state
    state = run(body())
    assert state["pid"] and "token" not in json.dumps(state).lower()
    assert fake_instance.remove(root) and not root.exists()


@pytest.mark.parametrize("outcome,code", [(fake_instance.StartError("seeding failed"), 1), (KeyboardInterrupt(), 0)])
def test_start_cleans_up_after_a_failure_or_ctrl_c(monkeypatch, tmp_path, capsys, outcome, code):
    import signal
    handlers = []
    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.append(sig))
    seen = {}

    async def serve(args, root):
        seen["root"] = root
        fake_instance.write_instance(root, 0)
        raise outcome
    monkeypatch.setattr(fake_instance, "serve", serve)
    assert fake_instance.main(["--dir", str(tmp_path / "instance")]) == code
    assert seen["root"] == (tmp_path / "instance").resolve() and not seen["root"].exists()
    assert handlers  # Ctrl+Break / SIGTERM reach the cleanup as KeyboardInterrupt
    assert ("FAILED seeding failed" in capsys.readouterr().err) == (code == 1)


def test_stop_ends_the_recorded_instance_process(tmp_path):
    import subprocess
    import sys
    instance = tmp_path / "instance"
    fake_instance.write_instance(instance, 0)
    # Stands in for a running instance: its command line names harness.fake_instance.
    import psutil
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "harness.fake_instance"])
    try:
        created = psutil.Process(proc.pid).create_time()
        # The same pid with another start time is a reused pid (another instance, say): never killed.
        fake_instance.write_handle(instance, {"pid": proc.pid, "pid_created": created - 60})
        assert fake_instance._instance_process({"pid": proc.pid, "pid_created": created - 60}) is None
        fake_instance.write_handle(instance, {"pid": proc.pid, "pid_created": created})
        assert fake_instance.stop(instance) == 0
        assert proc.wait(timeout=30) is not None and not instance.exists()
    finally:
        proc.kill()


def test_kill_survives_a_child_that_already_exited(monkeypatch):
    import psutil
    killed = []

    class Proc:
        def __init__(self, pid, gone=False, kids=()):
            self.pid, self.gone, self.kids = pid, gone, list(kids)

        def children(self, recursive=False):
            return self.kids

        def kill(self):
            if self.gone:
                raise psutil.NoSuchProcess(self.pid)
            killed.append(self.pid)
    parent = Proc(1, kids=[Proc(2, gone=True), Proc(3)])
    monkeypatch.setattr(psutil, "wait_procs", lambda procs, timeout: (procs, []))
    assert fake_instance._kill(parent) and killed == [3, 1]
    monkeypatch.setattr(psutil, "wait_procs", lambda procs, timeout: ([], procs))
    assert not fake_instance._kill(Proc(4))   # still alive: stop leaves the directory in place

    class Stubborn(Proc):
        def kill(self):
            raise psutil.AccessDenied(self.pid)
    child = Stubborn(6)
    # The instance exits but a child it can't kill lives on: that is not a clean stop either.
    monkeypatch.setattr(psutil, "wait_procs", lambda procs, timeout: ([p for p in procs if p is not child],
                                                                      [p for p in procs if p is child]))
    assert not fake_instance._kill(Proc(5, kids=[child]))


def test_stop_leaves_an_unrelated_process_alone(tmp_path):
    import subprocess
    import sys
    instance = tmp_path / "instance"
    fake_instance.write_instance(instance, 0)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        import psutil
        created = psutil.Process(proc.pid).create_time()
        fake_instance.write_handle(instance, {"pid": proc.pid, "pid_created": created})  # not an instance
        assert fake_instance.stop(instance) == 0 and proc.poll() is None and not instance.exists()
    finally:
        proc.kill()


@pytest.mark.parametrize("stage", ["lookup", "create_time", "cmdline"])
@pytest.mark.parametrize("error_name", ["AccessDenied", "Error"])
def test_stop_preserves_unverifiable_process(monkeypatch, tmp_path, capsys, stage, error_name):
    import psutil
    from unittest.mock import Mock
    instance = tmp_path / "instance"
    fake_instance.write_instance(instance, 0)
    fake_instance.write_handle(instance, {"pid": 123, "pid_created": 42})
    before = {p.relative_to(instance): p.read_bytes() for p in instance.rglob("*") if p.is_file()}
    error = getattr(psutil, error_name)(123)
    proc = Mock(pid=123)
    proc.create_time.return_value = 42
    proc.cmdline.return_value = ["python", "-m", "harness.fake_instance"]
    lookup = Mock(return_value=proc)
    (lookup if stage == "lookup" else getattr(proc, stage)).side_effect = error
    monkeypatch.setattr(psutil, "Process", lookup)
    kill, remove = Mock(), Mock()
    monkeypatch.setattr(fake_instance, "_kill", kill)
    monkeypatch.setattr(fake_instance, "remove", remove)
    assert fake_instance.stop(instance) == 1
    kill.assert_not_called()
    remove.assert_not_called()
    assert before == {p.relative_to(instance): p.read_bytes() for p in instance.rglob("*") if p.is_file()}
    assert "could not verify" in capsys.readouterr().err


@pytest.mark.parametrize("stage", ["lookup", "create_time", "cmdline", "reused", "unrelated"])
def test_stop_cleans_up_a_definitively_gone_or_unrelated_process(monkeypatch, tmp_path, stage):
    import psutil
    from unittest.mock import Mock
    instance = tmp_path / "instance"
    fake_instance.write_instance(instance, 0)
    fake_instance.write_handle(instance, {"pid": 123, "pid_created": 42})
    proc = Mock(pid=123)
    proc.create_time.return_value = 43 if stage == "reused" else 42
    proc.cmdline.return_value = ["unrelated"]
    lookup = Mock(return_value=proc)
    if stage in ("lookup", "create_time", "cmdline"):
        (lookup if stage == "lookup" else getattr(proc, stage)).side_effect = psutil.NoSuchProcess(123)
    monkeypatch.setattr(psutil, "Process", lookup)
    kill = Mock()
    monkeypatch.setattr(fake_instance, "_kill", kill)
    assert fake_instance.stop(instance) == 0 and not instance.exists()
    kill.assert_not_called()


@pytest.mark.parametrize("failure", ["alive", "inspection"])
def test_stop_preserves_directory_when_shutdown_fails(monkeypatch, tmp_path, failure):
    import psutil
    from unittest.mock import Mock
    instance = tmp_path / "instance"
    fake_instance.write_instance(instance, 0)
    fake_instance.write_handle(instance, {"pid": 123, "pid_created": 42})
    monkeypatch.setattr(fake_instance, "_instance_process", lambda handle: Mock(pid=123))
    kill = Mock(return_value=False, side_effect=psutil.AccessDenied(123) if failure == "inspection" else None)
    monkeypatch.setattr(fake_instance, "_kill", kill)
    remove = Mock()
    monkeypatch.setattr(fake_instance, "remove", remove)
    assert fake_instance.stop(instance) == 1
    remove.assert_not_called()
    assert (instance / fake_instance.HANDLE).is_file()


def test_main_stop_subcommand(tmp_path):
    assert fake_instance.main(["stop", "--dir", str(tmp_path / "absent")]) == 0


def test_ready_answers_503_until_seeded():
    from starlette.testclient import TestClient as Plain
    state = {"status": "starting"}
    client = Plain(fake_instance.web_app("http://127.0.0.1:1", "ho-x", state))
    assert client.get("/ready").status_code == 503
    state["status"] = "ready"
    assert client.get("/ready").json() == {"status": "ready"}
    assert client.get("/connect").status_code == 200 and client.get("/").status_code == 200
    assert client.get("/static/icon-192.png").status_code == 200


def test_connect_page_signs_the_web_copy_in():
    page = fake_instance.connect_page("http://127.0.0.1:1", "ho-<x>")
    assert '"harness.daemonUrl": "http://127.0.0.1:1"' in page and '"harness.ownerToken": "ho-<x>"' in page
