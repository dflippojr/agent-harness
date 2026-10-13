"""Fake model mode (#588): fixtures, the scripted chat, the opt-in guard and a full-stack run. Nothing needs the GPU."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from harness import compaction, config as config_mod, fake_instance, fake_model
from harness.api import create_app
from harness.llm import LLMError
from harness.local_inference import NoWarmer
from harness.manager import Manager
from harness.runner import CONTINUE_NUDGE
from waits import scaled

ROOT = Path(__file__).resolve().parent.parent
TOOLS = [{"type": "function", "function": {"name": "write_file", "parameters": {}}}]


@pytest.fixture
def fake_manager(monkeypatch, tmp_path):
    """A Manager on a fresh disposable-instance config in fake mode; its databases are closed afterwards."""
    config_dir, data_dir = fake_instance.write_instance(tmp_path / "instance", 0)
    monkeypatch.setenv(fake_model.ENV, str(fake_model.DEFAULT_FIXTURES))
    m = Manager(config_mod.load(config_dir, data_dir))
    yield m
    m.db.close()


def fixtures(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "fixtures.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def model(text: str, tmp_path: Path) -> fake_model.FakeModel:
    return fake_model.FakeModel.from_path(fixtures(tmp_path, text))


def run(coro):
    return asyncio.run(coro)


SCRIPT = """
context_tokens: 8192
scripts:
  - name: edit
    match: "edit"
    turns:
      - text: "Writing."
        reasoning: "Plan."
        tool_calls: [{name: write_file, args: {path: a.md, content: "x"}}]
      - text: "Done."
  - name: exact
    prompt: "Exactly this"
    turns: [{text: "exact match"}]
