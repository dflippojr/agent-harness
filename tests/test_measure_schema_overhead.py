"""scripts/measure_schema_overhead.py (#645): the documented schema-overhead tables."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from harness import config as config_mod

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_schema_overhead.py"


def _load():
    spec = importlib.util.spec_from_file_location("measure_schema_overhead", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_kits_cover_every_toolkit_and_split_is_a_core_subset():
    module = _load()
    kits = module.kits(config_mod.load())
    assert {name for _, names in module.PROFILES for name in names} <= set(kits)
    core = {s["function"]["name"] for s in kits["core"]}
    split = {s["function"]["name"] for s in kits["split"]}
    assert len(kits["core"]) == 12 and {"verify", "update_notes"} <= core
    assert split == set(module.SPLIT_TOOLS) and split <= core
    assert {s["function"]["name"] for s in kits["memory"]} >= {"memory_edit", "memory_write"}


def test_main_prints_both_tables(capsys):
    _load().main()
    out = capsys.readouterr().out
    assert "| Toolkit | Schemas | Characters | Est. tokens |" in out
    assert "| Every built-in toolkit | 33 |" in out
    assert "% of 64K" in out
