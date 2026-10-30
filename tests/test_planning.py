"""Per-turn plan re-injection (#641): `planning.reinject` appends the saved plan to a copy of each native model request.
Synthetic sessions and the scripted or fake model only; no GPU."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from harness import config as config_mod, fake_instance, fake_model, planning
from harness.api import create_app
from harness.config import PlanningConfig
from harness.llm import Completion
from harness.manager import Manager
from test_daemon import Script, call, make_cfg
from test_fake_model import fail_on_docker, wait_session
from test_phase3 import finished

STATE = {"goal": "fix the parser", "plan": "1. read parser.py\n2. fix the off-by-one\n3. run the tests",
         "errors": [], "next_step": "run pytest tests/test_parser.py", "notes": ""}


def conversation(last: dict) -> list[dict]:
    return [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "Fix the parser."},
            {"role": "assistant", "content": "", "reasoning_content": "Look first.",
             "tool_calls": [call("read_file", 0, path="parser.py")]}, last]


# the plan text
def test_plan_text_holds_the_plan_and_the_next_step():
    text = planning.plan_text(STATE)
    assert text == f"Plan:\n{STATE['plan']}\n\nNext step: {STATE['next_step']}"
    assert planning.plan_text({**STATE, "next_step": ""}) == f"Plan:\n{STATE['plan']}"
    assert planning.plan_text({**STATE, "plan": "  "}) == f"Next step: {STATE['next_step']}"


@pytest.mark.parametrize("saved", [
    None, "not a dict", {}, {"plan": "no goal"}, {**STATE, "extra": 1}, {**STATE, "plan": 3},
    {**STATE, "plan": "", "next_step": ""}, {"goal": "only a goal"}, {**STATE, "plan": " \n", "next_step": "\t"},
])
def test_no_valid_state_or_no_plan_means_no_text(saved):
    assert planning.plan_text(saved) == ""
    messages = conversation({"role": "tool", "tool_call_id": "c0-read_file", "content": "OUT"})
    assert planning.with_plan(messages, saved) is messages


def test_the_text_is_capped_at_1200_characters_keeping_both_ends():
    saved = {**STATE, "plan": "P" * 5000, "next_step": "the very next step"}
    text = planning.plan_text(saved)
    assert len(text) == planning.MAX_CHARS == 1200
    assert text.startswith("Plan:\nPPP") and text.endswith("Next step: the very next step")
    assert planning.ELLIPSIS in text
    near = {**STATE, "plan": "p" * (1200 - len("Plan:\n")), "next_step": ""}
    assert planning.plan_text(near) == "Plan:\n" + "p" * 1194  # exactly at the cap: kept whole


# placement
def test_the_plan_ends_the_last_tool_result_in_a_copy():
    messages = conversation({"role": "tool", "tool_call_id": "c0-read_file", "content": "OUT"})
    stored = copy.deepcopy(messages)
    sent = planning.with_plan(messages, STATE)
    assert messages == stored  # the stored context is untouched
    assert [m["role"] for m in sent] == [m["role"] for m in messages] and len(sent) == len(messages)
    assert sent[:-1] == messages[:-1]
    assert sent[-1]["content"] == f"OUT\n\n{planning.MARKER}\n{planning.plan_text(STATE)}"
    assert sent[-1]["tool_call_id"] == "c0-read_file"
    assert sent[2]["reasoning_content"] == "Look first."  # the assistant's reasoning still goes out
    sent[2]["tool_calls"][0]["id"] = "changed"  # a deep copy: nothing in it is shared with the stored context
    assert messages[2]["tool_calls"][0]["id"] == "c0-read_file"


def test_on_the_first_call_the_plan_ends_the_users_message():
    messages = [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "Carry on."}]
    sent = planning.with_plan(messages, STATE)
    assert sent[-1] == {"role": "user", "content": f"Carry on.\n\n{planning.MARKER}\n{planning.plan_text(STATE)}"}
    assert messages[-1]["content"] == "Carry on."


def test_content_parts_get_one_more_text_part():
    parts = [{"type": "text", "text": "Look at this."}, {"type": "image_url", "image_url": {"url": "data:,"}}]
    messages = [{"role": "user", "content": parts}]
    sent = planning.with_plan(messages, STATE)
    assert sent[0]["content"][:2] == parts and len(parts) == 2
    assert sent[0]["content"][2] == {"type": "text", "text": f"{planning.MARKER}\n{planning.plan_text(STATE)}"}


def test_a_message_without_text_content_is_left_alone():
    messages = [{"role": "assistant", "content": None, "tool_calls": [call("read_file")]}]
    assert planning.with_plan(messages, STATE) is messages
    assert planning.with_plan([], STATE) == []


@pytest.mark.parametrize("text", ["Run the stuck loop.", "", "a\n\nb"])
def test_strip_undoes_the_block(text):
    assert planning.strip(text + planning.block("Plan:\nx")) == text
    assert planning.strip(text) == text
    joined = " ".join([text, planning.block("Plan:\nx").lstrip("\n")])  # the fake model joins content parts
    assert planning.strip(joined) == text


# config
def write_config(tmp_path: Path, planning_section) -> tuple[Path, Path]:
    config_dir, data_dir = fake_instance.write_instance(tmp_path / "instance", 0)
    path = config_dir / "harness.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if planning_section is not None:
        raw["planning"] = planning_section
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return config_dir, data_dir


def test_the_flag_is_off_by_default_and_loads_when_set(tmp_path):
    assert PlanningConfig().reinject is False
    assert config_mod.load(*write_config(tmp_path / "a", None)).planning.reinject is False
    assert config_mod.load(*write_config(tmp_path / "b", {})).planning.reinject is False
    assert config_mod.load(*write_config(tmp_path / "c", {"reinject": True})).planning.reinject is True
    assert config_mod.load(*write_config(tmp_path / "d", {"reinject": False})).planning.reinject is False


@pytest.mark.parametrize("section, message", [
    ({"reinject": "yes"}, "planning.reinject must be true or false"),
    ({"reinject": 1}, "planning.reinject must be true or false"),
    ({"reinject": True, "cap": 10}, "unknown key 'cap'"),
    ([True], "planning must be a mapping"),
])
def test_a_bad_planning_section_fails_the_load(tmp_path, section, message):
    with pytest.raises(ValueError, match=message):
        config_mod.load(*write_config(tmp_path, section))


# the runner
def run_session(tmp_path: Path, reinject: bool, kind: str = "agent") -> tuple[Script, dict, list]:
    """One native session: save state, read a file, answer. Returns what the model got and what was stored."""
    script = Script([
        Completion(tool_calls=[call("update_state", 0, **STATE)]),
        Completion(tool_calls=[call("update_state", 1, **{**STATE, "next_step": "answer"})]),
        Completion(content="done"),
    ])

    async def body():
        cfg = make_cfg(tmp_path)
        cfg.planning = PlanningConfig(reinject=reinject)
        m = Manager(cfg, chat=script)
        await m.start(maintenance=False)
        s = await finished(m, m.create("Fix the parser.", project="scratch", kind=kind)["id"])
        events = m.db.events(s["id"])
        await m.stop()
        return s, events

    s, events = asyncio.run(body())
    assert s["status"] == "done", s
    return script, s, events


def test_with_the_flag_on_the_model_gets_the_plan_and_the_context_does_not(tmp_path):
    script, s, events = run_session(tmp_path, reinject=True)
    first, second, third = script.requests
    assert planning.MARKER not in json.dumps(first)  # no state saved yet
    assert second[-1]["role"] == "tool"
    assert second[-1]["content"].endswith(f"\n\n{planning.MARKER}\n{planning.plan_text(STATE)}")
    assert third[-1]["content"].endswith(f"{planning.MARKER}\nPlan:\n{STATE['plan']}\n\nNext step: answer")
    assert planning.MARKER not in json.dumps(s["context"]) and planning.MARKER not in json.dumps(events)
    # the request is the stored context with the plan block on its last message
    stored = s["context"][:len(third)]
    assert third[:-1] == stored[:-1] and third[-1]["content"] == stored[-1]["content"] + planning.block(
        planning.plan_text({**STATE, "next_step": "answer"}))
    system = s["context"][0]["content"]
    assert system.count(planning.PROMPT) == 1 and all(r[0]["content"] == system for r in script.requests)


def test_with_the_flag_off_the_requests_are_the_stored_context_byte_for_byte(tmp_path):
    script, s, _ = run_session(tmp_path, reinject=False)
    assert len(script.requests) == 3
    for request in script.requests:
        assert json.dumps(request) == json.dumps(s["context"][:len(request)])
    assert planning.PROMPT not in s["context"][0]["content"]


def test_with_the_flag_off_the_stored_context_object_goes_out(tmp_path):
    cfg = make_cfg(tmp_path)
    m = Manager(cfg, chat=Script([]))
    try:
        s = {"kind": "agent", "context": conversation({"role": "tool", "content": "OUT"}), "run": {"state": STATE}}
        assert m.runner._outgoing(s) is s["context"]
        cfg.planning = PlanningConfig(reinject=True)
        assert m.runner._outgoing(s) is not s["context"]
        assert m.runner._outgoing({**s, "kind": "chat"}) is s["context"]  # agent sessions only
    finally:
        m.db.close()


def test_chat_sessions_never_get_the_prompt_sentence(tmp_path):
    script = Script([Completion(content="hello")])

    async def body():
        cfg = make_cfg(tmp_path)
        cfg.planning = PlanningConfig(reinject=True)
        m = Manager(cfg, chat=script)
        await m.start(maintenance=False)
        s = await finished(m, m.create("hi", project="scratch", kind="chat")["id"])
        await m.stop()
        return s

    s = asyncio.run(body())
    assert planning.PROMPT not in s["context"][0]["content"]


# fake model mode, end to end
PLAN_SCRIPT = """
context_tokens: 16384
scripts:
  - name: plan
    match: "plan task"
    turns:
      - text: "Saving my plan."
        tool_calls: [{name: update_state, args: {goal: "write the notes", plan: "1. write notes.md\\n2. answer",
                                                 next_step: "write notes.md"}}]
      - text: "Writing."
        tool_calls: [{name: write_file, args: {path: notes.md, content: "notes"}}]
      - text: "Done."