"""


# fixtures
def test_default_fixtures_load_and_cover_the_redesign_surfaces():
    loaded = fake_model.load_fixtures(fake_model.DEFAULT_FIXTURES)
    names = {s.name for s in loaded.scripts}

    def picked(prompt: str) -> str:
        script = loaded.find(prompt)
        return script.name if script else ""
    assert {"tab-bar-tour", "tool-rows", "approval-sheet", "approval-batch", "slow-running", "model-retry",
            "model-failure", "compaction", "nightly-report"} <= names
    # Every seeded prompt and job reaches the script it is meant for.
    seeded = {prompt: picked(prompt) for _, prompt, _ in fake_instance.SEED_SESSIONS}
    assert seeded == {
        "Hello! Give me a quick tour of the tab bar.": "tab-bar-tour",
        "Show tool rows: write, read and search some files.": "tool-rows",
        "Simulate a retry after a model error.": "model-retry",
        "Simulate a failure.": "model-failure",
        "Run a long task that triggers compaction.": "compaction",
        "Edit the protected settings file.": "approval-sheet",
    }
    assert picked("Run a slow task so I can watch it running.") == "slow-running"
    assert {picked(job["prompt"]) for job in fake_instance.SEED_JOBS} == {"nightly-report"}
    # The harness ends a run on a reply without tool calls: only a script's last turn may lack them.
    for script in loaded.scripts:
        for turn in script.turns[:-1]:
            assert turn.tool_calls, f"{script.name}: a turn before the last has no tool calls"


@pytest.mark.parametrize("text,message", [
    ("scripts: [{name: Bad Name, match: x, turns: [{text: hi}]}]", "lowercase slug"),
    ("scripts: [{name: a, turns: [{text: hi}]}]", "exactly one of prompt"),
    ("scripts: [{name: a, match: x, prompt: y, turns: [{text: hi}]}]", "exactly one of prompt"),
    ("scripts: [{name: a, match: x, turns: []}]", "at least one turn"),
    ("scripts: [{name: a, match: x, turns: [{txt: hi}]}]", "unknown keys"),
    ("scripts: [{name: a, match: x, turns: [{text: hi, delay: 99}]}]", "0 to 60"),
    ("scripts: [{name: a, match: '(', turns: [{text: hi}]}]", "match"),
    ("scripts: [{name: a, match: x, turns: [{error: {retryable: true}}]}]", "needs a message"),
    ("scripts: [{name: a, match: x, turns: [{error: {message: m, retryable: true, times: 4}}]}]", "must be 1 to 3"),
    ("scripts: [{name: a, match: x, turns: [{error: {message: m, times: 2}}]}]", "drop times"),
    ("scripts: [{name: a, match: x, turns: [{tool_calls: [{args: {}}]}]}]", "needs a name"),
    ("context_tokens: 10\nscripts: []", "context_tokens"),
    ("scripts:\n  - {name: a, match: x, turns: [{text: hi}]}\n  - {name: a, match: y, turns: [{text: hi}]}",
     "defined twice"),
])
def test_fixture_errors_name_the_problem(tmp_path, text, message):
    with pytest.raises(fake_model.FixtureError, match=message):
        fake_model.load_fixtures(fixtures(tmp_path, text))


def test_missing_fixture_dir_is_an_error(tmp_path):
    with pytest.raises(fake_model.FixtureError, match="no fixture files"):
        fake_model.load_fixtures(tmp_path / "absent")


# the scripted chat
def test_turns_stream_and_tool_calls_continue_the_script(tmp_path):
    fake = model(SCRIPT, tmp_path)
    deltas, progress = [], []

    async def on_delta(kind, text):
        deltas.append((kind, text))

    async def on_progress(processed, total, cache):
        progress.append((processed, total, cache))
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Please edit a file"}]
    first = run(fake(None, messages, TOOLS, on_delta, on_progress=on_progress))
    assert (first.content, first.reasoning, first.finish_reason) == ("Writing.", "Plan.", "tool_calls")
    assert [k for k, _ in deltas] == ["reasoning", "content"]
    assert progress[-1][0] == progress[-1][1] > 0 and first.prompt_tokens == progress[-1][1]
    [call] = first.tool_calls
    assert call["id"].startswith("fake-edit-0-0-") and fake_model.CALL_ID.match(call["id"])
    # Ids are unique per call, even for the same context: the runner matches approvals and results by id.
    assert run(fake(None, messages, TOOLS)).tool_calls[0]["id"] != call["id"]
    assert json.loads(call["function"]["arguments"]) == {"path": "a.md", "content": "x"}
    messages += [{"role": "assistant", "content": first.content, "tool_calls": first.tool_calls},
                 {"role": "tool", "tool_call_id": call["id"], "content": "denied"}]
    second = run(fake(None, messages, TOOLS))
    assert (second.content, second.tool_calls, second.finish_reason) == ("Done.", [], "stop")


def test_a_compaction_summary_or_nudge_keeps_the_scripts_place(tmp_path):
    fake = model(SCRIPT, tmp_path)
    call = {"id": "fake-edit-0-0-0123456789ab", "type": "function",
            "function": {"name": "write_file", "arguments": "{}"}}
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "edit it"},
                {"role": "user", "content": f"{compaction.SUMMARY_TAG} Earlier work, condensed"},
                {"role": "assistant", "content": "", "tool_calls": [call]},
                {"role": "tool", "tool_call_id": call["id"], "content": "ok"},
                {"role": "user", "content": CONTINUE_NUDGE}]
    assert run(fake(None, messages, TOOLS)).content == "Done."
    # Someone's own unmatched message is not a nudge: the script doesn't resume past it.
    messages[-1] = {"role": "user", "content": "What time is it?"}
    assert "No fixture script matches" in run(fake(None, messages, TOOLS)).content


def test_follow_ups_pick_a_script_again_and_unmatched_ones_fall_back(tmp_path):
    fake = model(SCRIPT, tmp_path)
    done = [{"role": "system", "content": "sys"}, {"role": "user", "content": "edit"},
            {"role": "assistant", "content": "Done."}]
    assert run(fake(None, done + [{"role": "user", "content": "Exactly this"}], TOOLS)).content == "exact match"
    fallback = run(fake(None, done + [{"role": "user", "content": "something else"}], TOOLS)).content
    assert "No fixture script matches" in fallback and "edit, exact" in fallback
    # `prompt` is exact: a longer message doesn't match it.
    assert "No fixture" in run(fake(None, [{"role": "user", "content": "Exactly this, please"}], TOOLS)).content


def test_an_exhausted_script_ends_the_run(tmp_path):
    fake = model(SCRIPT, tmp_path)
    call = {"id": "fake-edit-1-0-0123456789ab", "type": "function",
            "function": {"name": "write_file", "arguments": "{}"}}
    out = run(fake(None, [{"role": "user", "content": "edit"}, {"role": "assistant", "content": "",
                                                                 "tool_calls": [call]}], TOOLS))
    assert "has no turn 3" in out.content and not out.tool_calls


def test_errors_fail_the_given_number_of_times_then_answer(tmp_path):
    fake = model("""
