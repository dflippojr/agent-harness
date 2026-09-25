"""Issue #16: self-installed Mac CLI/runner package and one-time native pairing."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

import pytest
from fastapi.testclient import TestClient

from harness import cli
from harness.api import create_app
from harness.config import RunnerConfig
from harness.manager import Manager
from test_daemon import make_cfg


def mac_manager(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.public_url = "https://tower.example.ts.net"
    cfg.runners["macbook"] = RunnerConfig(
        name="macbook", token_file=str(tmp_path / "secrets" / "runner.token"), min_free_gb=7)
    return Manager(cfg)


def test_owner_pairs_native_client_and_runner_once_without_storing_runner_secret(tmp_path):
    manager = mac_manager(tmp_path)
    with TestClient(create_app(manager)) as client:
        default_name = client.post("/runner-pairing-codes", json={"runner": "macbook"})
        assert default_name.status_code == 201
        assert default_name.json()["name"] == "Agent Harness for Mac"
        assert client.delete(f"/runner-pairing-codes/{default_name.json()['id']}").status_code == 204

        approved = client.post("/runner-pairing-codes", json={"name": "Dana's Mac", "runner": "macbook"})
        assert approved.status_code == 201
        code = approved.json()["code"]
        assert code.startswith("hrp-") and approved.headers["cache-control"] == "no-store"

        listed = client.get("/api/admin/v1/runner-pairing-codes").json()[0]
        assert listed["runner"] == "macbook" and "code" not in listed and "hash" not in listed
        runner_token = (tmp_path / "secrets" / "runner.token").read_text(encoding="utf-8").strip()
        stored = manager.db.conn.execute(
            "SELECT hash FROM runner_pairing_codes WHERE id = ?", (approved.json()["id"],)).fetchone()[0]
        assert stored != code and runner_token not in str(listed)

        assert client.post("/api/v1/runner-pair", headers={"Origin": "https://evil.example"},
                           json={"code": code}).status_code == 403
        paired = client.post("/api/v1/runner-pair", json={"code": code})
        assert paired.status_code == 201 and paired.headers["cache-control"] == "no-store"
        body = paired.json()
        assert body["server"] == manager.cfg.public_url
        assert body["owner_token"].startswith("ho-")
        assert body["runner"] == {"server": manager.cfg.public_url, "name": "macbook", "token": runner_token,
                                  "repo_roots": ["~/Projects"], "min_free_gb": 7}
        assert client.get("/api/admin/v1/sessions",
                          headers={"Authorization": f"Bearer {body['owner_token']}"}).status_code == 200
        assert client.post("/api/v1/runner-pair", json={"code": code}).status_code == 400
        assert runner_token not in json.dumps(client.get("/runner-pairing-codes").json())
        durable = b"".join(path.read_bytes() for path in manager.cfg.db_path.parent.glob(manager.cfg.db_path.name + "*"))
        assert runner_token.encode() not in durable and body["owner_token"].encode() not in durable

        expired = client.post("/runner-pairing-codes", json={"name": "Late", "runner": "macbook"}).json()
        manager.db.conn.execute("UPDATE runner_pairing_codes SET expires_at = 0 WHERE id = ?", (expired["id"],))
        keys_before = len(manager.db.list_api_keys())
        assert client.post("/api/v1/runner-pair", json={"code": expired["code"]}).status_code == 400
        assert len(manager.db.list_api_keys()) == keys_before

        cancelled = client.post("/runner-pairing-codes", json={"name": "Nope", "runner": "macbook"}).json()
        assert client.delete(f"/api/admin/v1/runner-pairing-codes/{cancelled['id']}").status_code == 204
        assert client.post("/api/v1/runner-pair", json={"code": cancelled["code"]}).status_code == 400

        app = client.post("/keys", json={"name": "app", "kind": "app", "scopes": ["sessions"]}).json()
        assert client.get("/api/admin/v1/runner-pairing-codes",
                          headers={"Authorization": f"Bearer {app['key']}"}).status_code == 403
        assert client.post("/runner-pairing-codes", json={"runner": "missing"}).status_code == 404


def test_mac_package_is_version_matched_and_contains_no_credentials(tmp_path):
    manager = mac_manager(tmp_path)
    with TestClient(create_app(manager)) as client:
        installer = client.get("/mac-client/install.sh")
        package = client.get("/mac-client/package.tar.gz")
    assert installer.status_code == package.status_code == 200
    assert "python3 -m venv" in installer.text and ".local/bin/harness" in installer.text
    with tarfile.open(fileobj=io.BytesIO(package.content), mode="r:gz") as archive:
        names = set(archive.getnames())
        contents = b"".join(archive.extractfile(name).read() for name in names)
    assert {"app/harness_runner.py", "app/sandbox.sb", "client/harness_cli.py", "client/harness_client.py",
            "dev.agent-harness.runner.plist"} <= names
    assert b"runner.token" not in contents and b"ho-" not in contents


def test_cli_pairing_project_roots_and_launchd_commands(tmp_path, monkeypatch):
    client_config = tmp_path / "client.json"
    runner_config = tmp_path / "runner.json"

    class Response:
        status_code = 201
        text = ""

        @staticmethod
        def json():
            return {"server": "https://tower.example", "owner_token": "ho-secret", "owner_key": {"id": "k-1"},
                    "runner": {"server": "https://tower.example", "name": "macbook", "token": "runner-secret",
                               "repo_roots": ["~/Projects"], "min_free_gb": 10}, "api_version": "1.7"}

    seen = []
    monkeypatch.setattr(cli.httpx, "post", lambda url, **kwargs: seen.append((url, kwargs)) or Response())
    paired = cli.pair_native("https://tower.example/", "hrp-once", client_config, runner_config)
    assert seen == [("https://tower.example/api/v1/runner-pair", {
        "json": {"code": "hrp-once"}, "timeout": 60,
        "headers": {"X-Agent-Harness-Client": "cli/2"},
    })]
    assert json.loads(client_config.read_text()) == {"server": "https://tower.example", "token": "ho-secret"}
    assert json.loads(runner_config.read_text())["token"] == "runner-secret"
    assert paired["owner_key"]["id"] == "k-1"

    project = tmp_path / "Projects" / "demo"
    project.mkdir(parents=True)
    added = cli.add_project_root(str(project), runner_config)
    cli.add_project_root(str(project), runner_config)
    assert added == project.resolve()
    roots = json.loads(runner_config.read_text())["repo_roots"]
    assert roots.count(str(project.resolve())) == 1

    calls = []
    monkeypatch.setattr(cli.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(cli.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs))
                        or subprocess.CompletedProcess(args, 0))
    cli.launchctl("kickstart", "-k", check=True)
    assert calls == [(["launchctl", "kickstart", "-k", "gui/501/dev.agent-harness.runner"],
                      {"check": True, "text": True})]


def test_paired_cli_uses_owner_api_and_bearer_token(tmp_path, monkeypatch):
    config = tmp_path / "client.json"
    config.write_text(json.dumps({"server": "https://tower.example", "token": "ho-client"}), encoding="utf-8")
    cli.configure(config)
    seen = []

    def request(method, url, timeout, **kwargs):
        seen.append((method, url, timeout, kwargs))
        import httpx
        return httpx.Response(200, json=[])

    monkeypatch.setattr(cli.httpx, "request", request)
    assert cli.api("GET", "/sessions") == []
    assert seen == [("GET", "https://tower.example/api/admin/v1/sessions", 60,
                     {"headers": {"X-Agent-Harness-Client": "cli/2", "Authorization": "Bearer ho-client"}})]


def test_mac_install_script_supports_pairing_and_legacy_update():
    script = (Path(__file__).parent.parent / "macrunner" / "install.sh").read_text(encoding="utf-8")
    assert "--server" in script and "--code" in script and "/api/v1/runner-pair" not in script
    assert "package.tar.gz" in script and "pip install" in script and "launchctl bootstrap" in script
    assert "agent_harness_client.pth" in script
    assert "harness_update.py" in script and "harness_compat.py" in script


def harness_home(tmp_path, monkeypatch):
    """Point the CLI's ~/.agent-harness at a temporary directory."""
    home = tmp_path / "home" / ".agent-harness"
    monkeypatch.setattr(cli, "HARNESS_HOME", home)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", home / "client" / "config.json")
    monkeypatch.setattr(cli, "DEFAULT_RUNNER_CONFIG", home / "runner" / "config.json")
    return home


