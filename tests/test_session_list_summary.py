"""The session list builds each card from targeted reads, off the event loop (issue #667)."""

from __future__ import annotations

import threading
import time

import pytest

from harness.db import Database
from harness.manager import Manager

from test_api import LOGIN, make_client
from test_daemon import Script, make_cfg


def _session(sid: str, answer: str) -> dict:
    now = time.time()
    return {"id": sid, "project": "scratch", "target": "tower", "model": "fake", "title": sid, "status": "done",
            "workspace": "", "created_at": now, "updated_at": now, "context": [{"role": "user", "content": "x"}],
            "owner_id": "owner", "answer": answer}


def _reference_summary(db: Database, sid: str) -> str:
    """The card text as `list_summary` built it before #667, from every event and the whole session row."""
    user_messages = [" ".join(e["data"].get("content", "").split()) for e in db.events(sid)
                     if e["type"] == "user_message" and e["data"].get("content", "").strip()]
    asks = " · ".join(text[:90] + ("…" if len(text) > 90 else "") for text in user_messages[:3])
    if len(user_messages) > 3:
        asks += f" · {len(user_messages) - 3} more follow-up{'s' if len(user_messages) > 4 else ''}"
    full = db.get_session(sid)
    assert full is not None
    answer = " ".join((full["answer"] or "").split())
    if answer:
        outcome = answer[:110] + ("…" if len(answer) > 110 else "")
        return f"{asks} — {outcome}" if asks else outcome
    return asks


LONG = "word " * 60
CASES = {
    "none-empty": ([], ""),
    "answer-only": ([], "Done."),
    "one": (["Make dark mode black"], "Changed the palette."),
    "two": (["Make  dark\nmode black", "   ", "Also move the profile control"], ""),
    "three-long": ([LONG, "second " * 20, "third"], LONG),
    "four": (["a", "b", "c", "d"], "short"),
    "many": ([f"follow-up {i}" for i in range(7)], "x" * 111),
    "exact-110": (["ask"], "y" * 110),
    "spaced-answer": (["ask"], " " * 600 + "tail words"),
    "spaced-long-answer": (["ask"], " \n" * 300 + "z" * 200),
    "blank-and-missing": (["", "\t", "real ask"], "ok"),
    "nul": (["Run it\x00 and report", "plain"], "Done.\x00Tests failed"),
    "nul-long-answer": (["ask"], "a\x00" + "b" * 500),
}


@pytest.fixture
def manager(tmp_path):
    m = Manager(make_cfg(tmp_path), chat=Script([]))
    for sid, (asks, answer) in CASES.items():
        m.db.insert_session(_session(sid, answer))
        for text in asks:
            m.db.insert_event(sid, "user_message", {"content": text})
        m.db.insert_event(sid, "user_message", {"attachments": []})  # a message without content counts for nothing
    yield m
    m.db.close()


def test_card_text_matches_the_previous_builder(manager):
    for sid in CASES:
        assert manager.list_summary(manager.db.get_session(sid))["chat_summary"] == _reference_summary(manager.db, sid), sid


def test_card_bits_hold_the_answer_and_every_user_message(tmp_path):
    db = Database(tmp_path / "bits.db")
    try:
        db.insert_session(_session("s1", "Finished the change. " * 20))
        db.insert_event("s1", "user_message", {"content": "Make dark mode black"})
        db.insert_event("s1", "tool_result", {"id": "c0", "output": "out\n" * 50})
        db.insert_event("s1", "user_message", {"content": "Also move the profile control"})
        bits = db.session_card_bits("s1")
        assert bits["asks"] == ["Make dark mode black", "Also move the profile control"]
        assert bits["answer_length"] == len("Finished the change. " * 20)
        assert db.session_card_bits("missing") == {"answer": "", "answer_length": 0, "asks": []}
    finally:
        db.close()


def test_a_card_reads_neither_events_nor_the_full_session(manager, monkeypatch):
    sid = "big"
    manager.db.insert_session(_session(sid, "Finished."))
    manager.db.insert_event(sid, "user_message", {"content": "Do the thing"})
    for n in range(250):
        manager.db.insert_event(sid, "tool_result", {"id": f"c{n}", "output": "out\n" * 100})
    row = manager.db.get_session(sid)
    expected = _reference_summary(manager.db, sid)

    def refuse(*_args, **_kwargs):
        raise AssertionError("a list card must not read every event or the whole session row")
    monkeypatch.setattr(Database, "events", refuse)
    monkeypatch.setattr(Database, "get_session", refuse)
    card = manager.list_summary(row)
    assert card["chat_summary"] == expected
    assert card["last_event_seq"] == manager.db.last_event_seq(sid)


def test_session_list_builds_cards_off_the_event_loop(tmp_path, monkeypatch):
    client, m, _ = make_client(tmp_path, [])
    m.db.insert_session(_session("s1", "Finished."))
    threads: list[threading.Thread] = []
    original = Manager.list_summary

    def spy(self, s, positions=None):
        threads.append(threading.current_thread())
        return original(self, s, positions)
    monkeypatch.setattr(Manager, "list_summary", spy)
    with client:
        assert client.portal is not None
        loop_thread = client.portal.call(threading.current_thread)
        cards = client.get("/sessions", headers={"Tailscale-User-Login": LOGIN}).json()
    assert [c["id"] for c in cards] == ["s1"] and cards[0]["chat_summary"] == "Finished."
    assert threads and all(t is not loop_thread for t in threads)