scripts:
  - name: flaky
    match: flaky
    turns: [{error: {message: "HTTP 500: boom", retryable: true, times: 2}, text: "recovered"}]
  - name: fatal
    match: fatal
    turns: [{error: {message: "HTTP 400: no"}}]
""", tmp_path)
    messages = [{"role": "user", "content": "flaky"}]
    async def retries():
        for _ in range(2):
            with pytest.raises(LLMError, match="boom") as raised:
                await fake(None, messages, TOOLS)
            assert raised.value.retryable
        assert (await fake(None, messages, TOOLS)).content == "recovered"
        # Success resets the sequence even if the caller reuses its task and messages.
        with pytest.raises(LLMError, match="boom"):
            await fake(None, messages, TOOLS)

    run(retries())
    fatal = [{"role": "user", "content": "fatal"}]
    for _ in range(2):   # a fatal error fails every time, so a rerun of the failed session fails the same way
        with pytest.raises(LLMError) as raised:
            run(fake(None, fatal, TOOLS))
        assert not raised.value.retryable


async def assert_retry_sequence(fake, messages, times):
    for _ in range(times):
        with pytest.raises(LLMError, match="boom") as raised:
            await fake(None, messages, TOOLS)
        assert raised.value.retryable
    assert (await fake(None, messages, TOOLS)).content == "recovered"


def test_cancelled_delayed_error_does_not_consume_a_fresh_requests_errors(monkeypatch, tmp_path):
    fake = model("""scripts: [{name: flaky, match: flaky, turns:
        [{wait: 60, error: {message: boom, retryable: true, times: 1}, text: recovered}]}]""", tmp_path)
    messages = [{"role": "user", "content": "flaky"}]

    async def scenario():
        entered = asyncio.Event()
        block = True

        async def delay(seconds):
            assert seconds == 60
            if block:
                entered.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(fake_model.asyncio, "sleep", delay)
        abandoned = asyncio.create_task(fake(None, messages, TOOLS))
        try:
            await asyncio.wait_for(entered.wait(), scaled(5))
        finally:
            abandoned.cancel()
            with pytest.raises(asyncio.CancelledError):
                await abandoned
        block = False
        assert not fake._failures
        await assert_retry_sequence(fake, messages, 1)
        # Keep a strong reference to the cancelled task: cleanup must not wait for garbage collection.
        assert abandoned.cancelled()
        assert not any(fake._failures.values())

    run(scenario())


@pytest.mark.parametrize("ending", ["cancel", "return", "error"])
def test_abandoned_retry_backoff_is_cleaned_up(monkeypatch, fake_manager, tmp_path, ending):
    fake = model("""scripts: [{name: flaky, match: flaky, turns:
        [{error: {message: boom, retryable: true, times: 2}, text: recovered}]}]""", tmp_path)
    messages = [{"role": "user", "content": "flaky"}]
    runner = fake_manager.runner
    runner.chat = fake
    retries = []

    async def capture(_sid, kind, data):
        if kind == "llm_retry":
            retries.append(data["attempt"])

    monkeypatch.setattr(runner.bus, "aemit", capture)
    stream = SimpleNamespace(on_delta=None, flush=lambda: None)

    async def scenario():
        entered = asyncio.Event()

        async def backoff(seconds):
            if seconds == 2:
                entered.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(fake_model.asyncio, "sleep", backoff)

        async def request():
            try:
                await runner._call_with_retries("synthetic", None, messages, TOOLS, stream, None,
                                               {"invalid_tool_calls": 0})
            except asyncio.CancelledError:
                if ending == "cancel":
                    raise
                if ending == "error":
                    raise RuntimeError("run terminated") from None

        abandoned = asyncio.create_task(request())
        try:
            await asyncio.wait_for(entered.wait(), scaled(5))
            assert retries == [1, 2]
        finally:
            abandoned.cancel()
            if ending == "return":
                await abandoned
            else:
                error = asyncio.CancelledError if ending == "cancel" else RuntimeError
                with pytest.raises(error):
                    await abandoned
        assert not fake._failures
        await assert_retry_sequence(fake, messages, 2)
        assert abandoned.done() and not any(fake._failures.values())

    run(scenario())


@pytest.mark.parametrize("times", [1, 3])
def test_identical_requests_have_independent_interleaved_retries(tmp_path, times):
    fake = model(f"""scripts: [{{name: flaky, match: flaky, turns:
        [{{error: {{message: boom, retryable: true, times: {times}}}, text: recovered}}]}}]""", tmp_path)
    messages = [{"role": "user", "content": "flaky"}]

    async def scenario():
        barrier = asyncio.Barrier(2)

        async def request():
            for _ in range(times):
                with pytest.raises(LLMError, match="boom"):
                    await fake(None, messages, TOOLS)
                await barrier.wait()
            assert (await fake(None, messages, TOOLS)).content == "recovered"

        await asyncio.wait_for(asyncio.gather(request(), request()), scaled(5))
        assert not fake._failures

    run(scenario())


def test_summary_requests_get_the_fixture_summary_and_no_tool_calls(tmp_path):
    fake = model(SCRIPT + 'summary: "Handoff notes."\n', tmp_path)
    request = compaction.summary_request([{"role": "system", "content": "s"}, {"role": "user", "content": "edit"},
                                          {"role": "assistant", "content": "a"}, {"role": "user", "content": "b"}],
                                         2, 4, 10000)
    out = run(fake(None, request, None))
    assert out.content == "Handoff notes." and not out.tool_calls
    # Any request without tools (a delegate edit, a skill review, Chat without web tools) gets text only.
    assert run(fake(None, [{"role": "user", "content": "edit"}], None)).tool_calls == []
    chat = run(fake(None, [{"role": "user", "content": "edit"}], []))
    assert chat.tool_calls == [] and chat.finish_reason == "stop"


def test_filler_makes_a_long_reply(tmp_path):
    fake = model("scripts: [{name: long, match: long, turns: [{text: Start., filler_chars: 5000}]}]", tmp_path)
    chunks = []

    async def on_delta(_kind, text):
        chunks.append(text)
    out = run(fake(None, [{"role": "user", "content": "long"}], TOOLS, on_delta))
    assert out.content.startswith("Start.") and len(out.content) == len("Start.") + 2 + 5000 and len(chunks) > 2


def test_malformed_arguments_reach_the_harness_as_written(tmp_path):
    fake = model("""scripts: [{name: bad, match: bad, turns: [{tool_calls: [{name: write_file, args: '{"path": '}]},
                                                              {text: ok}]}]""", tmp_path)
    [call] = run(fake(None, [{"role": "user", "content": "bad"}], TOOLS)).tool_calls
    assert call["function"]["arguments"] == '{"path": '


# opt-in: only a disposable instance, never by accident
def test_production_config_never_runs_the_fake(monkeypatch, tmp_path):
    monkeypatch.delenv(fake_model.ENV, raising=False)
    cfg = config_mod.load(ROOT / "config", tmp_path / "data")
    assert cfg.fake_model is None and fake_model.MODEL_NAME not in cfg.models
    assert Manager(cfg).runner.chat.__module__ == "harness.llm"
    # The variable leaking into the real service's environment refuses to start, rather than swapping the model.
    monkeypatch.setenv(fake_model.ENV, str(fake_model.DEFAULT_FIXTURES))
    with pytest.raises(fake_model.FakeModeRefused, match="not a disposable"):
        config_mod.load(ROOT / "config", tmp_path / "data")


def test_no_config_file_can_turn_it_on(monkeypatch, tmp_path):
    monkeypatch.delenv(fake_model.ENV, raising=False)
    config_dir, data_dir = fake_instance.write_instance(tmp_path, 0)
    path = config_dir / "harness.yaml"
    path.write_text(path.read_text(encoding="utf-8") + f"fake_model: {fake_model.DEFAULT_FIXTURES.as_posix()}\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match="HARNESS_FAKE_MODEL only"):
        config_mod.load(config_dir, data_dir)


def test_no_service_script_sets_the_variable():
    for folder in ("ops", "install", "config", ".github"):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and path.suffix in (".ps1", ".sh", ".yaml", ".yml", ".py", ".cmd", ".bat", ".json"):
                assert fake_model.ENV not in path.read_text(encoding="utf-8", errors="replace"), path


def test_apply_drops_everything_that_touches_the_gpu(fake_manager):
    m = fake_manager
    cfg = m.cfg
    assert cfg.fake_model == fake_model.DEFAULT_FIXTURES
    assert list(cfg.models) == [fake_model.MODEL_NAME] and cfg.default_model == fake_model.MODEL_NAME
    assert cfg.models["fake"].base_url == fake_model.UNROUTABLE
    assert cfg.modules.local_model and not any(getattr(cfg.modules, n) for n in fake_model.GPU_SWITCHES)
    assert not cfg.gpu_guard.enabled and not set(cfg.module_packages or ()) & set(fake_model.GPU_PACKAGES)
    assert isinstance(m.runner.chat, fake_model.FakeModel)
    assert m.guard is None and m.runner.guard is None and isinstance(m.runner.warmer, NoWarmer)
    assert "local_model" not in m.modules and "endpoint" not in m.modules and "images" not in m.modules


def test_maintenance_never_removes_containers_from_a_fake_instance(monkeypatch, fake_manager):
    """The Docker engine is shared with the real harness, whose session containers this instance doesn't know."""
    from harness import maintenance
    m = fake_manager
    calls = []

    async def docker(args, *rest, **kwargs):
        calls.append(args)
        return 0, '{"Names": "harness-live", "State": "running", "Labels": "agent-harness.session=live1"}', ""
    monkeypatch.setattr(maintenance, "run_cmd", docker)
    report = {"containers_removed": []}
    run(m.maintenance._containers(time.time(), report))
    assert calls == [] and report["containers_removed"] == []


