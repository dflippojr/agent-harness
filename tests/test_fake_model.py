"""Fake model mode (#588): fixtures, the scripted chat, the opt-in guard and a full-stack run. Nothing needs the GPU."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

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
    turns: [{error: {message: "HTTP 400: no", times: 1}}]
""", tmp_path)
    messages = [{"role": "user", "content": "flaky"}]
    for _ in range(2):
        with pytest.raises(LLMError, match="boom") as raised:
            run(fake(None, messages, TOOLS))
        assert raised.value.retryable
    assert run(fake(None, messages, TOOLS)).content == "recovered"
    # Once a call got through, the next session that reaches the same context fails the same way again.
    with pytest.raises(LLMError, match="boom"):
        run(fake(None, messages, TOOLS))
    fatal = [{"role": "user", "content": "fatal"}]
    with pytest.raises(LLMError) as raised:
        run(fake(None, fatal, TOOLS))
    assert not raised.value.retryable
    assert "Recovered after the scripted error" in run(fake(None, fatal, TOOLS)).content


def test_summary_requests_get_the_fixture_summary_and_no_tool_calls(tmp_path):
    fake = model(SCRIPT + 'summary: "Handoff notes."\n', tmp_path)
    request = compaction.summary_request([{"role": "system", "content": "s"}, {"role": "user", "content": "edit"},
                                          {"role": "assistant", "content": "a"}, {"role": "user", "content": "b"}],
                                         2, 4, 10000)
    out = run(fake(None, request, None))
    assert out.content == "Handoff notes." and not out.tool_calls
    # Any request without tools (a delegate edit, a skill review) gets text only.
    assert run(fake(None, [{"role": "user", "content": "edit"}], None)).tool_calls == []


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


# the disposable instance
def test_stop_is_safe_to_repeat_and_only_deletes_instance_dirs(tmp_path):
    assert fake_instance.stop(tmp_path / "absent") == 0
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    (stranger / "notes.txt").write_text("keep me", encoding="utf-8")
    assert fake_instance.stop(stranger) == 2 and (stranger / "notes.txt").exists()
    instance = tmp_path / "instance"
    fake_instance.write_instance(instance, 0)
    frozen = instance / "data" / "checkpoints" / "object"   # git objects are read-only on Windows
    frozen.parent.mkdir(parents=True)
    frozen.write_text("x", encoding="utf-8")
    frozen.chmod(0o444)
    # A handle whose process is gone (or isn't an instance) is not killed; the directory still goes.
    fake_instance.write_handle(instance, {"pid": 0})
    assert fake_instance.stop(instance) == 0 and not instance.exists()
    assert fake_instance.stop(instance) == 0
    lookalike = tmp_path / "lookalike"   # an instance.json alone is no licence to delete a directory
    lookalike.mkdir()
    (lookalike / fake_instance.HANDLE).write_text("{}", encoding="utf-8")
    (lookalike / "notes.txt").write_text("keep me", encoding="utf-8")
    assert fake_instance.stop(lookalike) == 2 and (lookalike / "notes.txt").exists()
    partial = tmp_path / "partial"
    partial.mkdir()   # a start that failed before writing anything
    assert fake_instance.stop(partial) == 0 and not partial.exists()


def test_start_refuses_an_occupied_port_or_a_used_dir(tmp_path, capsys):
    import socket
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        assert fake_instance.available_port(port) is None and fake_instance.available_port(0)
        with pytest.raises(SystemExit):
            fake_instance.main(["--port", str(port), "--dir", str(tmp_path / "new")])
        assert "is in use" in capsys.readouterr().err and not (tmp_path / "new").exists()
    used = tmp_path / "used"
    used.mkdir()
    (used / "x").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        fake_instance.main(["--dir", str(used)])
    assert "not empty" in capsys.readouterr().err


def test_serve_seeds_settles_and_shuts_down(monkeypatch, tmp_path):
    """The instance end to end in this process: READY only once the seeded runs settled, then a clean stop."""
    import argparse
    import httpx
    monkeypatch.setenv(fake_model.ENV, "")  # serve sets it for its own process; restored after the test
    root = tmp_path / "instance"
    root.mkdir()
    args = argparse.Namespace(port=0, web_port=0, fixtures=fake_model.DEFAULT_FIXTURES, no_seed=False, keep=False)

    async def body():
        task = asyncio.create_task(fake_instance.serve(args, root))
        deadline = time.monotonic() + scaled(120)
        state = {}
        while state.get("status") != "ready":
            assert time.monotonic() < deadline and not task.done(), "the instance never got ready"
            await asyncio.sleep(0.2)
            try:
                state = json.loads((root / fake_instance.HANDLE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        async with httpx.AsyncClient() as client:
            assert (await client.get(state["web"] + "/ready")).json()["key_id"] == state["key_id"]
            assert "harness.ownerToken" in (await client.get(state["connect"])).text
            assert (await client.get(state["server"] + "/health")).status_code == 200
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return state
    state = run(body())
    assert state["pid"] and "token" not in json.dumps(state).lower()
    assert fake_instance.remove(root) and not root.exists()


@pytest.mark.parametrize("outcome,code", [(fake_instance.StartError("seeding failed"), 1), (KeyboardInterrupt(), 0)])
def test_start_cleans_up_after_a_failure_or_ctrl_c(monkeypatch, tmp_path, capsys, outcome, code):
    import signal
    handlers = []
    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.append(sig))
    seen = {}

    async def serve(args, root):
        seen["root"] = root
        fake_instance.write_instance(root, 0)
        raise outcome
    monkeypatch.setattr(fake_instance, "serve", serve)
    assert fake_instance.main(["--dir", str(tmp_path / "instance")]) == code
    assert seen["root"] == (tmp_path / "instance").resolve() and not seen["root"].exists()
    assert handlers  # Ctrl+Break / SIGTERM reach the cleanup as KeyboardInterrupt
    assert ("FAILED seeding failed" in capsys.readouterr().err) == (code == 1)


def test_stop_ends_the_recorded_instance_process(tmp_path):
    import subprocess
    import sys
    instance = tmp_path / "instance"
    fake_instance.write_instance(instance, 0)
    # Stands in for a running instance: its command line names harness.fake_instance.
    import psutil
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "harness.fake_instance"])
    try:
        created = psutil.Process(proc.pid).create_time()
        # The same pid with another start time is a reused pid (another instance, say): never killed.
        fake_instance.write_handle(instance, {"pid": proc.pid, "pid_created": created - 60})
        assert fake_instance._instance_process({"pid": proc.pid, "pid_created": created - 60}) is None
        fake_instance.write_handle(instance, {"pid": proc.pid, "pid_created": created})
        assert fake_instance.stop(instance) == 0
        assert proc.wait(timeout=30) is not None and not instance.exists()
    finally:
        proc.kill()


