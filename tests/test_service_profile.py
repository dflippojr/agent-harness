"""Issue #26: hosted-provider service profile and capability discovery."""

from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from harness import config, setup_config
from harness.api import create_app
from harness.manager import Manager


def setup_args(tmp_path, *extra: str) -> list[str]:
    return [
        "--config-dir", str(tmp_path / "cfg"), "--data-dir", str(tmp_path / "data"),
        "--model", "gpt-oss", "--pause-flag", str(tmp_path / "paused"), *extra,
    ]


def test_service_profile_is_hosted_only_and_discovers_capabilities(tmp_path, monkeypatch):
    assert setup_config.main(setup_args(tmp_path, "--profile", "service")) == 0
    cfg = config.load(tmp_path / "cfg")
    assert cfg.profile == "service"
    assert cfg.models == {} and cfg.default_model == ""
    assert not any(vars(cfg.modules).values())
    assert sorted(cfg.backends) == ["claude", "codex", "cursor"]
    assert all(backend.enabled for backend in cfg.backends.values())

    # Capability discovery must not probe auth volumes or expose secrets.
    monkeypatch.setattr("harness.backend_state._subscription_status", lambda *_: False)
    manager = Manager(cfg)
    with TestClient(create_app(manager)) as client:
        health = client.get("/health").json()
        app_root = client.get("/api/v1").json()
        admin_root = client.get("/api/admin/v1").json()
        assert health["profile"] == "service"
        assert health["capabilities"] == app_root["capabilities"] == admin_root["capabilities"]
        assert health["capabilities"]["required"]["approvals"] is True
        assert health["capabilities"]["hosted_backends"] == ["claude", "codex", "cursor"]
        assert "api_key" not in str(health["capabilities"])
        assert client.get("/models/status").json() == []
        assert client.post("/models/warm").status_code == 400
        rejected = client.post("/sessions", json={"prompt": "local is off"})
        assert rejected.status_code == 400 and "local model is disabled" in rejected.json()["detail"]


def test_service_module_opt_in_and_local_dependency(tmp_path):
    setup_config.main(setup_args(tmp_path, "--profile", "service", "--enable-module", "jobs",
                                 "--enable-module", "endpoint"))
    cfg = config.load(tmp_path / "cfg")
    assert cfg.modules.jobs and cfg.jobs.enabled
    assert cfg.modules.endpoint and cfg.endpoint.enabled
    assert cfg.modules.local_model and cfg.default_model == "gpt-oss-20b"
    assert not cfg.modules.images


def test_switching_existing_full_install_preserves_configuration(tmp_path):
    args = setup_args(tmp_path)
    setup_config.main(args)
    harness_path = tmp_path / "cfg" / "harness.yaml"
    raw = yaml.safe_load(harness_path.read_text(encoding="utf-8"))
    raw["backends"] = {"codex": {"enabled": True, "api_key_file": "D:/private/codex.key",
                                    "model": "custom-codex"}}
    raw["provider_secret_files"] = {"invoice-app": "D:/private/invoice-app.key"}
    harness_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    before = harness_path.read_text(encoding="utf-8")

    setup_config.main(args + ["--profile", "service"])
    assert harness_path.read_text(encoding="utf-8") == before
    cfg = config.load(tmp_path / "cfg")
    assert cfg.profile == "service" and not cfg.modules.local_model
    assert cfg.backends["codex"].api_key_file == "D:/private/codex.key"
    assert cfg.backends["codex"].model == "custom-codex"
    assert cfg.provider_secret_files == {"invoice-app": "D:/private/invoice-app.key"}
    assert sorted(cfg.backends) == ["claude", "codex", "cursor"]

    setup_config.main(args)
    restored = config.load(tmp_path / "cfg")
    assert restored.profile == "full" and restored.modules.local_model
    assert restored.default_model == "gpt-oss-20b"


def test_new_service_install_can_be_promoted_to_full_without_force(tmp_path):
    args = setup_args(tmp_path)
    setup_config.main(args + ["--profile", "service"])
    assert config.load(tmp_path / "cfg").models == {}
    setup_config.main(args + ["--profile", "full"])
    promoted = config.load(tmp_path / "cfg")
    assert promoted.profile == "full"
    assert promoted.default_model == "gpt-oss-20b"
    assert list(promoted.models) == ["gpt-oss-20b"]


def test_invalid_profile_overlay_is_rejected(tmp_path):
    setup_config.main(setup_args(tmp_path))
    (tmp_path / "cfg" / "profile.yaml").write_text("profile: tiny\n", encoding="utf-8")
    try:
        config.load(tmp_path / "cfg")
    except ValueError as exc:
        assert "full' or 'service" in str(exc)
    else:
        raise AssertionError("invalid profile was accepted")