def test_disk_usage_without_docker_or_in_fake_mode_lists_no_containers(monkeypatch, fake_manager):
    from harness import maintenance
    m = fake_manager
    calls = []

    async def no_docker(args, *_rest, **_kwargs):
        calls.append(args)
        raise FileNotFoundError("docker")
    monkeypatch.setattr(maintenance, "run_cmd", no_docker)
    assert run(m.maintenance._container_sizes()) == [] and calls == []   # fake mode: the real harness's
    m.cfg.fake_model = None
    assert run(m.maintenance._container_sizes()) == [] and calls          # no Docker CLI: none, no error


def test_a_bad_fixture_fails_the_start(monkeypatch, tmp_path):
    config_dir, data_dir = fake_instance.write_instance(tmp_path, 0)
    monkeypatch.setenv(fake_model.ENV, str(fixtures(tmp_path, "scripts: [{name: x}]")))
    with pytest.raises(fake_model.FixtureError):
        config_mod.load(config_dir, data_dir)


def wait_session(client, sid, headers, *statuses):
    deadline, s = time.monotonic() + scaled(30), {"status": "unknown"}
    while time.monotonic() < deadline:
        s = client.get(f"/api/v1/sessions/{sid}", headers=headers).json()
        if s["status"] in statuses:
            return s
        time.sleep(0.05)
    raise AssertionError(f"session {sid} stayed {s['status']}, wanted {statuses}")