def test_cli_config_paths_must_stay_under_the_harness_home(tmp_path, monkeypatch, capsys):
    """Issue #221 (S2083): --config / --runner-config can't send the paired credentials anywhere else."""
    home = harness_home(tmp_path, monkeypatch)
    posted = []

    class Response:
        status_code = 201

        @staticmethod
        def json():
            return {"server": "https://tower.example", "owner_token": "ho-secret",
                    "runner": {"server": "https://tower.example", "name": "macbook", "token": "runner-secret"}}

    monkeypatch.setattr(cli.httpx, "post", lambda url, **kwargs: posted.append(url) or Response())
    outside = tmp_path / "elsewhere.json"
    for extra in (["--config", str(outside), "pair", "https://tower.example", "hrp-1"],
                  ["pair", "https://tower.example", "hrp-1", "--runner-config", str(outside)],
                  ["pair", "https://tower.example", "hrp-1", "--runner-config", str(home / ".." / "runner.json")],
                  ["projects", "add", str(tmp_path), "--runner-config", str(outside)]):
        monkeypatch.setattr(cli.sys, "argv", ["harness", *extra])
        with pytest.raises(SystemExit) as exit_:
            cli.main()
        assert exit_.value.code == 2
    assert posted == [] and not outside.exists() and not (tmp_path / "home" / "runner.json").exists()
    assert "must be a file under" in capsys.readouterr().err

    monkeypatch.setattr(cli.sys, "argv", ["harness", "--config", str(home / "client" / "config.json"), "pair",
                                          "https://tower.example", "hrp-1"])
    assert cli.main() == 0
    assert json.loads((home / "client" / "config.json").read_text())["token"] == "ho-secret"
    assert json.loads((home / "runner" / "config.json").read_text())["token"] == "runner-secret"


