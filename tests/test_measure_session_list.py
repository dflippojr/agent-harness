"""scripts/measure_session_list.py (#667): the documented session-list benchmark."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_session_list.py"


def _load():
    spec = importlib.util.spec_from_file_location("measure_session_list", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_benchmark_prints_one_row_for_a_small_fixture(monkeypatch, capsys):
    module = _load()
    monkeypatch.setattr(module, "SESSIONS", 3)
    monkeypatch.setattr(module, "RUNS", 1)
    monkeypatch.setattr(sys, "argv", ["measure_session_list.py", "12"])
    module.main()
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("| events per session | before | after |")
    assert len(lines) == 3 and lines[2].startswith("| 12 | ") and lines[2].endswith(" MB |")


def test_card_reads_see_both_user_messages(tmp_path, monkeypatch):
    module = _load()
    monkeypatch.setattr(module, "SESSIONS", 2)
    db = module.Database(tmp_path / "bench.db")
    try:
        sids = module.seed(db, 5)
        assert len(sids) == 2
        bits = db.session_card_bits(sids[0])
        assert bits["asks"] == ["Make dark mode black", "Also move the profile control"]
        assert bits["answer_length"] == len("Finished the change. " * 20)
        assert db.session_card_bits("missing") == {"answer": "", "answer_length": 0, "asks": []}
    finally:
        db.close()
