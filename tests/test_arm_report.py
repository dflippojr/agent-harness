"""docs/prefix-cache-study/arm_report.py (#898): only attempts the canary grades count towards an arm."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("arm_report", ROOT / "docs" / "prefix-cache-study" / "arm_report.py")
arm_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arm_report)


def _session(out: Path, name: str, outcome: dict | None, prompt_ms: float) -> None:
    rows = [{"seq": 1, "messages": [{"role": "system", "content": "sys"}], "prompt_tokens": 1000, "cache_tokens": 200,
             "prompt_ms": prompt_ms, "completion_tokens": 50, "decode_ms": 700.0}]
    (out / f"{name}.prompts.jsonl").write_bytes("".join(json.dumps(r) + "\n" for r in rows).encode())
    stdout = "Traceback ...\nKeyboardInterrupt\n"
    if outcome is not None:
        result = out / f"{name}.result.json"
        result.write_bytes(json.dumps({"status": "complete", "outcomes": [outcome]}).encode())
        stdout = f"complete: {int(outcome['ok'])}/1 passed · results in {result}\n"
    (out / f"{name}.stdout").write_bytes(stdout.encode())


def test_suspended_and_interrupted_attempts_are_left_out_of_the_arm(tmp_path):
    _session(tmp_path, "off-task-r1", {"ok": True, "status": "done", "seconds": 30.0}, 800.0)
    _session(tmp_path, "off-task-r2", {"ok": False, "status": "suspended", "seconds": 50.0}, 9000.0)
    _session(tmp_path, "off-task-r3", None, 9000.0)  # interrupted: no outcome file
    _session(tmp_path, "off-other-r1", {"ok": False, "status": "wall_limit", "seconds": 1800.0}, 400.0)

    result = arm_report.report(tmp_path)

    arm = result["arms"]["off"]
    assert (arm["sessions"], arm["passed"]) == (2, 1)  # done and wall_limit are graded; suspended and none are not
    assert arm["requests"] == 2 and arm["prefill_s"] == pytest.approx(1.2) and arm["wall_s"] == 1830.0
    assert result["rates_by_context"]["off"]["0-8K"]["requests"] == 2
    graded = {(s["task"], s["rep"]): s["graded"] for s in result["sessions"]}
    assert graded == {("task", "1"): True, ("task", "2"): False, ("task", "3"): False, ("other", "1"): True}