def test_private_json_never_writes_through_a_file_at_the_temp_name(tmp_path):
    """Issue #221: whatever already sits at `<name>.new` is replaced, not written through."""
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    target = tmp_path / "client" / "config.json"
    target.parent.mkdir()
    os.link(outside, target.with_name("config.json.new"))
    cli._write_private_json(target, {"token": "ho-secret"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"token": "ho-secret"}
    assert outside.read_text(encoding="utf-8") == "keep\n"
    assert not target.with_name("config.json.new").exists()
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600


def test_runner_logs_passes_only_a_count_and_the_log_to_tail(tmp_path, monkeypatch):
    """Issue #221 (S8705): --lines reaches tail as a whole number, and `--` ends tail's options."""
    home = harness_home(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(cli.subprocess, "call", lambda args: calls.append(args) or 0)
    for lines, follow in (("0", True), ("250", False)):
        monkeypatch.setattr(cli.sys, "argv", ["harness", "runner", "logs", "--lines", lines,
                                              *(["--follow"] if follow else [])])
        assert cli.main() == 0
    log = str(home / "logs" / "runner.log")
    assert calls == [["tail", "-n", "1", "-f", "--", log], ["tail", "-n", "250", "--", log]]
    with pytest.raises(ValueError):
        cli.tail_command(Path(log), "-f --pid=1", False)
