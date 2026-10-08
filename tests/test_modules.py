"""Issue #334 stage (b): optional modules plug in through harness/modules.py, and images is the first of them.

Images registers its routes, agent tool, settings, capabilities and CLI rows through the interface; with images
absent (service profile, or its package not installed) the daemon starts and none of them exist; and the core never
imports an add-on package."""

from __future__ import annotations

import ast
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from harness import cli, config, modules, setup_config
from harness.admin import PREFIX
from harness.api import create_app
from harness.llm import Completion
from harness.manager import Manager
from harness.settings_keys import build_registry

from test_daemon import Script, make_cfg

ROOT = Path(__file__).resolve().parents[1]
IMAGE_SETTINGS = ("images.enabled", "images.edit_enabled", "images.start_timeout_seconds",
                  "images.job_timeout_seconds", "images.max_upload_bytes", "images.max_pixels", "modules.images",
                  "modules.image_edit")
OWNER_ROUTES = ("/images", "/images/{iid}", "/images/{iid}/upscale", "/maintenance/image-archive/retention/apply")
APP_ROUTES = ("/api/v1/images", "/api/v1/images/{iid}", "/api/v1/images/{iid}/upscale")
SESSION = {"id": "s1", "kind": "agent", "project": "scratch", "owner_id": "owner", "app_id": ""}


def images_manager(tmp_path, *, packages=None, enabled=True) -> Manager:
    cfg = make_cfg(tmp_path)
    cfg.images = config.ImagesConfig(enabled=enabled, work_dir=str(tmp_path / "img"))
    cfg.module_packages = packages
    return Manager(cfg, chat=Script([Completion(content="hi")]))


def service_manager(tmp_path) -> Manager:
    """A real service-profile install: images is not selected, so its module is absent."""
    assert setup_config.main(["--config-dir", str(tmp_path / "cfg"), "--data-dir", str(tmp_path / "data"),
                              "--model", "gpt-oss", "--pause-flag", str(tmp_path / "paused"),
                              "--profile", "service"]) == 0
    return Manager(config.load(tmp_path / "cfg"))


def route_paths(app) -> set[str]:
    return {route.path for route in app.routes if isinstance(route, APIRoute)}


def tool_names(m: Manager) -> set[str]:
    return {name for kit in m.runner.daemon_toolkits(SESSION) for name in kit.tool_names}


def test_images_is_discovered_from_the_harness_modules_namespace():
    assert "harness_modules.images" in modules.namespace_packages()
    images = next(module for module in modules.discover() if module.name == "images")
    assert images.switches == ("images", "image_edit")
    assert modules.discover([]) == ()


def test_images_registers_everything_through_the_interface(tmp_path):
    m = images_manager(tmp_path)
    assert "images" in [module.name for module in modules.present(m.cfg)]
    app = create_app(m)
    assert set(OWNER_ROUTES) | set(APP_ROUTES) <= route_paths(app)
    # tool: offered to the owner's sessions, with the module's gate deciding how it is called
    assert "generate_image" in tool_names(m)
    gate, kit = m.modules.toolkits()[0]
    assert kit is m.images and gate.workspace and gate.span == "image_job"
    # settings: the registry keys images owns, with their defaults and enable checks
    registry = build_registry(m.cfg)
    assert set(IMAGE_SETTINGS) <= set(registry.specs)
    assert registry.get("images.job_timeout_seconds").default == 1200
    assert registry.get("images.enabled").enable_check is not None
    assert "images" in registry.get("app.capabilities").bounds.enum
    # capabilities and CLI
    caps = m.cfg.capabilities()["modules"]
    assert caps["images"] is True and caps["image_edit"] is False
    assert {row[0] for row in cli.admin_commands()} >= {"images list", "images create",
                                                       "maintenance image-retention-apply"}
    assert cli.admin_request(cli._build_parser().parse_args(["images", "show", "abc"]))[:2] == ("GET", "/images/abc")
    with TestClient(app) as client:
        root = client.get("/api/v1").json()
        assert root["features"]["images"] is True and "image_modes" in root
        assert "images" in root["scopes"]
        assert client.get("/me").json()["capabilities"]["images"] is True
        assert client.get("/images").status_code == 200
        operations = {(op["method"], op["path"]) for op in client.get(PREFIX).json()["operations"]}
        assert ("GET", PREFIX + "/images") in operations
        assert client.get(PREFIX + "/images").status_code == 200


