"""Transcript render handles mask compaction and skips malformed events (#248)."""

from __future__ import annotations

import logging

from harness.db import Database
from harness.transcript import render, write_transcript


def _db(tmp_path, sid: str = "s1") -> Database:
    db = Database(tmp_path / "h.sqlite3")
    db.insert_session({"id": sid, "project": "demo", "target": "tower", "model": "fake", "title": "Demo",
                       "status": "done", "workspace": "", "created_at": 1, "updated_at": 1, "context": [],
                       "answer": "", "totals": {}})
    return db


def test_render_mask_compaction_does_not_need_token_counts(tmp_path):
    db = _db(tmp_path)
    db.insert_event("s1", "user_message", {"content": "hello"})
    db.insert_event("s1", "compaction", {"tier": "mask", "tokens_saved": 4200, "characters_saved": 12600})
    text = render(db, "s1")
    assert "Replaced old tool outputs with recoverable receipts (~4200 tokens saved)" in text
    assert "Context compaction (mask)" not in text


def test_render_summary_and_elide_compaction_unchanged(tmp_path):
    db = _db(tmp_path)
    db.insert_event("s1", "compaction", {"tier": "elide", "tokens_before": 8000, "tokens_after": 5000})
    db.insert_event("s1", "compaction", {"tier": "summary", "tokens_before": 5000, "tokens_after": 2000,
                                         "summary": "kept going"})
    text = render(db, "s1")
    assert "Context compaction (elide): ~8000 → ~5000 tokens" in text
    assert "Context compaction (summary): ~5000 → ~2000 tokens" in text
    assert "kept going" in text


def test_render_skips_malformed_event_and_keeps_the_rest(tmp_path, caplog):
    db = _db(tmp_path)
    db.insert_event("s1", "user_message", {"content": "keep me"})
    db.insert_event("s1", "compaction", {"tier": "elide"})
    db.insert_event("s1", "status", {"status": "done", "stop_reason": "finished"})
    with caplog.at_level(logging.WARNING, logger="harness.transcript"):
        text = render(db, "s1")
    assert "keep me" in text
    assert "Done (finished)" in text
    assert "Context compaction (elide)" not in text
    assert "skipping malformed compaction event" in caplog.text


def test_write_transcript_survives_a_mask_event(tmp_path):
    db = _db(tmp_path)
    db.insert_event("s1", "compaction", {"tier": "mask", "tokens_saved": 9, "characters_saved": 27})
    path = write_transcript(db, tmp_path / "out", "s1")
    body = path.read_text(encoding="utf-8")
    assert path.name == "s1.md"
    assert "Replaced old tool outputs with recoverable receipts (~9 tokens saved)" in body
