"""One module-lifecycle table (#708): every add-on module in the four states an install can leave it in.

``on`` is present and switched on, ``off`` is present but switched off (the owner can turn it back on, so its keys
stay), ``absent`` has no package (``module_packages`` lists everything else) and ``uninstalled`` has the package but
not the profile switch (``installed.X = False``). A service-profile install leaves every module uninstalled.

Each row lists what a module contributes, and the test crosses the rows with the states through ``create_app``:
its probe route, its settings keys, its capability, feature and scope entries, its routes, CLI rows, metrics and
agent tools. Adding a module means adding one row here, not copying a test file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest
from fastapi.testclient import TestClient

from harness import config, modules, setup_config
from harness.api import create_app
from harness.config import Project
from harness.llm import Completion
from harness.manager import Manager
from harness.runner_contract import NoRunnerHub
from harness.local_inference import READY
from harness_modules.local_model.warmup import ModelWarmer

from test_daemon import Script, make_cfg
from test_modules import route_paths

PRESENT = ("on", "off")
MISSING = ("absent", "uninstalled")
GONE = (404, 405)  # no route; the static site refuses a POST with 405
SESSION = {"id": "s1", "kind": "agent", "project": "scratch", "owner_id": "owner", "app_id": "", "target": "tower"}


@dataclass(frozen=True)
class Row:
    name: str
    configure: Callable[[config.Config, Path, bool], None] = lambda cfg, tmp, enabled: None  # its config section
    states: tuple[str, ...] = ("on", "off", "absent", "uninstalled")
    probe: tuple[str, str] | None = None            # method and path, answered per state below
    answers: dict[str, int] = field(default_factory=dict)   # state -> status; a missing module answers GONE
    keys: tuple[str, ...] = ()                      # registry keys, present exactly when the module is
    caps: dict[str, str] = field(default_factory=dict)      # /api/v1 capabilities.modules: on | present | never
    features: dict[str, str] = field(default_factory=dict)  # /api/v1 features, same values
    scopes: tuple[str, ...] = ()                    # App scopes the module adds
    me: tuple[str, ...] = ()                        # /me capabilities
    app_capability: str = ""                        # an app.capabilities value an App may be narrowed by
    top: tuple[str, ...] = ()                       # top-level keys the module adds to the config schema
    operations: tuple[str, ...] = ()                # path fragments in the admin operation list
    gone: tuple[str, ...] = ()                      # GET paths that answer 404 once the module is missing
    routes: tuple[str, ...] = ()                    # registered route paths
    cli: str = ""                                   # one CLI row the module adds
    metrics: tuple[str, ...] = ()                   # metric families, present exactly when the module is
    tools: tuple[str, ...] = ()                     # agent tools offered to an owner session while on
    service: str = ""                               # Manager attribute holding the module's service
    service_in: tuple[str, ...] = ("on",)           # the states in which that attribute is set
    extra: Callable[[Manager, TestClient, str], None] | None = None
    switch: str = ""                                # profile switch deciding presence (default: the name)

    @property
    def package(self) -> str:
        return f"harness_modules.{self.name}"

    @property
    def installed(self) -> str:
        return self.switch or self.name


def _backup(cfg, tmp, on):
    cfg.backup = config.BackupConfig(enabled=on, dir=str(tmp / "backups"))


def _backup_status(m, client, state):
    assert ("backup" in m.maintenance.status_extras) == (state in PRESENT)
    assert (m.backup is not None and m.backup._backup_task is not None) == (state == "on")  # only "on" schedules


def _endpoint(cfg, tmp, on):
    cfg.endpoint.enabled = on
    cfg.endpoint.max_waiting = 7
    cfg.endpoint.agent_fair_seconds = 123


def _endpoint_gate(m, client, state):
    """The queue limits are shared with the core: the module sets them while present, the defaults hold without."""
    assert (m.runner.gate.max_waiting, m.runner.gate.fair_seconds) == ((7, 123) if state in PRESENT else (4, 90))


def _jobs(cfg, tmp, on):
    cfg.jobs.enabled = on


def _jobs_run(m, client, state):
    assert not modules.principal_capabilities(m.cfg, False).get("jobs", False)  # only the owner schedules jobs
    assert modules.principal_capabilities(m.cfg, True).get("jobs", False) == (state in PRESENT)
    assert (m.jobs is not None and m.jobs._task is not None) == (state == "on")  # the scheduler runs while on


def _runners(cfg, tmp, on):
    token = tmp / "runner.token"
    token.write_text("already-paired", encoding="utf-8")
    cfg.runners = {"mac": config.RunnerConfig(name="mac", token_file=str(token), min_free_gb=0)}


def _runners_hub(m, client, state):
    if state == "on":
        assert m.hub is m.runner.hub is m.modules.get("runners").service and m.runners is m.hub
        assert client.get("/api/admin/v1/runners").json()[0]["name"] == "mac"
        paths = {op["path"] for op in client.get("/api/admin/v1").json()["operations"]}
        assert not {"/api/admin/v1/runners/{name}/poll", "/api/admin/v1/runners/{name}/results"} & paths
    else:  # without the module a remote run is refused, never queued
        assert m.runners is None and isinstance(m.hub, NoRunnerHub) and m.hub is m.runner.hub
        assert client.get("/maintenance").json()["runners"] == []
        assert client.post("/api/v1/runner-pair", json={"code": "hrp-example"}).status_code in GONE


def _search(cfg, tmp, on):
    cfg.search.enabled = on


def _search_keeps_indexing(m, client, state):
    """The core keeps filling the session index whether or not the module serves it."""
    from harness.search_index import event_text
    assert event_text("user_message", {"content": "pineapple"}) == ("message", "pineapple")
    assert event_text("tool_result", {"name": "session_search", "output": "x"}) is None


def _skills(cfg, tmp, on):
    cfg.skills.enabled = on
    cfg.skills.local_review = False


def _skills_off(m, client, state):
    """A present skills module that is switched off answers empty lists and refuses changes."""
    if state != "off":
        return
    for prefix in ("", "/api/admin/v1"):
        assert client.get(prefix + "/skills").json() == {"enabled": False, "proposals": [], "installed": []}
        assert client.get(prefix + "/skills/enabled").json() == []
        assert client.post(prefix + "/skills/x/enable").status_code == 400


def _notifications(cfg, tmp, on):
    cfg.notify.enabled = on
    cfg.notify.server = "http://ntfy.test/"
    cfg.notify.topic = "t"


def _notifier(m, client, state):
    async def publish(client, payload):  # nothing reaches a real ntfy server
        return None
    m.notifier.publish = publish
    if state in MISSING:  # the core drops every notification and carries on
        assert not m.notifier.enabled
        m.notifier.send({"title": "x"})
        assert m.notifier.link("/") == ""


def _local_model(cfg, tmp, on):
    cfg.gpu_guard = config.GpuGuardConfig(enabled=on, pause_flag=str(tmp / "paused"))


def _supervision(m, client, state):
    """Present, the module supervises the local model; its guard (RAM check, pause) exists only while switched on."""
    assert m.cfg.module_effective("local_model") == (state in PRESENT)
    if state not in PRESENT:
        return
    assert isinstance(m.warmer, ModelWarmer) and m.runner.warmer is m.warmer
    assert (m.guard is not None) == (m.runner.ram is not None) == (state == "on")
    assert client.get("/models/status").json()[0]["state"] == READY
    assert client.post("/models/warm").status_code == 200
    assert client.get("/api/admin/v1/resources").status_code == 200
    if state == "off":
        assert client.post("/resources/pause").status_code == 400


def _images(cfg, tmp, on):
    cfg.images = config.ImagesConfig(enabled=on, work_dir=str(tmp / "img"))


def _memory_library(cfg, tmp, on):
    cfg.memory_library.clone_dir = str(tmp / "library")
    cfg.memory_library.enabled = on


def _memory_surface(m, client, state):
    assert ("User context: memory_index" in m._optional_prompts(Project(name="scratch"), {}, None)) == (state == "on")
    for prefix in ("", "/api/admin/v1"):
        if state in PRESENT:
            assert client.get(prefix + "/memory").json()["enabled"] == (state == "on")
            assert client.put(prefix + "/memory/profile", json={"content": "x"}).status_code == 400
        else:
            assert client.get(prefix + "/memory").status_code == 404
            assert client.put(prefix + "/memory/profile", json={"content": "x"}).status_code in GONE


def _hub(cfg, tmp, on):
    cfg.config_dir = tmp / "config"
    cfg.hub.enabled = on


def _hub_surface(m, client, state):
    hub = "/api/admin/v1/hub"
    if state == "off":
        assert "disabled" in client.get(hub).text
    operations = {(op["method"], op["path"]) for op in client.get("/api/admin/v1").json()["operations"]}
    assert (("GET", hub) in operations) == (state in PRESENT)
    assert client.get("/api/admin/v1/hub-claim").status_code == 200  # the claim flow is core, not the module


def _homelab_prompt(m, client, state):
    assert ("Homelab access" in m._optional_prompts(Project(name="lab", homelab=True), {}, None)) == (state == "on")


def _mcp_client(cfg, tmp, on):
    cfg.installed.mcp_client = True
    cfg.modules.mcp_client = True


ROWS = (
    Row("backup", _backup, probe=("POST", "/maintenance/backup"), answers={"on": 200, "off": 200},
        keys=("backup.enabled", "backup.at", "backup.keep_days", "backup.dir", "backup.member_key_dir"),
        caps={"backup": "on"}, routes=("/maintenance/backup",), cli="maintenance backup", service="backup",
        service_in=PRESENT, extra=_backup_status),
    Row("endpoint", _endpoint, probe=("GET", "/v1/models"), answers={"on": 401, "off": 404},
        keys=("endpoint.enabled", "modules.endpoint", "endpoint.max_waiting", "endpoint.agent_fair_seconds"),
        caps={"endpoint": "on"}, metrics=("harness_endpoint_active",), extra=_endpoint_gate),
    Row("jobs", _jobs, probe=("GET", "/jobs"), answers={"on": 200, "off": 400},
        keys=("jobs.enabled", "jobs.poll_seconds"), caps={"jobs": "on"}, me=("jobs",), routes=("/jobs",),
        operations=("/templates",), cli="jobs list", service="jobs", extra=_jobs_run),
    Row("remote_control", states=("off", "absent", "uninstalled"), probe=("GET", "/api/admin/v1/remote-control"),
        answers={"off": 200}, keys=("remote_control.enabled", "remote_control.discovery.roots",
                                    "modules.remote_control"),
        caps={"remote_control": "on"}, features={"remote_control": "on"}, scopes=("remote_control",),
        app_capability="remote_control", top=("discovery_limits",), operations=("/remote-control/discovery/",),
        routes=("/remote-control",), cli="remote-control promote"),
    Row("runners", _runners, states=("on", "absent", "uninstalled"), probe=("GET", "/api/admin/v1/runners"),
        answers={"on": 200}, keys=("modules.runners",), caps={"runners": "on"},
        features={"runner_pairing": "on"}, me=("runners",), routes=("/runners",), cli="runner list",
        gone=("/runners", "/runner-pairing-codes", "/mac-client/install.sh", "/mac-client/package.tar.gz",
              "/mac-client/manifest.json"), extra=_runners_hub),
    Row("search", _search, probe=("GET", "/search?q=x"), answers={"on": 200, "off": 400},
        keys=("search.enabled",), caps={"search": "on"}, routes=("/search", "/api/v1/search"), cli="search",
        tools=("session_search", "session_read"), service="search", extra=_search_keeps_indexing),
    Row("skills", _skills, probe=("GET", "/skills"), answers={"on": 200, "off": 200},
        keys=("skills.enabled",), caps={"skills": "on"}, routes=("/skills", "/skills/{slug}/projects"),
        cli="skills install", metrics=("harness_skills_installed",), tools=("propose_skill",), service="skills",
        extra=_skills_off),
    Row("notifications", _notifications, probe=("POST", "/notify/test"), answers={"on": 200, "off": 400},
        keys=("notifications.enabled", "notify.server", "notify.topic", "notify.token_file",
              "modules.notifications"),
        caps={"notifications": "on"}, routes=("/notify/test",), cli="notify test", extra=_notifier),
    Row("local_model", _local_model, switch="local_model", probe=("GET", "/models/status"),
        answers={"on": 200, "off": 200},
        keys=("gpu_guard.enabled", "gpu_guard.poll_seconds", "gpu_guard.resume_after_seconds",
              "gpu_guard.drain_timeout_seconds", "modules.gpu_guard", "modules.local_model"),
        caps={"local_model": "present", "gpu_guard": "on"}, scopes=("models:warm",),
        routes=("/models/status", "/models/warm", "/gpu", "/gpu/{action}", "/resources", "/resources/diagnostics",
                "/resources/{action}", "/api/v1/models/status", "/api/v1/models/warm"), cli="gpu pause",
        extra=_supervision),
    Row("images", _images, probe=("GET", "/images"), answers={"on": 200, "off": 400},
        keys=("images.enabled", "images.edit_enabled", "images.start_timeout_seconds", "images.job_timeout_seconds",
              "images.max_upload_bytes", "images.max_pixels", "modules.images", "modules.image_edit"),
        caps={"images": "on", "image_edit": "never"}, features={"images": "on"}, scopes=("images",),
        me=("images",), app_capability="images", operations=("image",),
        routes=("/images", "/images/{iid}", "/images/{iid}/upscale", "/maintenance/image-archive/retention/apply",
                "/api/v1/images", "/api/v1/images/{iid}", "/api/v1/images/{iid}/upscale"),
        cli="images list", tools=("generate_image",), service="images", gone=("/api/admin/v1/images",)),
    Row("memory_library", _memory_library, probe=("GET", "/memory"), answers={"on": 200, "off": 200},
        keys=("memory_library.enabled", "modules.memory_library"), caps={"memory_library": "on"},
        scopes=("memory_library",), routes=("/memory",), cli="memory show",
        tools=("memory_index", "memory_search", "memory_read"), service="memory_library", extra=_memory_surface),
    Row("hub", _hub, probe=("GET", "/api/admin/v1/hub"), answers={"on": 200, "off": 400},
        keys=("hub.enabled", "modules.hub"), caps={"hub": "on"}, cli="hub status",
        metrics=("harness_hub_apps",), extra=_hub_surface),
    Row("homelab", states=("on", "absent", "uninstalled"), keys=("modules.homelab",), caps={"homelab": "on"},
        scopes=("homelab",), extra=_homelab_prompt),
    Row("mcp_client", _mcp_client, states=("on", "absent", "uninstalled"), keys=("modules.mcp_client",)),
)


def _build(tmp_path: Path, row: Row, state: str) -> Manager:
    cfg = make_cfg(tmp_path)
    cfg.runners = {}
    for section in (cfg.memory_library, cfg.remote_control, cfg.notify, cfg.gpu_guard):
        section.enabled = False
    row.configure(cfg, tmp_path, state != "off")  # a missing module keeps its config enabled, and dormant
    if state == "absent":
        cfg.module_packages = tuple(p for p in modules.namespace_packages() if p != row.package)
    if state == "uninstalled":
        setattr(cfg.installed, row.installed, False)
    return Manager(cfg, chat=Script([Completion(content="hi")]))


def _expected(mode: str, state: str) -> bool:
    return {"on": state == "on", "present": state in PRESENT, "never": False}[mode]


def _tool_names(m: Manager) -> set[str]:
    return {name for kit in m.runner.daemon_toolkits(SESSION) for name in kit.tool_names}


def _families(text: str) -> set[str]:
    return set(re.findall(r"^# TYPE (\S+)", text, re.M))


def _check(m: Manager, row: Row, state: str) -> None:
    present = state in PRESENT
    assert (row.name in m.modules) == present
    if row.service:
        assert (getattr(m, row.service) is not None) == (state in row.service_in)
    if row.app_capability:
        assert (row.app_capability in m.settings.registry.get("app.capabilities").bounds.enum) == present
    assert set(row.routes) & route_paths(create_app(m)) == (set(row.routes) if present else set())
    if row.cli:  # the CLI asks the discovered packages, not the profile
        assert (row.cli in {r[0] for r in modules.cli_rows(m.cfg)}) == (state != "absent")
    assert set(row.tools) & _tool_names(m) == (set(row.tools) if state == "on" else set())
    with TestClient(create_app(m)) as client:
        assert client.get("/health").json()["ok"]
        assert client.get("/api/admin/v1/keys").status_code == 200  # the core's credentials never depend on a module
        for path in row.gone:
            assert present or client.get(path).status_code == 404, path
        if row.extra:
            row.extra(m, client, state)
        if row.probe:
            method, path = row.probe
            status = client.request(method, path).status_code
            if present:
                assert status == row.answers[state], (row.probe, state)
            else:
                assert status in (GONE if method == "POST" else (404,)), (row.probe, state)
        document = client.get("/api/admin/v1/config/schema").json()
        schema = {entry["key"] for entry in document["settings"]}
        assert {name for name in row.top if name in document} == (set(row.top) if present else set())
        paths = [op["path"] for op in client.get("/api/admin/v1").json()["operations"]]
        for fragment in row.operations:
            assert any(fragment in path for path in paths) == present, fragment
        assert set(row.keys) & schema == (set(row.keys) if present else set())
        root = client.get("/api/v1").json()
        for name, mode in row.caps.items():
            if present:
                assert root["capabilities"]["modules"][name] is _expected(mode, state), name
            else:
                assert name not in root["capabilities"]["modules"], name
        for name, mode in row.features.items():
            if present:
                assert bool(root["features"][name]) is _expected(mode, state), name
            else:
                assert name not in root["features"], name
        for scope in row.scopes:
            assert (scope in root["scopes"]) == present, scope
        me = client.get("/me").json()["capabilities"]
        for name in row.me:
            assert (name in me) == present, name
        if row.metrics:
            metrics = _families(client.get("/metrics").text)
            assert set(row.metrics) & metrics == (set(row.metrics) if present else set())
    m.db.close()


def test_every_module_has_a_row():
    assert {module.name for module in modules.discover()} == {row.name for row in ROWS}


CASES = [(row, state) for row in ROWS for state in row.states]


@pytest.mark.parametrize("row,state", CASES, ids=[f"{row.name}-{state}" for row, state in CASES])
def test_module_lifecycle(tmp_path, row, state):
    _check(_build(tmp_path, row, state), row, state)


@pytest.mark.parametrize("row", ROWS, ids=[row.name for row in ROWS])
def test_service_profile_leaves_every_module_uninstalled(tmp_path, monkeypatch, row):
    """A real service-profile install selects no module, so none of them contributes anything."""
    monkeypatch.setattr("harness.backend_state._subscription_status", lambda *_: False)
    assert setup_config.main(["--config-dir", str(tmp_path / "cfg"), "--data-dir", str(tmp_path / "data"),
                              "--model", "gpt-oss", "--pause-flag", str(tmp_path / "paused"),
                              "--profile", "service"]) == 0
    _check(Manager(config.load(tmp_path / "cfg")), row, "uninstalled")


@pytest.mark.parametrize("package,key,value", [
    ("images", "images.job_timeout_seconds", 900),
    ("local_model", "gpu_guard.poll_seconds", 12),
    ("jobs", "jobs.poll_seconds", 50),
    ("endpoint", "endpoint.max_waiting", 9),
    ("backup", "backup.keep_days", 7),
    ("remote_control", "remote_control.discovery.max_depth", 2),
])
def test_an_absent_modules_saved_settings_stay_dormant(tmp_path, package, key, value):
    """Uninstalling a module must not quarantine a managed overlay that still sets one of its keys."""
    cfg = make_cfg(tmp_path)
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    m.settings.patch_admin({key: value}, revision=None, actor={"kind": "owner"})
    m.settings.confirm_startup()
    m.db.close()
    cfg = make_cfg(tmp_path)
    cfg.module_packages = tuple(p for p in modules.namespace_packages() if p != f"harness_modules.{package}")
    again = Manager(cfg, chat=Script([Completion(content="hi")]))
    assert key not in again.settings.registry.specs
    assert again.settings.store.read_status().get("recovery") is None
    assert again.settings.store.read_active().values[key] == value
    again.db.close()
