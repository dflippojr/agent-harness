"""Issue #168: config-declared print-mode CLI backends, starting with OpenCode on the local model."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from harness import config as config_mod
from harness.api import create_app
from harness.cli_backends import OpencodeJsonl, PrintModeSession, forwarder_url
from harness.config import BackendConfig, ModelConfig, SandboxConfig
from harness.manager import HarnessError, Manager
from test_daemon import events, make_cfg, wait_status
from test_phase8 import wait_cli_gone

FIXTURES = Path(__file__).parent / "fixtures" / "opencode_1.18.30"
COMMAND = ["opencode", "run", "--format", "json", "--auto", "-m", "llama/{model}"]


def opencode_backend(**overrides) -> BackendConfig:
    spec = {"enabled": True, "kind": "cli", "image": "agent-harness-cli:1", "model": "fake", "command": COMMAND,
            "resume_args": ["--session", "{session_id}"], "parser": "opencode_jsonl",
            "network": "harness-cli-opencode", "volume": "harness-state-opencode", "max_sessions": 1,
            "permission_mode": "auto"}
    backend = BackendConfig(**{**spec, **overrides})
    config_mod._validate_cli_backend("opencode", {**spec, **overrides}, backend)
    return backend


def parse_fixture(name: str, extra: list[str] = ()) -> tuple[OpencodeJsonl, list[dict]]:
    parser = OpencodeJsonl()
    out = []
    for line in [*(FIXTURES / name).read_text(encoding="utf-8").splitlines(), *extra]:
        out += parser.parse(line)
    return parser, out


# --- parser (recorded `opencode run --format json` 1.18.30 output) --------------------------------------------------

def test_opencode_parser_maps_a_recorded_turn():
    parser, out = parse_fixture("opencode_turn.jsonl", extra=["not json at all", "[1, 2]", '{"type": "text"'])
    sessions = {e["id"] for e in out if e["type"] == "session"}
    assert sessions == {"ses_f5f0271e7ffeGkAS0Wj99tPl6U"}
    tools = [e for e in out if e["type"] == "tool"]
    assert [(t["name"], t["status"], t["error"]) for t in tools] == [("glob", "completed", False),
                                                                     ("edit", "error", True)]
    assert tools[0]["input"] == {"pattern": "todo", "path": "/workspace"}
    assert tools[0]["output"] == "No files found"
    assert "Found multiple matches" in tools[1]["output"]
    assert sum(e["type"] == "step" for e in out) == 3
    assert parser.answer.startswith("Implemented `--format` option")
    assert not [e for e in out if e["type"] == "error"]


def test_opencode_parser_maps_a_recorded_error_event():
    _, out = parse_fixture("opencode_error.jsonl")
    errors = [e for e in out if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["message"].startswith("ContextOverflowError: request (39874 tokens) exceeds")


def test_opencode_parser_answer_is_the_last_message_with_text():
    parser = OpencodeJsonl()
    for message, text in (("m1", "thinking out loud"), ("m2", "final "), ("m2", "answer")):
        parser.parse(json.dumps({"type": "text", "sessionID": "s",
                                 "part": {"messageID": message, "text": text}}))
    assert parser.answer == "final \nanswer"


# --- argv ------------------------------------------------------------------------------------------------------------

def test_print_mode_docker_command_is_sandboxed_local_only_and_resumes(tmp_path):
    backend = opencode_backend(model="qwen")
    model = ModelConfig(name="qwen", base_url="http://127.0.0.1:8091")
    cli = PrintModeSession(session_id="abc", workspace=tmp_path, backend=backend,
                           sandbox=SandboxConfig(memory="3g", cpus="1.5", pids=321), system_prompt="system",
                           name="opencode", model_config=model, base_url=forwarder_url(backend, model.base_url),
                           config_dir=tmp_path / "cfg")
    first = cli.command("do it; rm -rf / $(whoami)")
    assert first[:5] == ["docker", "run", "--rm", "--name", "harness-abc-opencode"]
    # Only the backend's own internal network, no proxy, no key
    assert [first[i + 1] for i, a in enumerate(first) if a == "--network"] == ["harness-cli-opencode"]
    assert not any("PROXY" in a for a in first)
    assert not any("KEY" in a or "TOKEN" in a for a in first)
    # Bind mounts: the workspace and the generated config (read-only), nothing else from the host
    mounts = [first[i + 1] for i, a in enumerate(first) if a == "--mount"]
    binds = [m for m in mounts if m.startswith("type=bind")]
    assert binds == [f"type=bind,source={tmp_path / 'cfg' / 'abc.json'},target=/etc/opencode/opencode.json,readonly",
                     f"type=bind,source={tmp_path.resolve()},target=/workspace"]
    assert [first[i + 1] for i, a in enumerate(first) if a == "-v"] == [
        "harness-state-opencode:/home/agent/.cli-state"]
    for pair in (["--memory", "3g"], ["--cpus", "1.5"], ["--pids-limit", "321"],
                 ["--security-opt", "no-new-privileges"], ["-e", "OPENCODE_DISABLE_PROJECT_CONFIG=1"],
                 ["--label", "agent-harness.session=abc"]):
        assert any(first[at:at + 2] == pair for at in range(len(first) - 1)), pair
    image = first.index("agent-harness-cli:1")
    assert first[image + 1:] == ["opencode", "run", "--format", "json", "--auto", "-m", "llama/qwen",
                                 "--", "do it; rm -rf / $(whoami)"]
    # A follow-up turn on the same OpenCode session
    cli.backend_session_id = "ses_123"
    second = cli.command("next")
    assert second[second.index("agent-harness-cli:1") + 1:] == [
        "opencode", "run", "--format", "json", "--auto", "-m", "llama/qwen", "--session", "ses_123", "--", "next"]


def test_print_mode_generated_config_keeps_the_denials(tmp_path):
    backend = opencode_backend(model="qwen")
    model = ModelConfig(name="qwen", base_url="http://127.0.0.1:8091", context_tokens=40000, max_tokens=4000)
    cli = PrintModeSession(session_id="abc", workspace=tmp_path, backend=backend, sandbox=SandboxConfig(),
                           system_prompt="s", name="opencode", model_config=model,
                           base_url=forwarder_url(backend, model.base_url), config_dir=tmp_path / "cfg",
                           command=[sys.executable])

    async def body():
        await cli.start()
        data = json.loads(cli.config_file.read_text(encoding="utf-8"))
        assert data["autoupdate"] is False and data["share"] == "disabled"
        assert data["permission"] == {"*": "allow", "webfetch": "deny", "websearch": "deny", "question": "deny"}
        llama = data["provider"]["llama"]
        assert llama["options"] == {"baseURL": "http://harness-cli-opencode-llm:8091/v1", "apiKey": "local"}
        assert llama["models"]["qwen"]["limit"] == {"context": 40000, "output": 4000}
        await cli.stop()
        assert not cli.config_file.exists()
    asyncio.run(body())


# --- config ----------------------------------------------------------------------------------------------------------

def _load(tmp_path, backends: dict):
    (tmp_path / "harness.yaml").write_text(yaml.safe_dump({
        "data_dir": str(tmp_path / "data"), "default_model": "qwen",
        "models": {"qwen": {"base_url": "http://127.0.0.1:8090"}}, "backends": backends}), encoding="utf-8")
    return config_mod.load(tmp_path)


@pytest.mark.parametrize("bad, message", [
    ({"proxy": "http://harness-egress-claude:8888"}, "provider-policy review"),
    ({"api_key_file": "D:/secret"}, "provider-policy review"),
    ({"parser": "nope"}, "parser must be one of"),
    ({"command": ["opencode", "{prompt}"]}, "{prompt} may only be the last"),
    ({"command": ["opencode", "{home}"]}, "unknown placeholder {home}"),
    ({"command": ["opencode", "--", "x"]}, "-- may only come right before"),
    ({"kind": "plugin"}, "kind must be empty"),
    ({"volume": ""}, "needs its own volume"),  # never the default, Claude's state volume
])
def test_cli_backend_config_validation_rejects(tmp_path, bad, message):
    spec = {"kind": "cli", "image": "agent-harness-cli:1", "model": "qwen", "command": COMMAND,
            "parser": "opencode_jsonl", "network": "harness-cli-opencode", "volume": "harness-state-opencode", **bad}
    with pytest.raises(ValueError) as e:
        _load(tmp_path, {"opencode": spec})
    assert message in str(e.value)


def test_cli_backend_config_pins_keyless_local_fields(tmp_path):
    cfg = _load(tmp_path, {"opencode": {
        "kind": "cli", "image": "agent-harness-cli:1", "model": "qwen", "command": [*COMMAND, "--", "{prompt}"],
        "parser": "opencode_jsonl", "network": "harness-cli-opencode", "volume": "harness-state-opencode"}})
    backend = cfg.backends["opencode"]
    assert backend.command == COMMAND
    assert (backend.proxy, backend.api_key_file, backend.billing, backend.auth, backend.mcp) == (
        "", "", "local", "none", False)
    with pytest.raises(ValueError):
        _load(tmp_path, {"claude": {"kind": "cli", "command": COMMAND, "parser": "opencode_jsonl"}})


def test_cli_backend_without_a_volume_never_mounts_claudes(tmp_path):
    with pytest.raises(ValueError, match="needs its own volume"):
        _load(tmp_path, {"opencode": {"kind": "cli", "image": "agent-harness-cli:1", "model": "qwen",
                                      "command": COMMAND, "parser": "opencode_jsonl",
                                      "network": "harness-cli-opencode"}})


@pytest.mark.parametrize("base_url, expected", [
    ("http://127.0.0.1:8091", "http://harness-cli-opencode-llm:8091/v1"),
    ("http://localhost", "http://harness-cli-opencode-llm:80/v1"),
    ("https://llm.example/v1", "https://harness-cli-opencode-llm:443/v1"),
])
def test_forwarder_url_uses_the_schemes_default_port(base_url, expected):
    assert forwarder_url(opencode_backend(), base_url) == expected


def test_setup_config_writes_a_disabled_opencode_entry(tmp_path):
    from harness import setup_config
    args = type("A", (), {"model": "qwen", "data_dir": str(tmp_path), "enable_module": [], "profile": "full",
                          "port": 8100, "public_url": "", "login": "", "llama_url": "http://127.0.0.1:8090",
                          "gpu_guard": True, "pause_flag": "x", "images_models_dir": ""})()
    backend = setup_config.build(args)["backends"]["opencode"]
    assert backend["enabled"] is False and backend["kind"] == "cli" and backend["max_sessions"] == 1
    assert backend["model"] == setup_config.PRESETS["qwen"]["name"]


# --- registry and the App allowlist ----------------------------------------------------------------------------------

def test_unknown_backend_is_still_rejected_by_the_manager_and_the_runner(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.backends["opencode"] = opencode_backend()
    cfg.backends["mystery"] = BackendConfig(enabled=True)  # a name with no built-in and no kind
    m = Manager(cfg)
    m._spawn = lambda *_a, **_k: None
    with pytest.raises(HarnessError) as e:
        m.create("x", backend="nope")
    assert "unknown backend" in str(e.value)
    with pytest.raises(HarnessError) as e:
        m.create("x", backend="mystery")
    assert "not built yet" in str(e.value)

    async def body():
        await m.start()
        sid = m.create("x", backend="opencode")["id"]
        m.db.update_session(sid, backend="nope")
        s = await wait_status(m, sid, "failed")
        assert "not implemented" in s["run"]["failure"]["message"]
        await m.stop()
    m._spawn = Manager._spawn.__get__(m)
    asyncio.run(body())


def test_opencode_is_invisible_to_every_app_until_granted(tmp_path, monkeypatch):
    from harness import backend_state
    monkeypatch.setattr(backend_state, "cli_route_ready", lambda *_: True, raising=False)
    monkeypatch.setattr("harness.cli_backends.cli_route_ready", lambda *_: True)
    cfg = make_cfg(tmp_path)
    cfg.backends["opencode"] = opencode_backend()
    m = Manager(cfg)
    m._spawn = lambda *_a, **_k: None
    with TestClient(create_app(m)) as client:
        app = client.post("/keys", json={"name": "one", "kind": "app", "scopes": ["sessions"]}).json()
        headers = {"Authorization": f"Bearer {app['key']}"}
        # Not managed, never granted: settings caps it with provider_policy, sessions and discovery refuse it
        patched = client.patch("/api/v1/config", headers=headers, json={
            "revision": 0, "changes": {"app.default_backend": "opencode"}})
        assert patched.status_code == 400
        assert patched.json()["error"]["keys"]["app.default_backend"]["message"] == "this app cannot select that backend"
        key = m.db.get_api_key(app["id"])
        assert m.settings._backend_allowed(key, "opencode") is False
        assert m.settings._policy_checked_backend("opencode", key) == ("", "provider_policy")
        refused = client.post("/api/v1/sessions", headers=headers, json={"prompt": "hi", "backend": "opencode"})
        assert refused.status_code == 403 and "provider_not_allowed" in refused.text
        listed = {row["name"]: row for row in client.get("/api/v1/backends", headers=headers).json()}
        assert listed["opencode"]["available"] is False
        assert listed["opencode"]["provider_policy"]["allowed"] is False
        # The owner grants it: subscription policy, no key
        with pytest.raises(HarnessError):
            m.set_app_provider_credential(app["id"], "opencode", "", "api_key", [])
        m.set_app_provider_credential(app["id"], "opencode", "", "subscription", [])
        assert m.settings._backend_allowed(key, "opencode") is True
        listed = {row["name"]: row for row in client.get("/api/v1/backends", headers=headers).json()}
        assert listed["opencode"]["available"] is True
        created = client.post("/api/v1/sessions", headers=headers, json={"prompt": "hi", "backend": "opencode"})
        assert created.status_code == 201, created.text


def test_opencode_card_and_no_secrets(tmp_path, monkeypatch):
    from harness import backend_state
    monkeypatch.setattr("harness.cli_backends.cli_route_ready", lambda *_: True)
    backend_state._route_cache.clear()
    cfg = make_cfg(tmp_path)
    cfg.backends["opencode"] = opencode_backend()
    m = Manager(cfg)
    view = backend_state.view(m, "opencode")
    assert view["logged_in"] is True and view["available"] is True
    assert view["login_note"] == "No login needed (local model)"
    assert view["notice"] == ("Runs without asking for approval inside its sandbox. Uses the local model, which is "
                              "much weaker than the hosted backends. Usage is not reported.")
    assert view["auth"] == "none" and view["billing"] == "local" and view["api_key_available"] is False
    assert not any(k in json.dumps(view).lower() for k in ("api_key_file", "secret", "token"))
    backend_state._route_cache.clear()
    monkeypatch.setattr("harness.cli_backends.cli_route_ready", lambda *_: False)
    assert backend_state.view(m, "opencode")["available"] is False  # Q9: no route, unavailable
    backend_state._route_cache.clear()


# --- runs ------------------------------------------------------------------------------------------------------------

FAKE_OPENCODE = r'''import json
import pathlib
import sys
import time

mode, state, fixtures = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
argv = sys.argv[4:]
prompt = argv[-1]
resumed = "--session" in argv
with state.open("a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "prompt": prompt, "resumed": resumed}) + "\n")
if mode == "error":
    sys.stdout.write((fixtures / "opencode_error.jsonl").read_text(encoding="utf-8"))
    raise SystemExit(0)
if mode == "crash":
    print("llama-server went away", file=sys.stderr)
    raise SystemExit(3)
if mode == "inbox" and not resumed:
    time.sleep(0.6)
if mode == "cancel":
    time.sleep(60)
for line in (fixtures / "opencode_turn.jsonl").read_text(encoding="utf-8").splitlines():
    print(line, flush=True)
    print("{not json", flush=True)
'''


def _opencode_manager(tmp_path, mode: str, admit=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake = tmp_path / "fake_opencode.py"
    fake.write_text(FAKE_OPENCODE, encoding="utf-8")
    state = tmp_path / "fake-opencode-state.jsonl"
    cfg = make_cfg(tmp_path)
    cfg.backends["opencode"] = opencode_backend()
    manager = Manager(cfg)
    made = []

    def factory(**kwargs):
        made.append(kwargs)
        return PrintModeSession(**kwargs, command=[sys.executable, "-u", str(fake), mode, str(state), str(FIXTURES)])

    manager.runner.print_mode_factory = factory
    manager.runner.cli_route_ready = lambda _backend: True
    manager.runner.gpu_poll_seconds = 0.02

    async def admitted(_sid, _model):
        return None
    if manager.modules is not None:
        manager.modules.local_turn_admission = lambda: admit or admitted
    return manager, made, state


def test_opencode_run_maps_events_and_binds_the_session(tmp_path):
    async def body():
        m, made, state = _opencode_manager(tmp_path, "normal")
        await m.start()
        s = await wait_status(m, m.create("fix it", backend="opencode")["id"], "done")
        await asyncio.gather(*m.tasks.values())
        assert s["answer"].startswith("Implemented `--format` option")
        assert s["run"]["backend_session_id"] == "ses_f5f0271e7ffeGkAS0Wj99tPl6U"
        assert s["run"]["tool_calls"] == 2 and s["run"]["tool_errors"] == 1
        assert s["run"]["billing_mode"] == "local"
        assert s["totals"]["turns"] == 3
        assert s["totals"]["prompt_tokens"] == 0 and s["totals"]["total_cost_usd"] == 0
        results = events(m, s["id"], "tool_result")
        assert [(r["name"], r["ok"]) for r in results] == [("glob", True), ("edit", False)]
        assert events(m, s["id"], "assistant")[-1]["content"] == s["answer"]
        record = json.loads(state.read_text(encoding="utf-8").splitlines()[0])
        assert record["argv"][-2] == "--" and "User task:\nfix it" in record["prompt"]
        assert made[0]["model"] == "fake" and made[0]["base_url"] == "http://harness-cli-opencode-llm:80/v1"
        await m.stop()
    asyncio.run(body())


def test_opencode_queues_a_live_message_as_a_resumed_turn(tmp_path):
    async def body():
        m, _, state = _opencode_manager(tmp_path, "inbox")
        await m.start()
        sid = m.create("first", backend="opencode")["id"]
        await wait_status(m, sid, "running")
        for _ in range(200):  # the session id arrives with the first event
            if state.exists():
                break
            await asyncio.sleep(0.02)
        await m.send(sid, "follow up")
        s = await wait_status(m, sid, "done")
        await asyncio.gather(*m.tasks.values())
        records = [json.loads(line) for line in state.read_text(encoding="utf-8").splitlines()]
        assert len(records) == 2
        assert records[1]["argv"][-4:-2] == ["--session", "ses_f5f0271e7ffeGkAS0Wj99tPl6U"]
        assert "follow up" in records[1]["prompt"]
        assert s["inbox"] == []
        await m.stop()
    asyncio.run(body())


@pytest.mark.parametrize("mode, text", [("error", "ContextOverflowError"), ("crash", "llama-server went away")])
def test_opencode_error_event_or_exit_fails_the_session(tmp_path, mode, text):
    async def body():
        m, _, _ = _opencode_manager(tmp_path, mode)
        await m.start()
        s = await wait_status(m, m.create("x", backend="opencode")["id"], "failed")
        assert text in s["run"]["failure"]["message"]
        await m.stop()
    asyncio.run(body())


def test_opencode_cancel_stops_the_container(tmp_path):
    async def body():
        m, _, _ = _opencode_manager(tmp_path, "cancel")
        await m.start()
        sid = m.create("wait", backend="opencode")["id"]
        await wait_status(m, sid, "running")
        assert (await m.cancel(sid))["status"] == "cancelled"
        await wait_cli_gone(m, sid)
        await m.stop()
    asyncio.run(body())


# --- GPU guard (Q7) and the parked model (Q10) -----------------------------------------------------------------------

class FakeGuard:
    def __init__(self, active: bool):
        self.active = active
        self.reasons = [{"kind": "game", "name": "game.exe"}]
        self.cfg = type("C", (), {"resume_after_seconds": 60})()

    @staticmethod
    def describe(_reasons):
        return "a game is running"


def test_no_opencode_turn_starts_while_the_gpu_guard_holds(tmp_path):
    async def body():
        m, _, state = _opencode_manager(tmp_path, "normal")
        await m.start()
        from harness_modules.local_model.runtime import LocalModelRuntime
        rt = m.modules.get("local_model") if m.modules is not None else None
        if rt is None:
            raise AssertionError("the local_model module is part of this repository")
        m.modules.local_turn_admission = LocalModelRuntime.local_turn_admission.__get__(rt)
        guard = FakeGuard(active=True)
        m.runner.guard = guard
        sid = m.create("x", backend="opencode")["id"]
        await asyncio.sleep(0.3)
        assert not state.exists()  # no OpenCode process yet
        assert m.db.get_session(sid)["status"] == "queued"
        assert events(m, sid, "gpu_paused")
        guard.active = False
        m.runner.gpu_resumed(1)
        s = await wait_status(m, sid, "done")
        assert events(m, sid, "gpu_resumed")
        assert s["answer"]
        m.runner.guard = None
        await m.stop()
    asyncio.run(body())


class FakeWarmer:
    def __init__(self, loads: bool):
        self.loads = loads
        self.loaded = False
        self.calls: list[str] = []

    def blocked(self):
        return False

    def parked(self, _model):
        return not self.loaded

    async def state(self, _model):
        return "ready" if self.loaded else "unloaded"

    async def ensure_loaded(self, _model):
        self.calls.append("ensure_loaded")
        self.loaded = self.loads


class FakeRam:
    poll_seconds = 0.01

    def __init__(self, low: bool):
        self._low = low

    def low(self):
        return False

    def load_low(self):
        return self._low

    def status(self):
        return {"available_bytes": 1, "threshold_bytes": 2}

    @staticmethod
    def describe(_status):
        return "RAM is low"


def _with_local_runtime(m, warmer):
    from harness_modules.local_model.runtime import LocalModelRuntime
    rt = m.modules.get("local_model") if m.modules is not None else None
    if rt is None:
        raise AssertionError("the local_model module is part of this repository")
    rt.service = warmer
    m.modules.local_turn_admission = LocalModelRuntime.local_turn_admission.__get__(rt)


def test_parked_model_is_loaded_before_the_opencode_turn(tmp_path):
    async def body():
        m, _, state = _opencode_manager(tmp_path, "normal")
        await m.start()
        warmer = FakeWarmer(loads=True)
        _with_local_runtime(m, warmer)
        popen_seen_loaded = []
        factory = m.runner.print_mode_factory

        def watching(**kwargs):
            cli = factory(**kwargs)
            spawn = cli._popen

            def popen(*a, **k):
                popen_seen_loaded.append(warmer.loaded)
                return spawn(*a, **k)
            cli._popen = popen
            return cli
        m.runner.print_mode_factory = watching
        s = await wait_status(m, m.create("x", backend="opencode")["id"], "done")
        assert warmer.calls == ["ensure_loaded"]
        assert popen_seen_loaded == [True]
        assert s["answer"]
        await m.stop()
    asyncio.run(body())


def test_ram_guard_refusal_is_the_low_memory_error(tmp_path):
    async def body():
        m, _, state = _opencode_manager(tmp_path, "normal")
        await m.start()
        _with_local_runtime(m, FakeWarmer(loads=False))
        ram = FakeRam(low=True)
        m.runner.ram = ram

        async def memory_wait(_sid, _model):  # the native wait returns once RAM is back; here it never loaded
            return None
        m.runner._memory_wait = memory_wait
        s = await wait_status(m, m.create("x", backend="opencode")["id"], "failed")
        assert s["run"]["failure"]["code"] == "low_memory"
        assert "RAM on the server is low" in s["run"]["failure"]["message"]
        assert not state.exists()
        m.runner.ram = None
        await m.stop()
    asyncio.run(body())


def test_without_the_local_model_module_the_server_must_answer(tmp_path):
    async def body():
        m, _, state = _opencode_manager(tmp_path, "normal")
        await m.start()
        m.modules.local_turn_admission = lambda: None

        async def unreachable(_model):
            return False
        m.runner._model_reachable = unreachable
        s = await wait_status(m, m.create("x", backend="opencode")["id"], "failed")
        assert "isn't reachable" in s["run"]["failure"]["message"]
        assert not state.exists()
        await m.stop()
    asyncio.run(body())


def test_missing_route_makes_the_session_unavailable(tmp_path):
    async def body():
        m, _, state = _opencode_manager(tmp_path, "normal")
        m.runner.cli_route_ready = lambda _backend: False
        await m.start()
        s = await wait_status(m, m.create("x", backend="opencode")["id"], "failed")
        assert s["run"]["failure"]["code"] == "backend_unavailable"
        assert not state.exists()
        await m.stop()
    asyncio.run(body())


# --- review fixes: erase and the inference gate ----------------------------------------------------------------------

def test_erasing_an_opencode_session_deletes_it_offline_from_its_domain_volume():
    from harness import cli_domains
    backend = opencode_backend()
    command = cli_domains.erase_command("opencode", backend, "app-1", "ses_f5f0271e7ffeGkAS0Wj99tPl6U")
    assert command[:7] == ["docker", "run", "--rm", "--network", "none", "--user", "1000:1000"]
    assert ["-v", "harness-cli-opencode-app-app-1:/home/agent/.cli-state"] == command[
        command.index("-v"):command.index("-v") + 2]
    assert command[-4:] == ["opencode", "session", "delete", "ses_f5f0271e7ffeGkAS0Wj99tPl6U"]
    with pytest.raises(ValueError):
        cli_domains.erase_command("opencode", backend, "", "x; rm -rf /")


# OpenCode 1.18.30's own stderr for `opencode session delete` of an id it does not have.
MISSING = "\x1b[91m\x1b[1mError: \x1b[0mSession not found: ses_f5f0271e7ffeGone000000\n"
NO_CLI = 'docker: Error response from daemon: ... exec: "opencode": executable file not found in $PATH\n'


@pytest.mark.parametrize("result, erased", [
    ((0, "", ""), True),
    ((1, "", MISSING), True),  # already gone
    ((127, "", NO_CLI), False),  # an image without opencode: the history is still there
    ((1, "", MISSING.replace("ses_f5f0271e7ffeGone000000", "ses_f5f0271e7ffeOther00000")), False),
])
def test_erase_history_treats_only_opencodes_missing_session_as_gone(monkeypatch, result, erased):
    from harness import cli_domains
    async def run_cmd(*_args, **_kwargs):
        return result
    monkeypatch.setattr(cli_domains, "run_cmd", run_cmd)
    erase = cli_domains.erase_history("opencode", opencode_backend(), "app-1", "ses_f5f0271e7ffeGone000000")
    if erased:
        asyncio.run(erase)
    else:
        with pytest.raises(RuntimeError, match="could not erase opencode history"):
            asyncio.run(erase)


def test_an_image_job_waits_for_a_running_opencode_turn(tmp_path):
    async def body():
        m, _, state = _opencode_manager(tmp_path, "cancel")  # the turn runs until cancelled
        await m.start()
        sid = m.create("x", backend="opencode")["id"]
        await wait_status(m, sid, "running")
        for _ in range(200):
            if state.exists():
                break
            await asyncio.sleep(0.02)
        exclusive = asyncio.create_task(m.runner.gate.acquire_exclusive())
        await asyncio.sleep(0.3)
        assert not exclusive.done()  # it can't stop llama-server under the turn
        await m.cancel(sid)
        slot = await asyncio.wait_for(exclusive, 10)
        await slot.release()
        await m.stop()
    asyncio.run(body())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer")
@pytest.mark.parametrize("profile", ["Full", "Service"])
def test_windows_installer_builds_the_cli_image_for_opencode_and_service(tmp_path, profile):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["powershell.exe", "-NoProfile", "-File", str(root / "install/install.ps1"),
                             "-InstallDir", str(tmp_path / "install"), "-Profile", profile,
                             *(["-ExistingServer", "http://localhost"] if profile == "Full" else []), "-DryRun",
                             "-NoTasks"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "[dry run] docker build -t agent-harness-cli:1" in result.stdout
    assert ("==> Local-model route for CLI backends" in result.stdout) == (profile == "Full")