def test_images_switched_off_keeps_its_settings_and_answers_disabled(tmp_path):
    """Present but off (images.enabled: false): the owner can turn it back on, so its keys stay."""
    cfg = make_cfg(tmp_path)
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    assert m.images is None and "images" in m.modules
    assert "images.enabled" in build_registry(cfg).specs
    assert cfg.capabilities()["modules"]["images"] is False
    assert "generate_image" not in tool_names(m)
    with TestClient(create_app(m)) as client:
        assert client.get("/images").status_code == 400


def _assert_absent(m: Manager) -> None:
    assert "images" not in m.modules and m.images is None
    registry = build_registry(m.cfg)
    assert not set(IMAGE_SETTINGS) & set(registry.specs)
    assert "images" not in registry.get("app.capabilities").bounds.enum
    caps = m.cfg.capabilities()["modules"]
    assert "images" not in caps and "image_edit" not in caps
    assert "generate_image" not in tool_names(m)
    app = create_app(m)
    assert not (set(OWNER_ROUTES) | set(APP_ROUTES)) & route_paths(app)
    with TestClient(app) as client:  # the daemon starts and runs without the module
        assert client.get("/health").json()["ok"] is True
        for path in ("/images", "/images/abc", "/api/v1/images/abc", PREFIX + "/images"):
            assert client.get(path).status_code == 404, path
        for path in ("/images", "/images/abc/upscale", "/api/v1/images", "/maintenance/image-archive/retention/apply"):
            assert client.post(path, json={}).status_code in (404, 405), path  # no route; the static site refuses it
        root = client.get("/api/v1").json()
        assert "images" not in root["features"] and "image_modes" not in root
        assert "images" not in root["capabilities"]["modules"]
        assert "images" not in client.get("/me").json()["capabilities"]
        operations = client.get(PREFIX).json()["operations"]
        assert not [op for op in operations if "image" in op["path"]]
        schema = {entry["key"] for entry in client.get(PREFIX + "/config/schema").json()["settings"]}
        assert not set(IMAGE_SETTINGS) & schema


def test_service_profile_runs_without_images(tmp_path, monkeypatch):
    monkeypatch.setattr("harness.backend_state._subscription_status", lambda *_: False)
    _assert_absent(service_manager(tmp_path))


def test_uninstalled_images_package_runs_without_images(tmp_path):
    m = images_manager(tmp_path, packages=())
    assert m.cfg.installed.images  # the full profile selects it; there is just no package to answer
    _assert_absent(m)


def test_an_absent_modules_saved_settings_stay_dormant(tmp_path):
    """Uninstalling images must not quarantine a managed overlay that still sets one of its keys."""
    m = images_manager(tmp_path)
    m.settings.patch_admin({"images.job_timeout_seconds": 900}, revision=None, actor={"kind": "owner"})
    m.settings.confirm_startup()
    cfg = make_cfg(tmp_path)
    cfg.module_packages = ()
    again = Manager(cfg, chat=Script([Completion(content="hi")]))
    assert again.settings.store.read_status().get("recovery") is None
    assert again.settings.store.read_active().values["images.job_timeout_seconds"] == 900


def test_module_packages_config_key(tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    model = "models:\n  m:\n    base_url: http://127.0.0.1:1\n"
    (cfg_dir / "harness.yaml").write_text(model + "module_packages: [harness_modules.images]\n", encoding="utf-8")
    assert config.load(cfg_dir, tmp_path / "data").module_packages == ("harness_modules.images",)
    (cfg_dir / "harness.yaml").write_text(model, encoding="utf-8")
    assert config.load(cfg_dir, tmp_path / "data").module_packages is None


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def test_core_never_imports_a_module_package():
    offenders = [f"{path.relative_to(ROOT)}: {name}" for path in sorted((ROOT / "harness").rglob("*.py"))
                 for name in _imports(path) if name.split(".")[0] == modules.NAMESPACE]
    assert not offenders, offenders
    for path in (ROOT / "harness").rglob("*.py"):  # nor by its old names
        text = path.read_text(encoding="utf-8")
        assert "import images" not in text and "image_edit import" not in text, path


def test_a_module_imports_only_the_core_interface_and_itself():
    packages = [p for p in sorted((ROOT / modules.NAMESPACE).iterdir()) if (p / "__init__.py").exists()]
    assert {p.name for p in packages} >= {"images", "notifications"}
    for package in packages:
        for path in package.rglob("*.py"):
            for name in _imports(path):
                top = name.split(".")[0]
                if top == "harness":
                    assert name == "harness.modules", f"{path.name} imports {name}"
                assert top != modules.NAMESPACE or name.startswith(f"{modules.NAMESPACE}.{package.name}"), (
                    path.name, name)
