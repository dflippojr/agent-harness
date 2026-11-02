"""Batched observation masking (#855, docs/compaction.md): with `compaction.mask_batch_at` off the prompts are what
they were before the setting existed; with it on, the prompt only grows between batches. Nothing needs a GPU."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from bakeoff import canary as canary_cli, prefix_diff, trim_bench
from harness import compaction, config as config_mod, fake_instance, fake_model
from harness.config import _mask_batch_at
from harness.llm import Completion
from harness.manager import Manager
from test_daemon import Script, call, events, make_cfg, wait_status

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


def test_the_shipped_setting_is_off(tmp_path):
    assert config_mod.load(config_mod.ROOT / "config", tmp_path / "data").mask_batch_at == 0


def test_a_session_with_the_setting_named_off_sends_the_same_prompts(monkeypatch, tmp_path):
    def off(cfg):
        cfg.mask_batch_at = 0.0

    assert [receipts(r) for r in run_session(monkeypatch, tmp_path, off)] == PER_TURN


@pytest.mark.parametrize("value, expected", [
    (None, 0.0), ("soon", 0.0), (True, 0.0), (0, 0.0), (-0.1, 0.0), (float("inf"), 0.0), (float("nan"), 0.0),
    (0.55, 0.0), (0.9, 0.0),  # at elide_at or above, trim and elide would see the results before masking does
    (0.45, 0.45), ("0.4", 0.4), (0.01, 0.01),
])
def test_mask_batch_at_from_config_values(value, expected):
    assert _mask_batch_at(value) == expected


def test_timing_decisions():
    n = 1000
    assert compaction.mask_due(0, n, 0.0, 0.55)                     # off: every pass
    assert not compaction.mask_due(449, n, 0.45, 0.55)
    assert compaction.mask_due(450, n, 0.45, 0.55)
    assert compaction.mask_due(300, n, 0.45, 0.30)                  # elide_at moved below the setting at run time
    assert compaction.mask_batch_worthwhile(0, 0, n, 0.0, 0.55)     # off: every pass
    assert not compaction.mask_batch_worthwhile(99, 500, n, 0.45, 0.55)
    assert compaction.mask_batch_worthwhile(100, 500, n, 0.45, 0.55)
    assert compaction.mask_batch_worthwhile(1, 550, n, 0.45, 0.55)  # at elide_at masking goes first, however little


def test_batched_session_masks_in_one_rewrite_and_only_grows_between(monkeypatch, tmp_path):
    def batched(cfg):
        cfg.mask_batch_at = 0.14

    rows = run_session(monkeypatch, tmp_path, batched)
    seen = [receipts(r) for r in rows]
    rewrites = [p for p in prefix_diff.pairs(rows) if p["class"] == "masking: observation receipt"]
    assert seen[-1] and set(seen[-1]) <= set(PER_TURN[-1])
    assert len(rewrites) < len(set(PER_TURN[-1]))                   # per-turn rewrites once for each of the five
    assert any(len(after) - len(before) > 1 for before, after in zip(seen, seen[1:], strict=False))  # at once
    assert {p["class"] for p in prefix_diff.pairs(rows)} == {"", "masking: observation receipt"}


# _maybe_compact, on a seeded context
RESULT = "0123456789" * 1500  # 15,000 characters: about 5,000 estimated tokens
WINDOW = 32768


def batching_manager(tmp_path, batch_at: float = 0.45) -> Manager:
    cfg = make_cfg(tmp_path, context_tokens=WINDOW)
    cfg.mask_batch_at = batch_at
    return Manager(cfg, chat=Script([Completion(content="done")]))


async def seed(m: Manager, sid: str, count: int, start: int = 1, text: str = RESULT) -> None:
    for i in range(start, start + count):
        c = call("read_file", i, path=f"f{i}.txt")
        s = m.db.get_session(sid)
        m.db.update_session(sid, context=s["context"] + [{"role": "assistant", "content": None, "tool_calls": [c]}])
        await m.runner._record_result(sid, c, "read_file", f"{i} {text}", ok=True)
    s = m.db.get_session(sid)
    m.db.update_session(sid, context=s["context"] + [{"role": "assistant", "content": "used them"}])


def masked_count(m: Manager, sid: str) -> int:
    return sum(1 for message in m.db.get_session(sid)["context"] if message["role"] == "tool"
               and message["content"].startswith(compaction.RECEIPT_PREFIX))


def test_below_the_threshold_nothing_is_rewritten_then_everything_eligible_is(tmp_path):
    async def body():
        m = batching_manager(tmp_path)
        await m.start()
        sid = (await wait_status(m, m.create("task")["id"], "done"))["id"]
        await seed(m, sid, 1)
        before = m.db.get_session(sid)["context"]
        await m.runner._maybe_compact(m.db.get_session(sid))
        assert m.db.get_session(sid)["context"] == before and not events(m, sid, "compaction")

        await seed(m, sid, 3, start=2)  # about 21,000 estimated tokens with the schemas: past 0.45 of the window
        await m.runner._maybe_compact(m.db.get_session(sid))
        assert masked_count(m, sid) == 4
        [mask] = [e for e in events(m, sid, "compaction") if e["tier"] == "mask"]  # one rewrite, one event
        assert mask["characters_saved"] > 4 * 14000
        for i in (1, 4):  # recoverable like any receipt
            digest = hashlib.sha256(f"{i} {RESULT}".encode("utf-8")).hexdigest()
            assert m.db.full_artifact(sid, digest) == f"{i} {RESULT}"
        await m.stop()
    asyncio.run(body())


def test_a_small_batch_waits_above_the_threshold_until_elide_at(tmp_path):
    """Above the threshold on results masking can't touch: one eligible result is no reason to break the prefix, until
    elide would otherwise shorten it."""
    async def body():
        m = batching_manager(tmp_path, 0.20)
        await m.start()
        sid = (await wait_status(m, m.create("task")["id"], "done"))["id"]
        s = m.db.get_session(sid)
        stuck = [{"role": "user", "content": "note " * 6000}]  # 30,000 characters: masking can't touch it
        m.db.update_session(sid, context=s["context"] + stuck)
        await seed(m, sid, 1, text="y" * 3000)                  # worth about 1,000 tokens: under the 10% minimum
        await m.runner._maybe_compact(m.db.get_session(sid))
        assert masked_count(m, sid) == 0

        s = m.db.get_session(sid)
        m.db.update_session(sid, context=s["context"] + [{"role": "user", "content": "note " * 18000}])  # past elide_at
        await m.runner._maybe_compact(m.db.get_session(sid))
        assert [e["tier"] for e in events(m, sid, "compaction")][0] == "mask"  # ahead of the trim and elide it heads off
        await m.stop()
    asyncio.run(body())


@pytest.mark.parametrize("batch_at, resets", [(0.45, False), (0.0, True)])
def test_a_batch_that_frees_the_context_does_not_also_reset_it(tmp_path, batch_at, resets):
    """Past reset_at with everything unmasked, under it once masked. Per-turn masking judges the size before the mask,
    and only ever sees one new result there."""
    async def body():
        m = batching_manager(tmp_path, batch_at)
        await m.start()
        sid = (await wait_status(m, m.create("task")["id"], "done"))["id"]
        s = m.db.get_session(sid)
        state = {"goal": "g", "plan": "p", "next_step": "n"}
        m.db.update_session(sid, run={**s["run"], "state": state})
        assert m.runner.has_valid_saved_state(m.db.get_session(sid))
        await seed(m, sid, 5)  # about 25,000 estimated tokens plus schemas: past 0.60 of 32,768
        await m.runner._maybe_compact(m.db.get_session(sid))
        tiers = {e["tier"] for e in events(m, sid, "compaction")}
        assert ("round_reset" in tiers) is resets
        assert (not tiers & {"elide", "summary"}) if not resets else True
        await m.stop()
    asyncio.run(body())


@pytest.mark.parametrize("batch_at, masked", [(0.45, True), (0.0, False)])
def test_an_overflow_masks_what_a_batch_had_not_reached_before_it_summarizes(tmp_path, batch_at, masked):
    """The server's count ran past the estimate while the results were still whole. Off, the context was already masked
    on the turn before, so this path is left as it was."""
    async def body():
        m = batching_manager(tmp_path, batch_at)
        m.cfg.keep_recent = 0.001
        script = Script([Completion(content="done"), Completion(content="SUMMARY: did things")])
        m.runner.chat = script
        await m.start()
        sid = (await wait_status(m, m.create("task")["id"], "done"))["id"]
        await seed(m, sid, 2, text="z" * 3000)
        await seed(m, sid, 1, start=3, text="z" * 3000)
        await m.runner._compact_on_overflow(m.db.get_session(sid))
        tiers = [e["tier"] for e in events(m, sid, "compaction")]
        assert ("mask" in tiers) is masked and tiers[-1] == "summary"
        summary_request = next(r for r in script.requests if r[0]["content"] == compaction.SUMMARY_SYSTEM)
        assert (compaction.RECEIPT_PREFIX in summary_request[1]["content"]) is masked
        digest = hashlib.sha256(f"1 {'z' * 3000}".encode("utf-8")).hexdigest()
        assert (m.db.full_artifact(sid, digest) is not None) is masked
        await m.stop()
    asyncio.run(body())


def test_canary_flag_reaches_the_run_and_rejects_a_value_past_elide(monkeypatch):
    seen = []

    async def standalone(tasks, repeats, fixture, **kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(canary_cli, "run_standalone", standalone)
    canary_cli.main(["--tasks", "a"])
    canary_cli.main(["--tasks", "a", "--mask-batch-at", "0.45"])
    assert "mask_batch_at" not in seen[0] and seen[1]["mask_batch_at"] == 0.45
    with pytest.raises(SystemExit):
        canary_cli.main(["--mask-batch-at", "0.55"])


def test_batching_keeps_summary_calls_at_the_production_window_and_breaks_less():
    """The #855 criterion, on every bench transcript at the window production uses."""
    for make in trim_bench.MASK_TRANSCRIPTS.values():
        steps = make()
        per_turn = trim_bench.ReadCost()
        off = trim_bench.replay(steps, trim_bench.MASK_ARMS[0], on_prompt=per_turn)
        batched = trim_bench.ReadCost()
        on = trim_bench.replay(steps, trim_bench.Arm("batch", True, mask_batch_at=0.45), on_prompt=batched)
        assert on.summary_calls <= off.summary_calls and on.passes <= off.passes
        assert batched.breaks < per_turn.breaks and batched.fallback < per_turn.fallback


def test_read_cost_prices_growth_and_a_rewrite():
    head = [{"role": "system", "content": "s" * 300}, {"role": "user", "content": "task"}]
    result = {"role": "tool", "tool_call_id": "c", "content": "r" * 30000}
    reply = {"role": "assistant", "content": "a" * 300}
    cost = trim_bench.ReadCost()
    cost(head + [reply, result])
    first = cost.first_change
    assert cost.breaks == 0 and first == cost.fallback  # the first prompt reads all of it

    grown = head + [reply, result, reply, {"role": "tool", "tool_call_id": "d", "content": "n" * 3000}]
    cost(grown)
    assert cost.breaks == 0
    new = (compaction.message_chars(grown[-1])) / trim_bench.CHARS_PER_TOKEN
    assert cost.first_change - first == pytest.approx(new)      # not the reply: the server generated it
    assert cost.fallback == cost.first_change

    masked = [*grown]
    masked[3] = {"role": "tool", "tool_call_id": "c", "content": compaction.RECEIPT_PREFIX}
    before_first, before_fallback = cost.first_change, cost.fallback
    cost(masked)
    assert cost.breaks == 1
    assert cost.first_change - before_first < cost.fallback - before_fallback  # the checkpoint lies before the change
