"""A session's `run` field is never lost to a write that read `run` earlier (#991, stage 1).

`run` is one JSON column of about 34 keys. Writing one key means writing the whole value, so a write that builds the
new value from a copy read on a pooled reader, and commits it later in a writer job of its own, overwrites whatever
another writer job committed in between. `SessionStore.patch_run` merges the fields into the row as it stands inside one
writer transaction, so nothing written meanwhile is lost.

Each case below runs one of the runner's `run` writes while `Interleave` queues a writer job at the worst moment: right
after the code's first read of the session outside a transaction (the copy a separate write would build on), or right
before its first write if it never reads first. The job sets a field of its own; the case checks that the field and the
code's own fields are both there afterwards. Synthetic sessions only.

`Interleave` fires before `patch_run` is entered, so the cases for `patch_run` and `unset_run` themselves hook their own
read of the session instead (`AfterTheRead`): another writer commits a field as soon as the database lets it, which is
only after the write when the read and the write are one transaction.
"""
from __future__ import annotations

import ast
import asyncio
import sqlite3
import time
from operator import methodcaller
from pathlib import Path

import pytest

from harness import telemetry
from harness.app_stores import SessionStores
from harness.backends.events import RateLimits, Result, Session, ToolResult
from harness.config import BackendConfig, Project
from harness.db import Database
from harness.manager import Manager
from test_daemon import make_cfg

INTERLEAVED = "interleaved_by_another_writer"


class Interleave:
    """While active, queue one writer job that sets `run[INTERLEAVED]` for session `sid`, between the code's read of
    the session on a pooled reader and its write; or, when it does not read first, before its first write or the
    first transaction it starts on the session's store."""

    SESSION_NAMES = ("get_session", "update_session", "patch_run")  # on the session proxy or store (#992 stage 6)

    def __init__(self, db, sid: str):
        self.db, self.sid, self.fired = db, sid, False

    def __enter__(self):
        sessions = self.db.sessions
        self.real = {name: getattr(sessions, name) for name in self.SESSION_NAMES}
        self.real["for_session"] = self.db.for_session
        sessions.get_session = self._get_session
        sessions.update_session = self._update_session
        sessions.patch_run = self._patch_run
        self.db.for_session = self._for_session
        return self

    def __exit__(self, *exc):
        for name in self.SESSION_NAMES:
            self.db.sessions.__dict__.pop(name, None)
        self.db.__dict__.pop("for_session", None)

    def _outside(self, sid: str) -> bool:
        return sid == self.sid and not self.fired and not self.db.in_transaction()

    def _fire(self) -> None:
        self.fired = True
        get, update = self.real["get_session"], self.real["update_session"]

        def another_writer() -> None:  # a read and a write in one transaction: it loses nothing itself
            run = dict(get(self.sid)["run"])
            run[INTERLEAVED] = True
            update(self.sid, run=run)
        self.real["for_session"](self.sid).write(another_writer)

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

    def _for_session(self, sid):
        store, interleave = self.real["for_session"](sid), self

        class Store:  # the session's store, firing before a transaction starts on it
            def __getattr__(self, name):
                return getattr(store, name)

            def write(self, fn, *args, **kwargs):
                if interleave._outside(sid):
                    interleave._fire()
                return store.write(fn, *args, **kwargs)

            async def awrite(self, fn, *args, **kwargs):
                if interleave._outside(sid):
                    interleave._fire()
                return await store.awrite(fn, *args, **kwargs)
        return Store()


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
    return manager.db.sessions.get_session(sid)["run"]


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
    manager.db.sessions.update_session(sid, project="repo", base_commit="abc123")

    async def refreshed(*_args):
        return ""
    manager.runner._refresh_origin = refreshed
    s = manager.db.sessions.get_session(sid)
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
    s = manager.db.sessions.get_session(sid)
    with Interleave(manager.db, sid) as interleave:
        asyncio.run(manager.runner._split_call(s, "write_file", {"path": "a.txt", "content": "hi"}))
    _check(manager, sid, interleave, files_touched=["a.txt"])


class AppliedCli:
    """What `Runner._apply` needs of a transport for these events."""
    backend_session_id = ""