def test_fake_mode_never_touches_docker(monkeypatch, fake_manager, tmp_path):
    """The Docker engine is shared with the real harness: a fake session's shell is a tool error, and its stop and
    remove do nothing."""
    from harness import sandbox
    from harness.fileops import ToolError
    monkeypatch.setattr(sandbox, "run_cmd", fail_on_docker)
    session = {"id": "s1", "target": "tower", "workspace": str(tmp_path), "project": "ui", "owner_id": "owner"}
    sb = fake_manager.runner.sandbox(session)
    assert isinstance(sb, sandbox.NoDockerSandbox)
    for call in (sb.exec("echo hi"), sb.ensure_running()):
        with pytest.raises(ToolError, match="sandbox is off in fake model mode"):
            run(call)
    run(sb.stop())
    run(sb.restart())
    run(sb.remove())
    assert not run(sb.stop_if_idle())
    sb.arm_idle_stop()


async def fail_on_docker(args, *_rest, **_kwargs):
    raise AssertionError(f"fake mode ran a docker command: {args}")


def test_full_stack_approval_and_compaction(monkeypatch, fake_manager):
    """The real server with the scripted model, never calling Docker: an approval the policy asks for, then a run that
    compacts."""
    from harness import sandbox
    monkeypatch.setattr(sandbox, "run_cmd", fail_on_docker)
    m = fake_manager
    _, token = m.db.main.create_api_key("ui-testing", "admin", "owner")
    headers = {"Authorization": f"Bearer {token}", **fake_instance.CLIENT}
    with TestClient(create_app(m)) as client:
        made = client.post("/api/v1/sessions", headers=headers,
                           json={"prompt": "Edit the protected settings file.", "project": fake_instance.PROJECT})
        assert made.status_code == 201, made.text
        sid = made.json()["id"]
        wait_session(client, sid, headers, "waiting_approval")
        [approval] = client.get(f"/api/v1/sessions/{sid}/approvals", headers=headers).json()
        assert approval["tool"] == "write_file" and approval["tool_call_id"].startswith("fake-approval-sheet-0-0-")
        decided = client.post(f"/api/v1/sessions/{sid}/approvals/{approval['id']}", headers=headers,
                              json={"decision": "approve"})
        assert decided.status_code == 200, decided.text
        done = wait_session(client, sid, headers, "done", "failed")
        assert done["status"] == "done", done
        workspace = Path(m.db.get_session(sid)["workspace"])
        assert json.loads((workspace / "protected" / "settings.json").read_text(encoding="utf-8"))["theme"] == "system"

        made = client.post("/api/v1/sessions", headers=headers, json={
            "prompt": "Run a long task that triggers compaction.", "project": fake_instance.PROJECT})
        sid = made.json()["id"]
        assert wait_session(client, sid, headers, "done", "failed")["status"] == "done"
        tiers = [e["data"]["tier"] for e in m.db.events(sid) if e["type"] == "compaction"]
        assert "summary" in tiers
        assert m.db.get_session(sid)["answer"].startswith("Done.")


