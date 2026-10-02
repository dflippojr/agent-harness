"""SQLite off the event loop (#294): one writer thread, a pool of read-only WAL connections, awaitable API."""

import asyncio
import sqlite3
import threading
import time

import pytest

from harness import metrics
from harness.bus import EventBus
from harness.db import Database


def _session(sid: str) -> dict:
    now = time.time()
    return {"id": sid, "project": "scratch", "target": "local", "model": "fake", "title": sid, "status": "running",
            "workspace": "/w", "created_at": now, "updated_at": now, "context": "[]"}


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.insert_session(_session("s1"))
    yield d
    d.close()


def test_writes_run_on_the_writer_thread_and_reads_on_a_read_only_connection(db):
    seen = {}

    def probe():
        seen["write_thread"] = threading.current_thread().name
        seen["write_conn_is_writer"] = db.conn is db._wconn
    db.write(probe)
    assert seen == {"write_thread": "harness-db-writer", "write_conn_is_writer": True}
    with db.reading() as conn:
        assert conn is not db._wconn
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM sessions")
    assert db.get_session("s1")["status"] == "running"


def test_write_is_one_transaction_and_rolls_back_on_error(db):
    def fail():
        db.update_session("s1", status="done")
        db.insert_event("s1", "status", {"status": "done"})
        raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        db.write(fail)
    assert db.get_session("s1")["status"] == "running"
    assert db.events("s1") == []


def test_nested_write_joins_the_outer_transaction(db):
    def inner():
        db.update_session("s1", status="queued")

    def outer():
        db.write(inner)
        assert db.in_transaction()
        raise RuntimeError("undo both")
    with pytest.raises(RuntimeError):
        db.write(outer)
    assert db.get_session("s1")["status"] == "running"


def test_reads_inside_a_write_see_its_uncommitted_changes(db):
    def change_and_read():
        db.update_session("s1", status="queued")
        return db.get_session("s1")["status"]
    assert db.write(change_and_read) == "queued"


def test_emit_inside_a_transaction_publishes_after_commit_in_the_caller_thread(db):
    bus = EventBus(db)
    heard = []
    bus.add_listener(lambda e: heard.append((e["type"], threading.current_thread().name,
                                             db.get_session("s1")["status"])))

    def change():
        db.update_session("s1", status="done")
        bus.emit("s1", "status", {"status": "done"})
        assert heard == []  # not before the commit
    db.write(change)
    assert heard == [("status", threading.current_thread().name, "done")]


def test_emit_is_dropped_with_a_rolled_back_transaction(db):
    bus = EventBus(db)
    heard = []
    bus.add_listener(heard.append)

    def change():
        bus.emit("s1", "status", {"status": "done"})
        raise RuntimeError("abort")
    with pytest.raises(RuntimeError):
        db.write(change)
    assert heard == [] and db.events("s1") == []


def test_a_slow_write_does_not_block_reads_or_the_event_loop(db):
    async def run():
        ticks = 0
        started = threading.Event()

        def slow():
            started.set()
            db.update_session("s1", status="done")
            time.sleep(0.4)

        write = asyncio.create_task(db.awrite(slow))
        await asyncio.to_thread(started.wait)
        t0 = time.perf_counter()
        status = (await db.aio.get_session("s1"))["status"]
        read_seconds = time.perf_counter() - t0
        while not write.done():
            ticks += 1
            await asyncio.sleep(0.01)
        return status, read_seconds, ticks

    status, read_seconds, ticks = asyncio.run(run())
    assert status == "running"  # the reader sees the last commit, not the open transaction
    assert read_seconds < 0.3
    assert ticks >= 5


def test_aio_methods_and_aemit(db):
    bus = EventBus(db)
    sub = bus.subscribe("s1")

    async def run():
        event = await bus.aemit("s1", "user_message", {"content": "parser cache"})
        await db.aio.update_session("s1", status="done")
        return event, await db.aio.get_session("s1"), sub.queue.get_nowait()

    event, s, published = asyncio.run(run())
    assert s["status"] == "done"
    assert published["seq"] == event["seq"] == db.last_event_seq("s1")


def test_awrite_returns_the_callable_result(db):
    async def run():
        return await db.awrite(lambda: db.insert_event("s1", "note", {"text": "x"})["seq"])
    assert asyncio.run(run()) == db.last_event_seq("s1")


def test_close_stops_the_writer_and_refuses_new_work(tmp_path):
    d = Database(tmp_path / "c.db")
    writer = d._writer
    d.close()
    assert not writer.is_alive()
    with pytest.raises(sqlite3.ProgrammingError):
        d.insert_session(_session("s2"))
    d.close()  # idempotent


def test_metrics_aggregates_run_on_a_read_connection_and_are_cached(db, monkeypatch):
    monkeypatch.setattr(metrics, "CORE_CACHE_SECONDS", 10.0)
    calls = []
    real = metrics._core_rows

    def counting(d):
        calls.append(d.conn is not d._wconn and threading.current_thread().name != "harness-db-writer")
        return real(d)
    monkeypatch.setattr(metrics, "_core_rows", counting)
    with db.reading():
        metrics._cached_core_rows(db)
        db.insert_event("s1", "error", {"message": "x"})
        rows = metrics._cached_core_rows(db)
    assert calls == [True]
    assert rows["kinds"].get("error", 0) == 0  # served from the cache
    monkeypatch.setattr(metrics, "CORE_CACHE_SECONDS", 0.0)
    with db.reading():
        assert metrics._cached_core_rows(db)["kinds"]["error"] == 1
    assert len(calls) == 2