@pytest.mark.parametrize("event, expected", [
    (Session("claude-session-1"), {"backend_session_id": "claude-session-1"}),
    (RateLimits({"status": "allowed", "utilization": 0.2, "resetsAt": 1795046400}),
     {"rate_limits": {"status": "allowed", "utilization": 0.2, "resetsAt": 1795046400}}),
    (ToolResult("toolu-1", "Bash", False, "exit 1"), {"tool_calls": 1, "tool_errors": 1}),
    (Result(answer="done", usage={"input_tokens": 3, "output_tokens": 2}, turns=2, cost=0.01),
     {"turns": 2, "prompt_tokens": 3, "completion_tokens": 2, "total_cost_usd": 0.01, "executing": None}),
], ids=["session", "rate-limits", "tool-result", "result"])
def test_the_hosted_applier_keeps_a_field_written_meanwhile(manager, event, expected):
    sid = manager.create("hosted", backend="claude")["id"]
    with Interleave(manager.db, sid) as interleave:
        asyncio.run(manager.runner._apply(sid, AppliedCli(), event))
    _check(manager, sid, interleave, **expected)


# --- patch_run itself -------------------------------------------------------------------------------------------------

def _row(sid: str, app_id: str = "", run: dict | None = None) -> dict:
    now = time.time()
    return {"id": sid, "project": "scratch", "target": "tower", "model": "fake", "title": sid, "status": "done",
            "workspace": "", "created_at": now, "updated_at": now, "context": [], "app_id": app_id,
            "owner_id": "owner", "run": run if run is not None else {"turns": 2, "notes": "keep", "stale": 1}}


def test_patch_run_merges_unset_run_removes_and_both_return_the_new_run(tmp_path):
    db = Database(tmp_path / "harness.sqlite3")
    try:
        db.sessions.insert_session(_row("s-1"))
        assert db.sessions.patch_run("s-1", turns=3, rate_limits={"utilization": 0.5}) == {
            "turns": 3, "notes": "keep", "stale": 1, "rate_limits": {"utilization": 0.5}}
        assert db.sessions.unset_run("s-1", "stale", "never-there") == {
            "turns": 3, "notes": "keep", "rate_limits": {"utilization": 0.5}}
        assert db.sessions.get_session("s-1")["run"] == {"turns": 3, "notes": "keep", "rate_limits": {"utilization": 0.5}}
        assert db.sessions.patch_run("missing", turns=1) is None  # a session that is gone: nothing written, no error
        assert db.sessions.unset_run("missing", "turns") is None

        def one_transaction() -> None:  # nested in a write it joins that transaction
            db.sessions.patch_run("s-1", a=1)
            db.sessions.patch_run("s-1", b=2)
        db.write(one_transaction)
        run = db.sessions.get_session("s-1")["run"]
        assert (run["a"], run["b"], run["turns"]) == (1, 2, 3)
    finally:
        db.close()


def test_patch_run_goes_to_the_app_store_and_never_to_an_erased_session(tmp_path):
    stores = SessionStores(Database(tmp_path / "harness.sqlite3"), tmp_path / "apps")
    try:
        stores.sessions.insert_session(_row("app-sess", app_id="k-app1"))
        stores.sessions.insert_session(_row("web-sess"))
        stores.sessions.patch_run("app-sess", turns=9)
        stores.sessions.patch_run("web-sess", turns=7)
        assert stores.sessions.get_session("app-sess")["run"]["turns"] == 9
        assert stores.sessions.get_session("web-sess")["run"]["turns"] == 7
        assert stores.main.sessions.get_session("app-sess") is None  # the App's row lives in its own store only
        stores.sessions.delete_session("app-sess")
        with pytest.raises(sqlite3.ProgrammingError):
            stores.sessions.patch_run("app-sess", turns=10)
    finally:
        stores.close()


class AfterTheRead:
    """While active, the first read of session `sid` through `SessionStore.get_session` lets another writer job set
    `run[INTERLEAVED]` as soon as the database allows (`Database.after_commit`): at once if the read is outside a
    writer transaction, or else right after that transaction commits. A write whose read is in the same transaction
    keeps the field; one that read on a pooled reader, or in an earlier transaction, writes over it."""

    def __init__(self, db: Database, sid: str):
        self.db, self.sid, self.fired = db, sid, False

    def __enter__(self):  # on the SessionStore, where the primitive reads (#992)
        self.read = self.db.sessions.get_session
        self.db.sessions.get_session = self._get_session
        return self

    def __exit__(self, *exc):
        self.db.sessions.__dict__.pop("get_session", None)

    def _get_session(self, sid, *args, **kwargs):
        row = self.read(sid, *args, **kwargs)
        if sid == self.sid and not self.fired:
            self.fired = True
            self.db.after_commit(lambda: self.db.write(self._another_writer))
        return row

    def _another_writer(self) -> None:  # a read and a write in one transaction: it loses nothing itself
        run = dict(self.read(self.sid)["run"])
        run[INTERLEAVED] = True
        self.db.sessions.update_session(self.sid, run=run)


