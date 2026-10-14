"""Issue #16: self-installed Mac CLI/runner package and one-time native pairing."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile

import pytest
from fastapi.testclient import TestClient

from harness import cli
from harness.api import create_app
from harness.config import RunnerConfig
from harness.manager import Manager
from harness_modules.runners import mac_client
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
        assert code.startswith("hrp-")
        assert approved.headers["cache-control"] == "no-store"

        listed = client.get("/api/admin/v1/runner-pairing-codes").json()[0]
        assert listed["runner"] == "macbook"
        assert "code" not in listed
        assert "hash" not in listed
        runner_token = (tmp_path / "secrets" / "runner.token").read_text(encoding="utf-8").strip()
        stored = manager.db.conn.execute(
            "SELECT hash FROM runner_pairing_codes WHERE id = ?", (approved.json()["id"],)).fetchone()[0]
        assert stored != code
        assert runner_token not in str(listed)

        assert client.post("/api/v1/runner-pair", headers={"Origin": "https://evil.example"},
                           json={"code": code}).status_code == 403
        paired = client.post("/api/v1/runner-pair", json={"code": code})
        assert paired.status_code == 201
        assert paired.headers["cache-control"] == "no-store"
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
        assert runner_token.encode() not in durable
        assert body["owner_token"].encode() not in durable

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
    assert "python3 -m venv" in installer.text
    assert ".local/bin/harness" in installer.text
    with tarfile.open(fileobj=io.BytesIO(package.content), mode="r:gz") as archive:
        names = set(archive.getnames())
        contents = b"".join(archive.extractfile(name).read() for name in names)
    assert {"app/harness_runner.py", "app/sandbox.sb", "client/harness_cli.py", "client/harness_client.py",
            "dev.agent-harness.runner.plist"} <= names
    assert b"runner.token" not in contents
    assert b"ho-" not in contents


def test_cli_pairs_through_the_daemon_and_then_calls_the_owner_api(tmp_path, monkeypatch):
    home = harness_home(tmp_path, monkeypatch)
    manager = mac_manager(tmp_path)
    with TestClient(create_app(manager)) as daemon:
        code = daemon.post("/runner-pairing-codes", json={"name": "Dana's Mac", "runner": "macbook"}).json()["code"]
        sent = []

        def post(url, *, timeout, **kwargs):
            sent.append(("POST", url, kwargs.get("headers") or {}))
            return daemon.post(url, **kwargs)

        def request(method, url, *, timeout, **kwargs):
            sent.append((method, url, kwargs.get("headers") or {}))
            return daemon.request(method, url, **kwargs)

        monkeypatch.setattr(cli.httpx, "post", post)
        monkeypatch.setattr(cli.httpx, "request", request)
        server = manager.cfg.public_url
        monkeypatch.setattr(cli.sys, "argv", ["harness", "pair", server, code])
        assert cli.main() == 0
        assert sent == [("POST", server + "/api/v1/runner-pair", {"X-Agent-Harness-Client": "cli/2"})]

        client_config = json.loads((home / "client" / "config.json").read_text(encoding="utf-8"))
        runner_config = json.loads((home / "runner" / "config.json").read_text(encoding="utf-8"))
        runner_token = (tmp_path / "secrets" / "runner.token").read_text(encoding="utf-8").strip()
        assert client_config == {"server": server, "token": client_config["token"]}
        assert client_config["token"].startswith("ho-")
        assert (runner_config["name"], runner_config["token"]) == ("macbook", runner_token)

        cli.configure(home / "client" / "config.json")
        assert cli.api("GET", "/sessions") == []
        method, url, headers = sent[-1]
        assert (method, url) == ("GET", server + "/api/admin/v1/sessions")
        assert headers["Authorization"] == f"Bearer {client_config['token']}"

        with pytest.raises(SystemExit) as replayed:  # a code works once
            cli.main()
        assert "pairing failed (400)" in str(replayed.value.code)


def test_cli_project_roots_and_launchd_commands(tmp_path, monkeypatch):
    runner_config = tmp_path / "runner.json"
    runner_config.write_text(json.dumps({"name": "macbook", "token": "runner-secret", "repo_roots": ["~/Projects"]}),
                             encoding="utf-8")

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
    assert posted == []
    assert not outside.exists()
    assert not (tmp_path / "home" / "runner.json").exists()
    assert "must be a file under" in capsys.readouterr().err

    monkeypatch.setattr(cli.sys, "argv", ["harness", "--config", str(home / "client" / "config.json"), "pair",
                                          "https://tower.example", "hrp-1"])
    assert cli.main() == 0
    assert json.loads((home / "client" / "config.json").read_text())["token"] == "ho-secret"
    assert json.loads((home / "runner" / "config.json").read_text())["token"] == "runner-secret"


def test_private_json_never_writes_through_a_file_at_the_temp_name(tmp_path):
    """Issue #221: a file or link sitting at a guessable temp name is never written through."""
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    target = tmp_path / "client" / "config.json"
    target.parent.mkdir()
    os.link(outside, target.with_name("config.json.new"))
    cli._write_private_json(target, {"token": "ho-secret"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"token": "ho-secret"}
    assert outside.read_text(encoding="utf-8") == "keep\n"
    assert target.with_name("config.json.new").stat().st_nlink == 2  # the planted link is left alone
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600


def test_runner_logs_print_last_lines_without_external_command(tmp_path, monkeypatch, capsys):
    """Issue #221 (S8705): runner logs stay in Python and never pass CLI input to an OS command."""
    home = harness_home(tmp_path, monkeypatch)
    log = home / "logs" / "runner.log"
    log.parent.mkdir(parents=True)
    log.write_text("one\ntwo\nthree\n", encoding="utf-8")
    monkeypatch.setattr(cli.subprocess, "call", lambda *_: pytest.fail("runner logs must not invoke a command"))
    for lines, expected in (("0", "three\n"), ("2", "two\nthree\n")):
        monkeypatch.setattr(cli.sys, "argv", ["harness", "runner", "logs", "--lines", lines])
        assert cli.main() == 0
        assert capsys.readouterr().out == expected


def test_runner_logs_follow_appended_lines(tmp_path, monkeypatch, capsys):
    home = harness_home(tmp_path, monkeypatch)
    log = home / "logs" / "runner.log"
    log.parent.mkdir(parents=True)
    log.write_text("older\nlatest\n", encoding="utf-8")
    sleeps = 0

    def append_then_stop(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            with log.open("a", encoding="utf-8") as stream:
                stream.write("followed\n")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", append_then_stop)
    monkeypatch.setattr(cli.sys, "argv", ["harness", "runner", "logs", "--lines", "1", "--follow"])
    assert cli.main() == 130
    assert capsys.readouterr().out == "latest\nfollowed\n"


def test_packaged_standalone_cli_preview_prints_disclosures_and_digest(tmp_path):
    with tarfile.open(fileobj=io.BytesIO(mac_client.package_bytes()), mode="r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    disclosures = [{"scope": "example:read", "tier": "standard", "text": "Read café files."}]
    probe = """
import json
from pathlib import Path
import sys
import harness_cli as cli
cli.HARNESS_HOME = Path(sys.argv[1])
cli.DEFAULT_CONFIG = cli.HARNESS_HOME / 'client.json'
cli._local_owner_token = lambda: ''
values = json.loads(sys.argv[2])
def api(method, path, **kwargs):
    assert (method, path, kwargs) == ('POST', '/pairing-requests/preview', {'json': {'scopes': ['example:read']}})
    return values
cli.api = api
sys.argv = ['harness', 'pairing-requests', 'preview', '--scopes', 'example:read']
raise SystemExit(cli.main())
"""
    result = subprocess.run([sys.executable, "-c", probe, str(tmp_path), json.dumps(disclosures)],
                            cwd=tmp_path / "client", capture_output=True, text=True, encoding="utf-8", timeout=30,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == disclosures
    canonical = json.dumps(disclosures, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert result.stderr.strip() == f"Disclosures digest: {digest}"