def test_teardown_removes_only_this_instances_sandbox_containers(monkeypatch, tmp_path):
    import sqlite3
    import subprocess
    instance = tmp_path / "instance"
    fake_instance.write_instance(instance, 0)
    for db, ids in ((instance / "data" / "harness.sqlite3", ("s1", "s2")),
                    (instance / "data" / "apps" / "app-x" / "harness.sqlite3", ("s3",))):
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE sessions (id TEXT)")
        con.executemany("INSERT INTO sessions VALUES (?)", [(i,) for i in ids])
        con.commit()
        con.close()

    def no_docker(args, **kwargs):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(subprocess, "run", no_docker)
    fake_instance.remove_containers(instance)   # no Docker CLI: no containers, no error
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: calls.append(args))
    assert fake_instance.stop(instance) == 0 and not instance.exists()
    assert calls == [["docker", "rm", "-f", "harness-s1", "harness-s2", "harness-s3"]]


def test_kill_survives_a_child_that_already_exited(monkeypatch):
    import psutil
    killed = []

    class Proc:
        def __init__(self, pid, gone=False, kids=()):
            self.pid, self.gone, self.kids = pid, gone, list(kids)

        def children(self, recursive=False):
            return self.kids

        def kill(self):
            if self.gone:
                raise psutil.NoSuchProcess(self.pid)
            killed.append(self.pid)
    parent = Proc(1, kids=[Proc(2, gone=True), Proc(3)])
    monkeypatch.setattr(psutil, "wait_procs", lambda procs, timeout: (procs, []))
    assert fake_instance._kill(parent) and killed == [3, 1]
    monkeypatch.setattr(psutil, "wait_procs", lambda procs, timeout: ([], procs))
    assert not fake_instance._kill(Proc(4))   # still alive: stop leaves the directory in place


def test_stop_leaves_an_unrelated_process_alone(tmp_path):
    import subprocess
    import sys
    instance = tmp_path / "instance"
    fake_instance.write_instance(instance, 0)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        import psutil
        created = psutil.Process(proc.pid).create_time()
        fake_instance.write_handle(instance, {"pid": proc.pid, "pid_created": created})  # not an instance
        assert fake_instance.stop(instance) == 0 and proc.poll() is None and not instance.exists()
    finally:
        proc.kill()


def test_main_stop_subcommand(tmp_path):
    assert fake_instance.main(["stop", "--dir", str(tmp_path / "absent")]) == 0


def test_ready_answers_503_until_seeded():
    from starlette.testclient import TestClient as Plain
    state = {"status": "starting"}
    client = Plain(fake_instance.web_app("http://127.0.0.1:1", "ho-x", state))
    assert client.get("/ready").status_code == 503
    state["status"] = "ready"
    assert client.get("/ready").json() == {"status": "ready"}
    assert client.get("/connect").status_code == 200 and client.get("/").status_code == 200
    assert client.get("/static/icon-192.png").status_code == 200


def test_connect_page_signs_the_web_copy_in():
    page = fake_instance.connect_page("http://127.0.0.1:1", "ho-<x>")
    assert '"harness.daemonUrl": "http://127.0.0.1:1"' in page and '"harness.ownerToken": "ho-<x>"' in page


def wait_session(client, sid, headers, *statuses):
    deadline, s = time.monotonic() + scaled(30), {"status": "unknown"}
    while time.monotonic() < deadline:
        s = client.get(f"/api/v1/sessions/{sid}", headers=headers).json()
        if s["status"] in statuses:
            return s
        time.sleep(0.05)
    raise AssertionError(f"session {sid} stayed {s['status']}, wanted {statuses}")


def test_sandbox_stop_without_docker_is_a_no_op(monkeypatch, tmp_path):
    from harness import sandbox
    from harness.config import SandboxConfig

    async def no_docker(*_args, **_kwargs):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(sandbox, "run_cmd", no_docker)
    run(sandbox.Sandbox("s1", tmp_path, SandboxConfig()).stop())


def test_full_stack_approval_and_compaction(monkeypatch, fake_manager):
    """The real server with the scripted model, on a machine without Docker: an approval the policy asks for, then a
    run that compacts."""
    from harness import sandbox
    real_run_cmd = sandbox.run_cmd

    async def no_docker(args, *rest, **kwargs):
        if args and args[0] == "docker":
            raise FileNotFoundError("docker")
        return await real_run_cmd(args, *rest, **kwargs)
    monkeypatch.setattr(sandbox, "run_cmd", no_docker)
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