"""


def test_fake_model_receives_the_plan_and_nothing_stores_it(monkeypatch, tmp_path):
    from harness import sandbox
    monkeypatch.setattr(sandbox, "run_cmd", fail_on_docker)
    config_dir, data_dir = write_config(tmp_path, {"reinject": True})
    fixture = tmp_path / "plan.yaml"
    fixture.write_text(PLAN_SCRIPT, encoding="utf-8")
    monkeypatch.setenv(fake_model.ENV, str(fixture))
    requests: list[list[dict]] = []
    real_call = fake_model.FakeModel.__call__

    async def recording(self, model_name, messages, *args, **kwargs):
        requests.append(json.loads(json.dumps(messages)))
        return await real_call(self, model_name, messages, *args, **kwargs)
    monkeypatch.setattr(fake_model.FakeModel, "__call__", recording)
    m = Manager(config_mod.load(config_dir, data_dir))
    try:
        _, token = m.db.main.create_api_key("ui-testing", "admin", "owner")
        headers = {"Authorization": f"Bearer {token}", **fake_instance.CLIENT}
        with TestClient(create_app(m)) as client:
            made = client.post("/api/v1/sessions", headers=headers,
                               json={"prompt": "Run the plan task.", "project": fake_instance.PROJECT})
            assert made.status_code == 201, made.text
            sid = made.json()["id"]
            done = wait_session(client, sid, headers, "done", "failed")
        events = json.dumps(m.db.events(sid))
        assert (done["status"], done["answer"]) == ("done", "Done."), done
        plan = "Plan:\n1. write notes.md\n2. answer\n\nNext step: write notes.md"
        assert len(requests) == 3 and planning.MARKER not in json.dumps(requests[0])
        for request in requests[1:]:
            assert request[-1]["role"] == "tool" and request[-1]["content"].endswith(f"\n\n{planning.MARKER}\n{plan}")
        assert planning.PROMPT in requests[0][0]["content"]
        context = m.db.get_session(sid)["context"]
        assert planning.MARKER not in json.dumps(context) and planning.MARKER not in events
        stored = [p for p in m.cfg.transcripts_dir.rglob("*") if p.is_file()]
        assert all(planning.MARKER not in p.read_text(encoding="utf-8", errors="replace") for p in stored)
    finally:
        m.db.close()


# the canary comparison's flag-on arm
def test_the_standalone_canary_can_turn_the_flag_on(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from bakeoff import canary
    from harness.canary import Report
    seen = {}

    async def fake_run(task_ids, repeats, fixture, heldout=False, plan_reinject=False, **_):
        seen["cli"] = plan_reinject
    monkeypatch.setattr(canary, "run_standalone", fake_run)
    canary.main(["--plan-reinject", "--tasks", "sqlite_report"])
    assert seen == {"cli": True}
    monkeypatch.undo()

    class FakeManager:
        def __init__(self, cfg):
            self.cfg, self.db = cfg, SimpleNamespace(close=lambda: None)

        async def start(self, maintenance=True):
            pass

        async def stop(self):
            pass

    async def fake_runner(self, sha, only):
        seen.setdefault("reinject", []).append(self.m.cfg.planning.reinject)
        return Report("complete", [])

    base = SimpleNamespace(default_model="m", models={}, sandbox=None, canary=config_mod.CanaryConfig(),
                           web=SimpleNamespace(page_chars=1000), planning=PlanningConfig())
    monkeypatch.setattr(config_mod, "load", lambda: base)
    monkeypatch.setattr(config_mod, "Config", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr("harness.manager.Manager", FakeManager)
    monkeypatch.setattr(canary.CanaryRunner, "run", fake_runner)
    monkeypatch.setattr(canary, "ROOT", tmp_path)
    off = asyncio.run(canary.run_standalone(["sqlite_report"], 1, tmp_path))
    on = asyncio.run(canary.run_standalone(["sqlite_report"], 1, tmp_path, plan_reinject=True))
    base.planning = PlanningConfig(reinject=True)  # harness.yaml's own setting also counts
    from_yaml = asyncio.run(canary.run_standalone(["sqlite_report"], 1, tmp_path))
    assert seen["reinject"] == [False, True, True]
    assert off.name.startswith("canary-2") and on.name.startswith("canary-plan-")
    assert from_yaml.name.startswith("canary-plan-")


# forks
def test_with_prompt_adds_the_sentence_once_and_removes_it():
    on = planning.with_prompt("SYSTEM", True)
    assert on == f"SYSTEM\n\n{planning.PROMPT}" and planning.with_prompt(on, True) == on
    assert planning.with_prompt(on, False) == "SYSTEM" and planning.with_prompt("SYSTEM", False) == "SYSTEM"


def test_a_fork_has_the_sentence_only_where_it_runs_natively(tmp_path):
    """A fork inherits its parent's prompt; the sentence follows the backend the fork runs on, in its context and in
    its saved checkpoint."""
    from test_backend_override import started

    def sentences(s: dict) -> tuple[int, int]:
        saved, _ = m.runner.checkpointer.store(s).load_turn(1)
        return s["context"][0]["content"].count(planning.PROMPT), saved[0]["content"].count(planning.PROMPT)

    async def body():
        nonlocal m
        m, s = await started(tmp_path)  # made with the flag off
        assert planning.PROMPT not in s["context"][0]["content"]
        m.cfg.planning = PlanningConfig(reinject=True)
        local = await finished(m, (await m.fork(s["id"], 1, "carry on"))["id"])
        hosted = m.db.get_session((await m.fork(local["id"], 1, "on codex", backend="codex"))["id"])
        await finished(m, hosted["id"])
        m.db.update_session(s["id"], backend="claude", model="claude-opus-5", effort="high")
        back = m.db.get_session((await m.fork(s["id"], 1, "back home", backend="local"))["id"])
        await finished(m, back["id"])
        await m.stop()
        return local, hosted, back

    m = None
    local, hosted, back = asyncio.run(body())
    assert sentences(local) == (1, 1)    # a local fork made with the flag on
    assert sentences(hosted) == (0, 0)   # local to hosted: dropped
    assert sentences(back) == (1, 1)     # hosted to local: added