@pytest.mark.parametrize("call, expected", [
    (methodcaller("patch_run", "s-1", turns=3), {"turns": 3, "notes": "keep", "stale": 1}),
    (methodcaller("unset_run", "s-1", "stale"), {"turns": 2, "notes": "keep"}),
], ids=["patch_run", "unset_run"])
def test_a_field_another_writer_commits_after_the_primitive_reads_survives(tmp_path, call, expected):
    """`patch_run` and `unset_run` themselves (#991): the read of the session and the write of `run` are one writer
    transaction, so another writer's field lands after the write, never between the two. Each case fails if its
    primitive loses the transaction wrapper or reads outside the transaction it writes in."""
    db = Database(tmp_path / "harness.sqlite3")
    try:
        db.sessions.insert_session(_row("s-1"))
        with AfterTheRead(db, "s-1") as after:
            assert call(db.sessions) == expected
        assert after.fired, "the primitive never read the session through SessionStore.get_session"
        run = db.sessions.get_session("s-1")["run"]
        assert run.get(INTERLEAVED) is True, f"the field another writer committed after the read was lost: {run}"
        assert run == {**expected, INTERLEAVED: True}
    finally:
        db.close()


# --- the source audit -------------------------------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ("harness", "harness_modules")


def _spread(node) -> bool:
    """A dict display with a `**` entry: a copy of another dict with some keys set."""
    return isinstance(node, ast.Dict) and any(key is None for key in node.keys)


def _spreads_run(node) -> bool:
    """A dict display that copies a `run` (`{**s["run"], ...}`, `{**run, ...}`) with some keys set."""
    return _spread(node) and any(key is None and "run" in ast.unparse(value)
                                 for key, value in zip(node.keys, node.values, strict=True))


def _run_rewrites(tree: ast.AST) -> list[tuple[str, int]]:
    """Where a function writes `run` from a copy: `run={**...}`, `"run": {**...}`, or a name holding a spread copy of
    a run that is then passed as `run=`."""
    found: list[tuple[str, int]] = []

    def scan(function, name: str) -> None:
        copies = {target.id for node in ast.walk(function) if isinstance(node, ast.Assign) and _spreads_run(node.value)
                  for target in node.targets if isinstance(target, ast.Name)}
        for node in ast.walk(function):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "run" and (_spread(keyword.value) or (
                            isinstance(keyword.value, ast.Name) and keyword.value.id in copies)):
                        found.append((name, node.lineno))
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "run" and _spread(value):
                        found.append((name, node.lineno))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scan(node, node.name)
    return sorted(set(found))


def _scan() -> list[str]:
    hits = []
    for top in SOURCES:
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            hits += [f"{rel}:{line} ({name})" for name, line in _run_rewrites(ast.parse(path.read_text("utf-8")))]
    return hits


def test_no_code_rewrites_run_from_a_copy():
    """#991: every write of some of `run`'s fields goes through `SessionStore.patch_run`, which merges them into the row
    inside one writer transaction. A whole-value write built from a spread copy of `run` would overwrite whatever
    another write committed after that copy was read."""
    assert _scan() == [], "write these fields with db.sessions.patch_run(sid, **fields) instead"


def test_the_audit_flags_each_form_of_a_rewrite_from_a_copy():
    def scan(source: str) -> list[int]:
        return [line for _name, line in _run_rewrites(ast.parse(source))]

    assert scan("def f(db, s):\n    db.update_session(s['id'], run={**s['run'], 'x': 1})\n") == [2]
    assert scan("def f(db, sid, run):\n    marked = {'status': 'x', 'run': {**run, 'y': 2}}\n") == [2]
    assert scan("def f(db, s):\n    run = {**s['run'], 'x': 1}\n    db.update_session(s['id'], run=run)\n") == [3]
    assert scan("def f(db, sid):\n    db.patch_run(sid, x=1)\n") == []
    assert scan("def f(db, sid, run):\n    db.update_session(sid, run=run)\n") == []  # not a copy it made
    assert scan("def f(e):\n    return {**e, 'data': {}}\n") == []
