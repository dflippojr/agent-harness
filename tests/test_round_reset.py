"""Structured state scratchpad and per-round context reset (#156)."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from harness import compaction
from harness.config import Project, _reset_at, _state_max_chars
from harness.llm import Completion
from harness.manager import Manager
from harness.metrics import render as render_metrics
from harness.runner import Runner, new_run
from harness.state import (
    git_porcelain, inject_payload, is_dead_end_retry, is_valid_state, paths_since, validate_state,
)
from harness.tools import ToolError
from test_daemon import Script, call, events, make_cfg, wait_status

PAGE = "SearXNG is free software released under the GNU Affero General Public License, version 3 or later."
FAIL_ARGS = {"cmd": "boom", "flag": False}


def _state_from_context(context: list[dict]) -> dict:
    for message in context:
        content = message.get("content") or ""
        if content.startswith(compaction.STATE_TAG):
            return json.loads(content.split("\n\n", 1)[1])
    raise AssertionError("no tagged state message in context")


def _assert_tool_groups_intact(context: list[dict]) -> None:
    pending: set[str] = set()
    for message in context:
        role = message.get("role")
        if role == "assistant":
            assert not pending, "assistant tool_calls were split from their results"
            pending = {item["id"] for item in message.get("tool_calls") or []}
        elif role == "tool":
            call_id = message.get("tool_call_id")
            assert call_id in pending, "tool result without its assistant call"
            pending.discard(call_id)


def test_schema_rejects_unknown_and_fills_omitted_fields():
    got = validate_state({"goal": "ship it", "plan": "step 1"}, 8000)
    assert got == {"goal": "ship it", "plan": "step 1", "errors": [], "next_step": "", "notes": ""}
    with pytest.raises(ToolError, match="invalid state"):
        validate_state({"goal": "x", "files_modified": ["a.py"]}, 8000)
    with pytest.raises(ToolError, match="invalid state"):
        validate_state({"plan": "no goal"}, 8000)
    with pytest.raises(ToolError, match="invalid state"):
        validate_state({"goal": 1}, 8000)
    with pytest.raises(ToolError, match="invalid state"):
        validate_state({"goal": "x", "errors": [{"command": "run_shell"}]}, 8000)
    with pytest.raises(ToolError, match="characters"):
        validate_state({"goal": "g" * 9000}, 8000)
    assert not is_valid_state(None)
    assert not is_valid_state({})
    assert is_valid_state(got)


def test_dead_end_retry_matches_recorded_command_and_args():
    saved = {"goal": "g", "errors": [{"command": "run_fake", "args": FAIL_ARGS, "message": "nope"}],
             "plan": "", "next_step": "", "notes": ""}
    assert is_dead_end_retry(saved, "run_fake", FAIL_ARGS)
    assert not is_dead_end_retry(saved, "run_fake", {**FAIL_ARGS, "cmd": "other"})
    assert not is_dead_end_retry(saved, "write_file", FAIL_ARGS)


def test_apply_round_reset_keeps_head_and_atomic_tail():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    msgs.append({"role": "assistant", "content": "", "tool_calls": [call("read_file", 0, path="a")]})
    msgs.append({"role": "tool", "tool_call_id": "c0-read_file", "content": "old"})
    last = call("reset_round", 1)
    msgs.append({"role": "assistant", "content": "", "tool_calls": [last, call("read_file", 2, path="b")]})
    msgs.append({"role": "tool", "tool_call_id": last["id"], "content": "Round reset scheduled."})
    msgs.append({"role": "tool", "tool_call_id": "c2-read_file", "content": "b"})
    payload = inject_payload({"goal": "g", "errors": [], "plan": "", "next_step": "", "notes": ""},
                             ["hello.txt"], 8000)
    new = compaction.apply_round_reset(msgs, payload)
    assert new[0]["content"] == "sys"
    assert new[1]["content"] == "task"
    assert new[2]["content"].startswith(compaction.STATE_TAG)
    assert new[3]["content"] == compaction.ROUND_CONTINUE
    assert new[4:] == msgs[compaction.last_turn_start(msgs):]
    _assert_tool_groups_intact(new)
    assert "hello.txt" in new[2]["content"]


def test_git_porcelain_paths_and_baseline_diff(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    assert git_porcelain(tmp_path) == []
    (tmp_path / "new.txt").write_text("x", encoding="utf-8")
    current = git_porcelain(tmp_path)
    assert current is not None
    assert "new.txt" in paths_since([], current)
    assert paths_since(current, current) == []
    assert git_porcelain(tmp_path / "missing") is None


def test_reset_at_and_state_max_chars_fallback():
    assert _reset_at(None) == 0.60
    assert _reset_at("nope") == 0.60
    assert _reset_at(0.05) == 0.60
    assert _reset_at(0.99) == 0.60
    assert _reset_at(0.7, 0.55, 0.90) == 0.7
    assert _reset_at(0.60) == 0.60
    assert _reset_at(True) == 0.60
    assert _reset_at(float("nan")) == 0.60
    assert _reset_at(float("nan"), 0.55, 0.90) == 0.60
    assert _reset_at(0.60, 0.4, 0.6) == 0.5
    assert _state_max_chars("nope") == 8000
    assert _state_max_chars(100) == 8000
    assert _state_max_chars(4096) == 4096
    assert _state_max_chars(True) == 8000


def test_validate_compaction_requires_elide_reset_summarize_order(tmp_path):
    from harness.settings_keys import validate_compaction
    cfg = make_cfg(tmp_path)
    assert validate_compaction(cfg, {}) == []
    keys = {item["key"] for item in validate_compaction(cfg, {"compaction.reset_at": 0.50})}
    assert "compaction.reset_at" in keys
    keys = {item["key"] for item in validate_compaction(cfg, {"compaction.reset_at": 0.80})}
    assert "compaction.reset_at" in keys


def test_new_run_carries_state():
    run = new_run(carry={"notes": "n", "state": {"goal": "g"}, "pending_round_reset": True})
    assert run["notes"] == "n"
    assert run["state"] == {"goal": "g"}
    assert "pending_round_reset" not in run


def test_config_yaml_loads_reset_keys(tmp_path):
    from harness import config
    cfg_dir = tmp_path / "cfg"
    shutil.copytree(config.ROOT / "config", cfg_dir)
    loaded = config.load(cfg_dir, tmp_path / "data")
    assert loaded.reset_at == 0.60
    assert loaded.state_max_chars == 8000
    text = (cfg_dir / "harness.yaml").read_text(encoding="utf-8")
    (cfg_dir / "harness.yaml").write_text(
        text.replace("reset_at: 0.60", "reset_at: no").replace("state_max_chars: 8000", "state_max_chars: 12"),
        encoding="utf-8")
    loaded = config.load(cfg_dir, tmp_path / "data")
    assert loaded.reset_at == 0.60
    assert loaded.state_max_chars == 8000


def test_update_notes_alias_preserves_other_fields(tmp_path):
    script = Script([
        Completion(tool_calls=[call("update_state", 0, goal="keep me", plan="the plan",
                                    errors=[{"command": "run_fake", "args": FAIL_ARGS, "message": "nope"}],
                                    notes="old")]),
        Completion(tool_calls=[call("update_notes", 1, notes="only notes changed")]),
        Completion(content="done"),
    ])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        await m.start()
        s = await wait_status(m, m.create("alias")["id"], "done")
        state = s["run"]["state"]
        assert state["goal"] == "keep me"
        assert state["plan"] == "the plan"
        assert state["errors"][0]["command"] == "run_fake"
        assert state["notes"] == "only notes changed"
        assert s["run"]["notes"] == "only notes changed"
        assert events(m, s["id"], "state")
        await m.stop()
    asyncio.run(body())


def test_schema_rejection_writes_nothing(tmp_path):
    script = Script([
        Completion(tool_calls=[call("update_state", 0, goal="ok", plan="p")]),
        Completion(tool_calls=[call("update_state", 1, goal="wipe", extra="nope")]),
        Completion(tool_calls=[call("update_state", 2, files_modified=["x.py"], goal="no")]),
        Completion(content="done"),
    ])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        await m.start()
        s = await wait_status(m, m.create("reject")["id"], "done")
        assert s["run"]["state"]["goal"] == "ok"
        assert s["run"]["state"]["plan"] == "p"
        results = events(m, s["id"], "tool_result")
        bad = [item for item in results if item["name"] == "update_state" and not item["ok"]]
        assert len(bad) == 2
        assert all("Error:" in item["output"] for item in bad)
        await m.stop()
    asyncio.run(body())


def test_explicit_reset_keeps_dead_end_and_files_and_grounding(tmp_path):
    steps = iter([
        Completion(tool_calls=[call("write_file", 0, path="license.txt", content=PAGE)]),
        Completion(tool_calls=[call("read_file", 1, path="license.txt")]),
        Completion(tool_calls=[call("run_fake", 2, **FAIL_ARGS)]),
        Completion(tool_calls=[call("update_state", 3, goal="quote the license",
                                    errors=[{"command": "run_fake", "args": FAIL_ARGS,
                                             "message": "unknown tool"}],
                                    next_step="try different arguments")]),
        Completion(tool_calls=[call("reset_round")]),
        Completion(tool_calls=[call("run_fake", 4, **FAIL_ARGS)]),
        Completion(tool_calls=[call("run_fake", 5, cmd="other")]),
        Completion(tool_calls=[call("finish", 6, answer=(
            'Done. The file says "released under the GNU Affero General Public License".'))]),
    ])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([lambda _msgs: next(steps)]))
        await m.start()
        s = await wait_status(m, m.create("quote the license")["id"], "done")
        await asyncio.gather(*m.tasks.values())
        s = m.db.get_session(s["id"])
        ctx = s["context"]
        payload = _state_from_context(ctx)
        assert payload["goal"] == "quote the license"
        assert is_dead_end_retry(payload, "run_fake", FAIL_ARGS)
        assert not is_dead_end_retry(payload, "run_fake", {"cmd": "other"})
        assert "license.txt" in payload["files_modified"]
        _assert_tool_groups_intact(ctx)
        assert ctx[0]["role"] == "system"
        assert ctx[1]["content"] == "quote the license"
        assert ctx[3]["content"] == compaction.ROUND_CONTINUE
        resets = [item for item in events(m, s["id"], "compaction") if item["tier"] == "round_reset"]
        assert len(resets) == 1
        assert "tokens_before" in resets[0] and "tokens_after" in resets[0]
        assert resets[0]["tokens_before"] >= resets[0]["tokens_after"]
        assert "harness_round_resets_total 1" in render_metrics(m)
        assert events(m, s["id"], "quote_check") == []
        assert events(m, s["id"], "ungrounded_quotes") == []
        text = (m.cfg.transcripts_dir / f"{s['id']}.md").read_text(encoding="utf-8")
        assert "Round reset:" in text
        assert "Agent saved state" in text
        await m.stop()
    asyncio.run(body())


def test_reset_lists_files_after_commit(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "README").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "add", "README"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
                   check=True, capture_output=True)
    cfg = make_cfg(tmp_path)
    cfg.projects["repo"] = Project(name="repo", repo=str(src))
    box: dict = {}

    def commit_then_reset(_msgs):
        ws = Path(box["m"].db.get_session(box["id"])["workspace"])
        subprocess.run(["git", "-C", str(ws), "add", "a.py", "b.py"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "work"],
                       check=True, capture_output=True)
        return Completion(tool_calls=[call("reset_round")])

    steps = iter([
        Completion(tool_calls=[call("write_file", 0, path="a.py", content="a\n"),
                               call("write_file", 1, path="b.py", content="b\n")]),
        Completion(tool_calls=[call("update_state", 2, goal="ship files")]),
        commit_then_reset,
        Completion(tool_calls=[call("finish", 3, answer="done")]),
    ])

    def next_step(msgs):
        step = next(steps)
        return step(msgs) if callable(step) else step

    async def body():
        m = Manager(cfg, chat=Script([next_step]))
        box["m"] = m
        await m.start()
        s = m.create("edit and commit", project="repo")
        box["id"] = s["id"]
        s = await wait_status(m, s["id"], "done")
        await asyncio.gather(*m.tasks.values())
        payload = _state_from_context(m.db.get_session(s["id"])["context"])
        assert "a.py" in payload["files_modified"]
        assert "b.py" in payload["files_modified"]
        await m.stop()

    asyncio.run(body())


def test_threshold_without_state_falls_through(tmp_path):
    cfg = make_cfg(tmp_path, context_tokens=8000)
    cfg.reset_at = 0.50
    big = "line of output\n" * 400

    def step(msgs):
        n = sum(1 for m in msgs if m["role"] == "assistant")
        if n < 8:
            return Completion(tool_calls=[call("run_fake", n)], prompt_tokens=sum(
                compaction.message_chars(m) for m in msgs) // 3)
        return Completion(content="final", prompt_tokens=1000)

    async def body():
        m = Manager(cfg, chat=Script([step]))
        orig = m.runner._record_result

        def padded(sid, c, name, output, ok, seconds=0.0):
            orig(sid, c, name, output + big, ok, seconds)
        m.runner._record_result = padded
        await m.start()
        s = await wait_status(m, m.create("long task")["id"], "done", timeout=20)
        comp = events(m, s["id"], "compaction")
        assert not any(item["tier"] == "round_reset" for item in comp)
        assert any(item["tier"] in ("elide", "summary") for item in comp)
        assert "harness_round_resets_total 0" in render_metrics(m)
        await m.stop()
    asyncio.run(body())


def test_threshold_with_state_resets_without_elide(tmp_path):
    async def body():
        m = Manager(make_cfg(tmp_path, context_tokens=2000), chat=Script([Completion(content="done")]))
        await m.start()
        s = await wait_status(m, m.create("task")["id"], "done")
        sid = s["id"]
        state = validate_state({"goal": "g", "errors": [
            {"command": "run_fake", "args": FAIL_ARGS, "message": "nope"}]}, 8000)
        run = {**m.db.get_session(sid)["run"], "state": state, "chars_per_token": 3.0}
        padded = s["context"] + [
            {"role": "assistant", "content": "x" * 4000, "tool_calls": [call("read_file", 9, path="a")]},
            {"role": "tool", "tool_call_id": "c9-read_file", "content": "y" * 4000},
            {"role": "assistant", "content": "used it"},
        ]
        m.db.update_session(sid, context=padded, run=run)
        before = len(m.db.events(sid))
        await m.runner._maybe_compact(m.db.get_session(sid))
        s = m.db.get_session(sid)
        payload = _state_from_context(s["context"])
        assert payload["goal"] == "g"
        _assert_tool_groups_intact(s["context"])
        tiers = [e["data"]["tier"] for e in m.db.events(sid)[before:] if e["type"] == "compaction"]
        assert "round_reset" in tiers
        assert "elide" not in tiers
        assert "summary" not in tiers
        await m.stop()
    asyncio.run(body())


def test_has_valid_saved_state_predicate():
    assert not Runner.has_valid_saved_state({"run": {}})
    assert not Runner.has_valid_saved_state({"run": {"state": None}})
    assert not Runner.has_valid_saved_state({"run": {"state": {"plan": "no goal"}}})
    assert Runner.has_valid_saved_state({"run": {"state": validate_state({"goal": "g"}, 8000)}})


def test_reset_round_without_state_is_tool_error(tmp_path):
    steps = iter([
        Completion(tool_calls=[call("reset_round")]),
        Completion(tool_calls=[call("finish", 1, answer="stopped")]),
    ])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([lambda _msgs: next(steps)]))
        await m.start()
        s = await wait_status(m, m.create("reset with nothing saved")["id"], "done")
        await asyncio.gather(*m.tasks.values())
        s = m.db.get_session(s["id"])
        resets = [item for item in events(m, s["id"], "tool_result") if item["name"] == "reset_round"]
        assert len(resets) == 1
        assert resets[0]["ok"] is False
        assert "update_state" in resets[0]["output"]
        assert "pending_round_reset" not in s["run"]
        assert not any(item["tier"] == "round_reset" for item in events(m, s["id"], "compaction"))
        assert "harness_round_resets_total 0" in render_metrics(m)
        with pytest.raises(AssertionError, match="no tagged state"):
            _state_from_context(s["context"])
        await m.stop()
    asyncio.run(body())


def test_reset_round_after_rejected_state_is_tool_error(tmp_path):
    steps = iter([
        Completion(tool_calls=[call("update_state", 0, plan="no goal"), call("reset_round", 1)]),
        Completion(tool_calls=[call("finish", 2, answer="stopped")]),
    ])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([lambda _msgs: next(steps)]))
        await m.start()
        s = await wait_status(m, m.create("reject then reset")["id"], "done")
        await asyncio.gather(*m.tasks.values())
        s = m.db.get_session(s["id"])
        results = events(m, s["id"], "tool_result")
        update = [item for item in results if item["name"] == "update_state"]
        reset = [item for item in results if item["name"] == "reset_round"]
        assert update and update[0]["ok"] is False
        assert reset and reset[0]["ok"] is False
        assert "update_state" in reset[0]["output"]
        assert s["run"].get("state") is None or "state" not in s["run"]
        assert "pending_round_reset" not in s["run"]
        assert not any(item["tier"] == "round_reset" for item in events(m, s["id"], "compaction"))
        await m.stop()
    asyncio.run(body())


def test_scheduled_reset_skipped_if_state_cleared(tmp_path):
    async def body():
        m = Manager(make_cfg(tmp_path, context_tokens=2000), chat=Script([Completion(content="done")]))
        await m.start()
        s = await wait_status(m, m.create("task")["id"], "done")
        sid = s["id"]
        run = {**m.db.get_session(sid)["run"], "pending_round_reset": True, "chars_per_token": 3.0}
        run.pop("state", None)
        padded = s["context"] + [
            {"role": "assistant", "content": "x" * 4000, "tool_calls": [call("read_file", 9, path="a")]},
            {"role": "tool", "tool_call_id": "c9-read_file", "content": "y" * 4000},
            {"role": "assistant", "content": "used it"},
        ]
        m.db.update_session(sid, context=padded, run=run)
        before = len(m.db.events(sid))
        await m.runner._maybe_compact(m.db.get_session(sid))
        s = m.db.get_session(sid)
        tiers = [e["data"]["tier"] for e in m.db.events(sid)[before:] if e["type"] == "compaction"]
        assert "round_reset" not in tiers
        assert any(tier in ("elide", "summary") for tier in tiers)
        assert "pending_round_reset" not in s["run"]
        assert "harness_round_resets_total 0" in render_metrics(m)
        with pytest.raises(AssertionError, match="no tagged state"):
            _state_from_context(s["context"])
        await m.stop()
    asyncio.run(body())
