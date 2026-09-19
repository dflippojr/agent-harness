"""Issue #69: first-party protocol compatibility and transactional client updates."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tarfile
import io

import pytest
from fastapi.testclient import TestClient

from harness import compat, mac_client, remote, updater
from harness.api import create_app
from harness.config import RunnerConfig
from harness.manager import Manager
from test_daemon import make_cfg


def manager_with_runner(tmp_path: Path) -> Manager:
    cfg = make_cfg(tmp_path)
    cfg.public_url = "https://tower.example"
    token = tmp_path / "runner.token"
    token.write_text("runner-secret\n", encoding="utf-8")
    cfg.runners["macbook"] = RunnerConfig(name="macbook", token_file=str(token))
    return Manager(cfg)


@pytest.mark.parametrize("surface,path,kind,auth", [
    ("app", "/api/v1/backends?auth=skip", "web", {}),
    ("admin", "/api/admin/v1/me", "cli", {}),
    ("runner", "/runners/macbook/poll", "runner", {"Authorization": "Bearer runner-secret"}),
])
def test_protocol_accepts_current_and_previous_and_rejects_skew(tmp_path, surface, path, kind, auth, monkeypatch):
    manager = manager_with_runner(tmp_path)
    monkeypatch.setattr(remote, "POLL_HOLD_SECONDS", 0.01)
    body = {"instance": "i", "inflight": [], "info": {"protocol": 2}} if surface == "runner" else None
    with TestClient(create_app(manager)) as client:
        call = client.post if body is not None else client.get
        for version in (compat.PROTOCOLS[surface]["min"], compat.PROTOCOLS[surface]["max"]):
            response = call(path, headers={**auth, compat.CLIENT_HEADER: f"{kind}/{version}"}, **({"json": body} if body else {}))
            assert response.status_code == 200, response.text
        old = call(path, headers={**auth, compat.CLIENT_HEADER: f"{kind}/0"}, **({"json": body} if body else {}))
        assert old.status_code == 426 and old.json()["error"]["code"] == "client_update_required"
        new = call(path, headers={**auth, compat.CLIENT_HEADER: f"{kind}/99"}, **({"json": body} if body else {}))
        assert new.status_code == 426 and new.json()["error"]["code"] == "daemon_update_required"


def test_discovery_is_always_reachable_and_omitted_header_has_transition_notice(tmp_path):
    manager = manager_with_runner(tmp_path)
    with TestClient(create_app(manager)) as client:
        health = client.get("/health", headers={compat.CLIENT_HEADER: "web/99"})
        assert health.status_code == 200
        data = health.json()
        assert data["release"] == compat.RELEASE and data["build_id"] == compat.BUILD_ID
        assert data["protocols"] == compat.PROTOCOLS
        assert data["minimum_clients"]["runner"] == "4.1"
        serialized = json.dumps(data)
        assert "runner-secret" not in serialized and str(tmp_path) not in serialized

        for root, header in (("/api/v1", "web/99"), ("/api/admin/v1", "cli/99")):
            response = client.get(root, headers={compat.CLIENT_HEADER: header})
            assert response.status_code == 200
            assert response.json()["protocols"] == compat.PROTOCOLS

        legacy = client.get("/api/admin/v1/me")
        assert legacy.status_code == 200
        assert legacy.headers["x-agent-harness-deprecation"] == "missing_client_version"


def hosted_web_client(tmp_path):
    manager = manager_with_runner(tmp_path)
    origin = "https://web.example"
    _, token = manager.db.create_api_key("Agent Harness Web", "admin", "owner", [origin])
    return manager, origin, {"Authorization": f"Bearer {token}", "Origin": origin}


def test_separately_hosted_web_can_preflight_and_read_health(tmp_path):
    manager, origin, headers = hosted_web_client(tmp_path)
    with TestClient(create_app(manager)) as client:
        preflight = client.options("/health", headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-agent-harness-client",
        })
        assert preflight.status_code == 204
        response = client.get("/health", headers={
            **headers, compat.CLIENT_HEADER: "web/2",
        })
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin


def test_hosted_web_compatibility_errors_keep_cors_headers(tmp_path):
    manager, origin, headers = hosted_web_client(tmp_path)
    with TestClient(create_app(manager)) as client:
        compatible = client.get("/api/admin/v1/me", headers={**headers, compat.CLIENT_HEADER: "web/2"})
        assert compatible.status_code == 200
        assert compatible.headers["access-control-allow-origin"] == origin

        for path, header, status, code in (
            ("/api/admin/v1/me", "web/99", 426, "daemon_update_required"),
            ("/api/v1/backends", "web/99", 426, "daemon_update_required"),
            ("/api/admin/v1/me", "web/0", 426, "client_update_required"),
            ("/api/v1/backends", "web/0", 426, "client_update_required"),
            ("/api/admin/v1/me", "not-a-client", 400, "invalid_client_identity"),
            ("/api/v1/backends", "web", 400, "invalid_client_identity"),
        ):
            response = client.get(path, headers={**headers, compat.CLIENT_HEADER: header})
            assert response.status_code == status, response.text
            assert response.headers["access-control-allow-origin"] == origin
            assert response.headers["vary"] == "Origin"
            assert response.json()["error"]["code"] == code


def test_new_main_routes_reject_version_skew_with_cors(tmp_path):
    """Household, config-registry, skills, and smart-approvals routes stay behind the compatibility guard."""
    manager, origin, headers = hosted_web_client(tmp_path)
    with TestClient(create_app(manager)) as client:
        for path in (
            "/api/admin/v1/accounts",  # #102 household accounts
            "/api/admin/v1/config",    # #103 typed config registry
            "/api/admin/v1/skills",    # #100 agent-written skills
            "/api/admin/v1/smart-approvals",  # #99 smart approvals
        ):
            response = client.get(path, headers={**headers, compat.CLIENT_HEADER: "web/99"})
            assert response.status_code == 426, (path, response.text)
            assert response.headers["access-control-allow-origin"] == origin
            assert response.headers["vary"] == "Origin"
            assert response.json()["error"]["code"] == "daemon_update_required"


def test_mac_package_manifest_is_version_matched_and_hash_verified(tmp_path):
    manager = manager_with_runner(tmp_path)
    with TestClient(create_app(manager)) as client:
        manifest = client.get("/mac-client/manifest.json").json()
        package = client.get(manifest["package_url"]).content
    assert manifest["version"] == compat.MAC_CLIENT_VERSION
    assert manifest["runner_protocol"] == compat.CLIENT_PROTOCOLS["runner"]
    assert manifest["bytes"] == len(package)
    import hashlib
    assert manifest["sha256"] == hashlib.sha256(package).hexdigest()


def test_packaged_cli_and_runner_have_their_version_and_update_dependencies(tmp_path):
    with tarfile.open(fileobj=io.BytesIO(mac_client.package_bytes()), mode="r:gz") as archive:
        archive.extractall(tmp_path)
    cli_help = subprocess.run([sys.executable, str(tmp_path / "client/harness_cli.py"), "--help"],
                              capture_output=True, text=True, check=False)
    assert cli_help.returncode == 0, cli_help.stderr
    probe = subprocess.run([sys.executable, "-c",
                            "import sys; sys.path.insert(0, sys.argv[1]); import harness_runner; "
                            "print(harness_runner.VERSION)", str(tmp_path / "app")],
                           capture_output=True, text=True, check=False)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == compat.MAC_CLIENT_VERSION


def installed_runtime(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    base = home / ".agent-harness"
    (base / "runner/app").mkdir(parents=True)
    (base / "runner/app/old.txt").write_text("old runner", encoding="utf-8")
    (base / "runner/config.json").write_text('{"token":"runner-secret"}', encoding="utf-8")
    (base / "client").mkdir()
    (base / "client/old.txt").write_text("old client", encoding="utf-8")
    (base / "client/config.json").write_text('{"token":"owner-secret"}', encoding="utf-8")
    plist = home / "Library/LaunchAgents/dev.agent-harness.runner.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("old plist", encoding="utf-8")
    return home, base


def fake_download(monkeypatch, *, bad_hash: bool = False):
    package = mac_client.package_bytes()
    manifest = mac_client.package_manifest()
    if bad_hash:
        manifest["sha256"] = "0" * 64

    def download(url, destination=None):
        if destination is None:
            return json.dumps(manifest).encode()
        destination.write_bytes(package)
        return b""
    monkeypatch.setattr(updater, "_download", download)


def packaged_plist_text(home: Path, base: Path) -> str:
    raw = (Path(mac_client.ROOT) / "macrunner/dev.agent-harness.runner.plist").read_text(encoding="utf-8")
    return raw.replace("__HOME__", str(home)).replace("__PYTHON__", str(base / "venv" / "bin" / "python"))


def fake_launchctl(monkeypatch, *, fail_on: str | None = None, print_loaded: bool = False,
                   fail_times: int | None = None, plist: Path | None = None):
    calls = []
    remaining = fail_times
    loaded = True
    active_plist = plist

    def run(args, **kwargs):
        nonlocal remaining, loaded, active_plist
        calls.append(list(args))
        command = args[1] if len(args) > 1 else ""
        if fail_on and command == fail_on and (remaining is None or remaining > 0):
            if remaining is not None:
                remaining -= 1
            raise subprocess.CalledProcessError(1, args)
        if command == "bootout" and not print_loaded:
            loaded = False
        elif command == "bootstrap":
            loaded = True
            if len(args) > 3:
                active_plist = Path(args[3])
        if command == "print":
            if loaded or print_loaded:
                stdout = "dev.agent-harness.runner"
                if active_plist and Path(active_plist).is_file():
                    stdout = Path(active_plist).read_text(encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(updater.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(updater.subprocess, "run", run)
    monkeypatch.setattr(updater, "_BOOTOUT_POLL_SECONDS", 0)
    return calls


def test_mac_update_is_atomic_preserves_credentials_and_restarts(tmp_path, monkeypatch):
    home, base = installed_runtime(tmp_path)
    fake_download(monkeypatch)
    calls = fake_launchctl(monkeypatch)
    result = updater.apply_update("https://tower.example", base, home=home)
    assert result["ok"] and result["version"] == compat.MAC_CLIENT_VERSION
    assert (base / "runner/app/harness_runner.py").is_file()
    assert json.loads((base / "runner/config.json").read_text())["token"] == "runner-secret"
    assert json.loads((base / "client/config.json").read_text())["token"] == "owner-secret"
    plist = home / "Library/LaunchAgents/dev.agent-harness.runner.plist"
    assert result["plist_changed"] is True
    assert calls == [
        ["launchctl", "bootout", "gui/501/dev.agent-harness.runner"],
        ["launchctl", "print", "gui/501/dev.agent-harness.runner"],
        ["launchctl", "bootstrap", "gui/501", str(plist)],
        ["launchctl", "enable", "gui/501/dev.agent-harness.runner"],
        ["launchctl", "print", "gui/501/dev.agent-harness.runner"],
    ]
    assert json.loads((base / "runner/last-update.json").read_text())["ok"] is True
    assert not list(base.glob(".update-*"))
    assert "ProgramArguments" in plist.read_text(encoding="utf-8")


def test_mac_update_hash_failure_and_restart_failure_preserve_prior_runtime(tmp_path, monkeypatch):
    home, base = installed_runtime(tmp_path)
    fake_download(monkeypatch, bad_hash=True)
    with pytest.raises(RuntimeError, match="SHA-256"):
        updater.apply_update("https://tower.example", base, home=home)
    assert (base / "runner/app/old.txt").read_text() == "old runner"
    assert (base / "client/old.txt").read_text() == "old client"

    fake_download(monkeypatch)
    fake_launchctl(monkeypatch, fail_on="bootstrap")
    with pytest.raises(RuntimeError, match="prior client preserved"):
        updater.apply_update("https://tower.example", base, home=home)
    assert (base / "runner/app/old.txt").read_text() == "old runner"
    assert (base / "client/old.txt").read_text() == "old client"
    assert json.loads((base / "client/config.json").read_text())["token"] == "owner-secret"
    recorded = json.loads((base / "runner/last-update.json").read_text())
    assert recorded["ok"] is False
    assert "prior client preserved" in recorded["message"]


def test_mac_update_reload_failure_restores_prior_plist_into_launchd(tmp_path, monkeypatch):
    home, base = installed_runtime(tmp_path)
    fake_download(monkeypatch)
    plist = home / "Library/LaunchAgents/dev.agent-harness.runner.plist"
    bootout = ["launchctl", "bootout", "gui/501/dev.agent-harness.runner"]
    probe = ["launchctl", "print", "gui/501/dev.agent-harness.runner"]
    bootstrap = ["launchctl", "bootstrap", "gui/501", str(plist)]
    enable = ["launchctl", "enable", "gui/501/dev.agent-harness.runner"]
    reload = [bootout, probe, bootstrap, enable, probe]
    expected = {
        "bootstrap": [bootout, probe, bootstrap] + reload,
        "enable": [bootout, probe, bootstrap, enable] + reload,
    }
    for command, want in expected.items():
        calls = fake_launchctl(monkeypatch, fail_on=command, fail_times=1)
        with pytest.raises(RuntimeError, match="prior client preserved"):
            updater.apply_update("https://tower.example", base, home=home)
        assert (base / "runner/app/old.txt").read_text() == "old runner"
        assert plist.read_text() == "old plist"
        assert json.loads((base / "client/config.json").read_text())["token"] == "owner-secret"
        recorded = json.loads((base / "runner/last-update.json").read_text())
        assert recorded["ok"] is False
        assert calls == want


def test_mac_update_stuck_launchd_job_is_not_reported_ok(tmp_path, monkeypatch):
    home, base = installed_runtime(tmp_path)
    fake_download(monkeypatch)
    calls = fake_launchctl(monkeypatch, print_loaded=True)
    with pytest.raises(RuntimeError, match="did not unload after bootout"):
        updater.apply_update("https://tower.example", base, home=home)
    assert (base / "runner/app/old.txt").read_text() == "old runner"
    assert json.loads((base / "runner/last-update.json").read_text())["ok"] is False
    unload = ["bootout"] + ["print"] * 5
    assert [call[1] for call in calls] == unload + unload


def test_interrupted_mac_update_staging_leaves_runtime_and_credentials_untouched(tmp_path, monkeypatch):
    home, base = installed_runtime(tmp_path)
    fake_download(monkeypatch)
    monkeypatch.setattr(updater, "_safe_extract", lambda *args: (_ for _ in ()).throw(
        OSError("simulated interrupted staging")))
    with pytest.raises(RuntimeError, match="interrupted staging"):
        updater.apply_update("https://tower.example", base, home=home)
    assert (base / "runner/app/old.txt").read_text() == "old runner"
    assert (base / "client/old.txt").read_text() == "old client"
    assert json.loads((base / "runner/config.json").read_text())["token"] == "runner-secret"
    assert json.loads((base / "client/config.json").read_text())["token"] == "owner-secret"


def test_web_update_reloads_changed_plist_via_shared_handoff(tmp_path, monkeypatch):
    home, base = installed_runtime(tmp_path)
    fake_download(monkeypatch)
    plist = home / "Library/LaunchAgents/dev.agent-harness.runner.plist"
    assert updater.apply_update("https://tower.example", base, restart=False, home=home)["plist_changed"] is True
    assert "ProgramArguments" in plist.read_text(encoding="utf-8")
    previous = updater.previous_plist_path(base)
    assert previous.read_text(encoding="utf-8") == "old plist"
    calls = fake_launchctl(monkeypatch, plist=plist)
    recorded = updater.perform_launchd_handoff(
        plist, definition_changed=True, previous_plist=previous, base=base)
    assert recorded["ok"] is True
    assert calls == [
        ["launchctl", "bootout", "gui/501/dev.agent-harness.runner"],
        ["launchctl", "print", "gui/501/dev.agent-harness.runner"],
        ["launchctl", "bootstrap", "gui/501", str(plist)],
        ["launchctl", "enable", "gui/501/dev.agent-harness.runner"],
        ["launchctl", "print", "gui/501/dev.agent-harness.runner"],
    ]
    assert not previous.exists()
    assert json.loads((base / "runner/last-update.json").read_text())["ok"] is True


def test_unchanged_plist_update_kickstarts(tmp_path, monkeypatch):
    home, base = installed_runtime(tmp_path)
    plist = home / "Library/LaunchAgents/dev.agent-harness.runner.plist"
    plist.write_text(packaged_plist_text(home, base), encoding="utf-8")
    fake_download(monkeypatch)
    result = updater.apply_update("https://tower.example", base, restart=False, home=home)
    assert result["ok"] and result["plist_changed"] is False
    calls = fake_launchctl(monkeypatch, plist=plist)
    recorded = updater.perform_launchd_handoff(
        plist, definition_changed=False, previous_plist=updater.previous_plist_path(base), base=base)
    assert recorded["ok"] is True
    assert calls == [
        ["launchctl", "kickstart", "-k", "gui/501/dev.agent-harness.runner"],
        ["launchctl", "print", "gui/501/dev.agent-harness.runner"],
    ]
    cli_calls = fake_launchctl(monkeypatch, plist=plist)
    cli = updater.apply_update("https://tower.example", base, home=home)
    assert cli["plist_changed"] is False
    assert cli_calls == [
        ["launchctl", "kickstart", "-k", "gui/501/dev.agent-harness.runner"],
        ["launchctl", "print", "gui/501/dev.agent-harness.runner"],
    ]


def test_handoff_bootstrap_failure_restores_previous_plist(tmp_path, monkeypatch):
    home, base = installed_runtime(tmp_path)
    fake_download(monkeypatch)
    plist = home / "Library/LaunchAgents/dev.agent-harness.runner.plist"
    updater.apply_update("https://tower.example", base, restart=False, home=home)
    previous = updater.previous_plist_path(base)
    prior_text = previous.read_text(encoding="utf-8")
    assert prior_text == "old plist"
    assert prior_text != plist.read_text(encoding="utf-8")
    calls = fake_launchctl(monkeypatch, fail_on="bootstrap", fail_times=1, plist=plist)
    recorded = updater.perform_launchd_handoff(
        plist, definition_changed=True, previous_plist=previous, base=base)
    assert recorded["ok"] is False
    assert "prior launchd job restored" in recorded["message"]
    assert plist.read_text(encoding="utf-8") == prior_text
    assert json.loads((base / "runner/last-update.json").read_text())["ok"] is False
    bootout = ["launchctl", "bootout", "gui/501/dev.agent-harness.runner"]
    probe = ["launchctl", "print", "gui/501/dev.agent-harness.runner"]
    bootstrap = ["launchctl", "bootstrap", "gui/501", str(plist)]
    enable = ["launchctl", "enable", "gui/501/dev.agent-harness.runner"]
    assert calls == [bootout, probe, bootstrap] + [bootout, probe, bootstrap, enable, probe]


def test_runner_schedules_shared_handoff_after_posting_update_result(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "macrunner"))
    import harness_runner
    home, base = installed_runtime(tmp_path)
    updater.previous_plist_path(base).write_text("old plist", encoding="utf-8")
    scheduled = []
    posted = []
    monkeypatch.setattr(harness_runner, "schedule_launchd_handoff",
                        lambda *args, **kwargs: scheduled.append((args, kwargs)))
    executor = harness_runner.Executor(
        workspaces=tmp_path / "ws", repo_roots=[tmp_path], profile=None, home=home, min_free_gb=0,
        server="https://tower.example")
    monkeypatch.setattr(executor, "handle", lambda rid, op, params: {
        "ok": True, "version": "4.2", "plist_changed": True})

    class FakeClient:
        def post(self, path, body, timeout):
            posted.append(body)
            return {}

    harness_runner.Runner(FakeClient(), executor).work(
        {"id": "req-1", "op": "update_client", "params": {}})
    assert posted and posted[0]["ok"] is True and posted[0]["id"] == "req-1"
    assert scheduled and scheduled[0][1]["definition_changed"] is True
    assert scheduled[0][1]["previous_plist"] == updater.previous_plist_path(base)
    assert scheduled[0][1]["base"] == base
    argv = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda args, **kwargs: argv.append((args, kwargs)) or type(
        "P", (), {})())
    updater.schedule_launchd_handoff(
        home / "Library/LaunchAgents/dev.agent-harness.runner.plist",
        definition_changed=True, previous_plist=updater.previous_plist_path(base), base=base,
        python=sys.executable, app_dir=base / "runner" / "app")
    assert argv and "kickstart" not in " ".join(argv[0][0])
    assert "perform_launchd_handoff" in argv[0][0][2]
    assert "HARNESS_LAUNCHD_HANDOFF" in argv[0][1]["env"]


def test_old_runner_gets_exact_manual_update_fallback(tmp_path):
    manager = manager_with_runner(tmp_path)
    state = manager.hub.state["macbook"]
    state.last_seen = 10**12  # online for the duration of this deterministic test
    state.info = {"version": "4.1", "protocol": 1}
    with TestClient(create_app(manager)) as client:
        response = client.post("/api/admin/v1/runners/macbook/update",
                               headers={compat.CLIENT_HEADER: "web/2"})
    assert response.status_code == 409
    assert "harness update" in response.json()["detail"]
    status = manager.hub.status()[0]
    assert status["compatibility"]["state"] == "compatible"
    assert status["update_supported"] is False and status["manual_update"] == "harness update"


def test_web_bundle_has_safe_cache_update_and_version_handshake():
    root = Path(__file__).parents[1] / "harness/web"
    app = (root / "app.js").read_text(encoding="utf-8")
    client = (root / "client.mjs").read_text(encoding="utf-8")
    worker = (root / "sw.js").read_text(encoding="utf-8")
    assert f'WEB_BUILD_ID = "{compat.WEB_BUILD_ID}"' in client
    assert f'BUILD_ID = "{compat.WEB_BUILD_ID}"' in worker
    assert "X-Agent-Harness-Client" in client and "compatibility()" in client
    assert "hasUnsavedInput" in app and "Reload and update" in app
    assert "sessionStorage" in app and "PURGE_SHELL" in app
    assert "sessionStorage.setItem(UPDATE_GUARD, WEB_BUILD_ID)" in app
    assert "harness.webUpdatePrompt.${available}" not in app
    assert "harness-shell-${BUILD_ID}" in worker and "PURGE_SHELL" in worker
    assert "event.origin !== self.location.origin" in worker
    assert "api/v1" not in worker and "api/admin" not in worker
