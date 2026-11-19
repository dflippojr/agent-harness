"""A session's `run` field is never lost to a write that read `run` earlier (#991, stage 1).

`run` is one JSON column of about 34 keys. Writing one key means writing the whole value, so a write that builds the
new value from a copy read on a pooled reader, and commits it later in a writer job of its own, overwrites whatever
another writer job committed in between. `Database.patch_run` merges the fields into the row as it stands inside one
writer transaction, so nothing written meanwhile is lost.

Each case below runs one of the runner's `run` writes while `Interleave` queues a writer job at the worst moment: right
after the code's first read of the session outside a transaction (the copy a separate write would build on), or right
before its first write if it never reads first. The job sets a field of its own; the case checks that the field and the
code's own fields are both there afterwards. Synthetic sessions only.
"""
from __future__ import annotations

import asyncio

import pytest

from harness import telemetry
from harness.config import BackendConfig, Project
from harness.manager import Manager
from test_daemon import make_cfg

INTERLEAVED = "interleaved_by_another_writer"


class Interleave:
    """While active, queue one writer job that sets `run[INTERLEAVED]` for session `sid`, between the code's read of
    the session on a pooled reader and its write (or before its first write when it does not read first)."""

    def __init__(self, db, sid: str):
        self.db, self.sid, self.fired = db, sid, False
        self.names = [name for name in ("get_session", "update_session", "patch_run") if hasattr(db, name)]

    def __enter__(self):
        self.real = {name: getattr(self.db, name) for name in self.names}
        self.db.get_session = self._get_session
        self.db.update_session = self._update_session
        if "patch_run" in self.real:
            self.db.patch_run = self._patch_run
        return self

    def __exit__(self, *exc):
        for name in self.names:
            self.db.__dict__.pop(name, None)

    def _outside(self, sid: str) -> bool:
        return sid == self.sid and not self.fired and not self.db.in_transaction()

    def _fire(self) -> None:
        self.fired = True
        get, update = self.real["get_session"], self.real["update_session"]

        def another_writer() -> None:  # a read and a write in one transaction: it loses nothing itself
            run = dict(get(self.sid)["run"])
            run[INTERLEAVED] = True
            update(self.sid, run=run)
        self.db.for_session(self.sid).write(another_writer)

    def _get_session(self, sid, *args, **kwargs):
        row = self.real["get_session"](sid, *args, **kwargs)
        if self._outside(sid):
            self._fire()
        return row

    def _update_session(self, sid, **fields):
        if "run" in fields and self._outside(sid):
            self._fire()
        return self.real["update_session"](sid, **fields)

    def _patch_run(self, sid, *args, **fields):
        if self._outside(sid):
            self._fire()
        return self.real["patch_run"](sid, *args, **fields)


@pytest.fixture
def manager(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5")
    cfg.projects["repo"] = Project(name="repo", repo="https://example.invalid/owner/repo.git")
    m = Manager(cfg)
    m._spawn = lambda *_args, **_kwargs: None  # no run starts: each case drives one write itself
    yield m
    m.db.close()


def _run(manager, sid: str) -> dict:
    return manager.db.get_session(sid)["run"]


def _check(manager, sid: str, interleave: Interleave, **expected) -> None:
    assert interleave.fired, "the code under test never read or wrote the session"
    run = _run(manager, sid)
    assert run.get(INTERLEAVED) is True, f"the field another writer set meanwhile was lost: {sorted(run)}"
    for key, value in expected.items():
        assert run.get(key) == value, (key, run.get(key))


def test_record_failure_keeps_a_field_written_meanwhile(manager):
    sid = manager.create("fail")["id"]
    with Interleave(manager.db, sid) as interleave:
        manager.runner._record_failure(sid, "provider_error", "boom", True, "provider_error: boom")
    failure = {"code": "provider_error", "provider": "local", "message": "boom", "retryable": True}
    _check(manager, sid, interleave, failure=failure)


def test_session_trace_keeps_a_field_written_meanwhile(manager, monkeypatch):
    class Tracing:
        enabled = True

        @staticmethod
        def new_trace() -> dict:
            return {"trace_id": "t-1", "span_id": "s-1"}
    sid = manager.create("trace")["id"]
    monkeypatch.setattr(telemetry, "_tracer", Tracing())  # on after the create, so the session has no trace yet
    with Interleave(manager.db, sid) as interleave:
        trace = manager.runner._session_trace(sid)
    assert trace["trace_id"] == "t-1"
    _check(manager, sid, interleave, trace=trace)


def test_trace_run_stopped_keeps_a_field_written_meanwhile(manager):
    class Root:
        def set(self, attrs):
            self.attrs = attrs
    sid = manager.create("trace")["id"]
    with Interleave(manager.db, sid) as interleave:
        manager.runner._trace_run_stopped(sid, {"trace_id": "t-1"}, Root())
    assert _run(manager, sid)["trace"]["last_stop"] > 0
    _check(manager, sid, interleave)


def test_workspace_context_keeps_a_field_written_meanwhile(manager):
    sid = manager.create("context")["id"]
    with Interleave(manager.db, sid) as interleave:
        manager.runner._workspace_context_at_start(sid)
    _check(manager, sid, interleave, workspace_context_applied=True)


def test_origin_refresh_keeps_a_field_written_meanwhile(manager):
    sid = manager.create("refresh")["id"]
    manager.db.update_session(sid, project="repo", base_commit="abc123")

    async def refreshed(*_args):
        return ""
    manager.runner._refresh_origin = refreshed
    s = manager.db.get_session(sid)
    with Interleave(manager.db, sid) as interleave:
        asyncio.run(manager.runner._setup_repo(s))
    _check(manager, sid, interleave, origin_refreshed=True)


def test_hosted_start_keeps_a_field_written_meanwhile(manager):
    sid = manager.create("hosted", backend="claude")["id"]

    class FakeCli:
        def __init__(self, **kwargs):
            self.backend_session_id = kwargs.get("backend_session_id", "")

        async def start(self):
            pass

        async def initialize(self, _prompt):
            self.backend_session_id = "claude-session-1"

    manager.runner.cli_factory = FakeCli
    credential = {"policy": "subscription", "key": "", "source": "user_file", "assignment_id": "", "path": "",
                  "marker": None}
    with Interleave(manager.db, sid) as interleave:
        asyncio.run(manager.runner._start_cli(sid, "claude", manager.cfg.backends["claude"], credential, False, "",
                                              "", False))
    _check(manager, sid, interleave, billing_mode="subscription", credential_source="subscription",
           backend_session_id="claude-session-1")


def test_split_mode_file_write_keeps_a_field_written_meanwhile(manager):
    manager.cfg.backends["claude"].tool_mode = "split"
    sid = manager.create("split", backend="claude")["id"]
    s = manager.db.get_session(sid)
    with Interleave(manager.db, sid) as interleave:
        asyncio.run(manager.runner._split_call(s, "write_file", {"path": "a.txt", "content": "hi"}))
    _check(manager, sid, interleave, files_touched=["a.txt"])