def test_full_stack_stuck_loop_nudges_then_ends_the_run(monkeypatch, fake_manager):
    """#643: the scripted model repeats one failing read; the third result carries the nudge and the eighth ends the
    run with stuck_repeat and a `stuck` event."""
    from harness import sandbox
    monkeypatch.setattr(sandbox, "run_cmd", fail_on_docker)
    m = fake_manager
    _, token = m.db.main.create_api_key("ui-testing", "admin", "owner")
    headers = {"Authorization": f"Bearer {token}", **fake_instance.CLIENT}
    with TestClient(create_app(m)) as client:
        made = client.post("/api/v1/sessions", headers=headers,
                           json={"prompt": "Run the stuck loop.", "project": fake_instance.PROJECT})
        assert made.status_code == 201, made.text
        sid = made.json()["id"]
        done = wait_session(client, sid, headers, "done", "failed")
    assert (done["status"], done["stop_reason"]) == ("done", "stuck_repeat"), done
    events = m.db.events(sid)
    results = [e["data"] for e in events if e["type"] == "tool_result"]
    assert len(results) == 8 and not any(r["ok"] for r in results)
    assert "will not work" not in results[1]["output"]
    assert "failed 3 times" in results[2]["output"] and "will not work" in results[2]["output"]
    assert [e["data"] for e in events if e["type"] == "stuck"] == [
        {"reason": "stuck_repeat", "tool": "read_file", "count": 8}]
    context = m.db.get_session(sid)["context"]
    assert "will not work" in [msg for msg in context if msg["role"] == "tool"][2]["content"]


