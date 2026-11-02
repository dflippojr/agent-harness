"""Batched observation masking (#855, docs/compaction.md): with `compaction.mask_batch_at` off the prompts are what
they were before the setting existed; with it on, the prompt only grows between batches. Nothing needs a GPU."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bakeoff import canary as canary_cli, prefix_diff, trim_bench
from harness import compaction, config as config_mod, fake_instance, fake_model
from harness.manager import Manager

FIXTURE = Path(__file__).parent / "fixtures" / "mask_off_prompts.json"

# A session that uses five long results in a row, on a window small enough to reach compaction.
SESSION_SCRIPT = """
context_tokens: %d
scripts:
  - name: mask-batch
    match: "mask batch"
    turns:
      - text: "Writing."
        tool_calls: [{name: write_file, args: {path: data.txt, content: "%s"}}]
      - text: "Reading one."
        tool_calls: [{name: read_file, args: {path: data.txt}}]
      - text: "Reading two."
        tool_calls: [{name: read_file, args: {path: data.txt, start_line: 1}}]
      - text: "Reading three."
        tool_calls: [{name: read_file, args: {path: data.txt, start_line: 1, max_lines: 5000}}]
      - text: "Listing."
        tool_calls: [{name: list_files, args: {path: "."}}]
      - text: "Reading four."
        tool_calls: [{name: read_file, args: {path: data.txt, start_line: 1, max_lines: 4000}}]
      - text: "Reading five."
        tool_calls: [{name: read_file, args: {path: data.txt, start_line: 1, max_lines: 3000}}]
      - text: "Listing again."
        tool_calls: [{name: list_files, args: {path: "."}}]
      - text: "Done."
""" % (32768, "line of data " * 400)


def run_session(monkeypatch, tmp_path, configure=None) -> list[dict]:
    """The real runner with the scripted model; returns the captured requests in order."""
    from harness import sandbox

    def no_docker(*args, **kwargs):
        raise AssertionError("fake mode must not call Docker")

    monkeypatch.setattr(sandbox, "run_cmd", no_docker)
    config_dir, data_dir = fake_instance.write_instance(tmp_path / "instance", 0)
    script = tmp_path / "fixtures.yaml"
    script.write_text(SESSION_SCRIPT, encoding="utf-8")
    monkeypatch.setenv(fake_model.ENV, str(script))
    cfg = config_mod.load(config_dir, data_dir)
    if configure:
        configure(cfg)
    m = Manager(cfg)
    path = tmp_path / "prompts.jsonl"
    m.runner.chat = canary_cli.PromptCapture(path, chat=m.runner.chat, holder=lambda: m.scheduler.holder)

    async def go():
        await m.start(maintenance=False)
        try:
            s = m.create("Run the mask batch session.", project=fake_instance.PROJECT)
            for _ in range(600):
                if m.db.get_session(s["id"])["status"] in ("done", "failed"):
                    break
                await asyncio.sleep(0.05)
            return m.db.get_session(s["id"])
        finally:
            await m.stop()

    try:
        final = asyncio.run(go())
    finally:
        m.db.close()
    assert final["status"] == "done", final
    return prefix_diff.read_jsonl(path)


def receipts(request: dict) -> list[int]:
    return [i for i, message in enumerate(request["messages"]) if message["role"] == "tool"
            and (message.get("content") or "").startswith(compaction.RECEIPT_PREFIX)]


def test_bench_prompts_match_the_ones_recorded_before_the_setting_existed(monkeypatch):
    """The fixture was written by `trim_bench.prompt_digests` on the commit before batching. If a deliberate change
    to masking, trim, elide or the summary moves it, rewrite the fixture in that change."""
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # Thresholds depend on the size of the tool schemas, which change for reasons of their own.
    monkeypatch.setattr(trim_bench, "OVERHEAD_CHARS", recorded["overhead_chars"])
    assert trim_bench.prompt_digests() == recorded["digests"]


PER_TURN = [[], [], [], [5], [5, 7], [5, 7, 9], [5, 7, 9], [5, 7, 9, 13], [5, 7, 9, 13, 15]]


def test_a_session_masks_each_result_one_turn_after_it_is_used(monkeypatch, tmp_path):
    """Today's timing through the real runner, recorded before the setting existed."""
    assert [receipts(r) for r in run_session(monkeypatch, tmp_path)] == PER_TURN
