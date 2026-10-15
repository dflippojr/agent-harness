"""scripts/measure_schema_overhead.py (#645): the documented schema-overhead tables."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_schema_overhead.py"


def _load():
    spec = importlib.util.spec_from_file_location("measure_schema_overhead", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_kits_cover_every_toolkit_and_split_is_a_core_subset():
    module = _load()
    kits = module.kits(module.load_config())
    assert {name for _, names in module.PROFILES for name in names} <= set(kits)
    core = {s["function"]["name"] for s in kits["core"]}
    split = {s["function"]["name"] for s in kits["split"]}
    assert len(kits["core"]) == len(core) and {"verify", "update_notes"} <= core  # names are unique
    assert split == set(module.SPLIT_TOOLS) and split <= core
    assert {s["function"]["name"] for s in kits["memory"]} >= {"memory_edit", "memory_write"}


def test_load_config_leaves_the_real_data_dir_alone(monkeypatch):
    module = _load()
    seen = []
    real_load = module.config_mod.load

    def spy(config_dir=None, data_dir=None):
        seen.append(data_dir)
        return real_load(config_dir, data_dir)
    monkeypatch.setattr(module.config_mod, "load", spy)
    cfg = module.load_config()
    assert seen and seen[0] is not None and not seen[0].exists()
    assert cfg.data_dir == seen[0]


def test_main_prints_both_tables(capsys):
    module = _load()
    module.main()
    out = capsys.readouterr().out
    assert "| Toolkit | Schemas | Characters | Est. tokens |" in out
    # The all-toolkits profile row is the sum of the per-toolkit rows, whatever tools are added.
    counts = {cells[0]: int(cells[1]) for cells in (
        [c.strip() for c in line.strip("|").split("|")] for line in out.splitlines()
        if line.startswith("| ") and "---" not in line) if cells[1].isdigit()}
    every = dict(module.PROFILES)["Every built-in toolkit"]
    assert counts["Every built-in toolkit"] == sum(counts[name] for name in every)
    assert "% of 64K" in out