TRIM_SCRIPT = """
context_tokens: 16384
scripts:
  - name: trim
    match: "trim"
    turns:
      - text: "Writing the data file."
        tool_calls: [{name: write_file, args: {path: notes/data.txt, content: "%s"}}]
      - text: "Reading it."
        filler_chars: 12000
        tool_calls: [{name: read_file, args: {path: notes/data.txt}}]
      - text: "Reading it again."
        filler_chars: 12000
        tool_calls: [{name: read_file, args: {path: notes/data.txt}}]
      - text: "Another long pass."
        filler_chars: 12000
        tool_calls: [{name: write_file, args: {path: notes/out.md, content: "1"}}]
      - text: "Another long pass."
        filler_chars: 12000
        tool_calls: [{name: write_file, args: {path: notes/out.md, content: "2"}}]
      - text: "Done."
""" % "".join(f"row {i:02d} alpha beta gamma delta\n" for i in range(40))


def test_full_stack_trim_runs_before_the_summary(monkeypatch, tmp_path):
    """#637: a transcript that crosses summarize_at in fake model mode. The older of two identical reads is dropped by
    the trim tier, its `trim` event comes before the summary, and the model gets the trimmed context."""
    from harness import sandbox
    monkeypatch.setattr(sandbox, "run_cmd", fail_on_docker)
    config_dir, data_dir = fake_instance.write_instance(tmp_path / "instance", 0)
    monkeypatch.setenv(fake_model.ENV, str(fixtures(tmp_path, TRIM_SCRIPT)))
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
                               json={"prompt": "Run the trim task.", "project": fake_instance.PROJECT})
            assert made.status_code == 201, made.text
            sid = made.json()["id"]
            done = wait_session(client, sid, headers, "done", "failed")
        assert done["status"] == "done", done
        compactions = [e["data"] for e in m.db.events(sid) if e["type"] == "compaction"]
        tiers = [c["tier"] for c in compactions]
        assert "trim" in tiers and "summary" in tiers and tiers.index("trim") < tiers.index("summary"), tiers
        assert compactions[tiers.index("trim")]["characters_saved"] > 1000
        marker = compaction.SUPERSEDED.format(path="notes/data.txt")
        summary = next(r for r in requests if r[0]["content"] == compaction.SUMMARY_SYSTEM)
        excerpt = summary[1]["content"]
        assert f"TOOL RESULT: {marker}" in excerpt  # the summarizer got the trimmed context
        assert excerpt.count("row 07 alpha") <= 2   # the write's arguments and the newer read; the older read is gone
    finally:
        m.db.close()
