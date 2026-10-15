"""Issue #334 stage (b): optional modules plug in through harness/modules.py, and images is the first of them.

Images registers its routes, agent tool, settings, capabilities and CLI rows through the interface, and the core
never imports an add-on package. What an absent, uninstalled or switched-off module leaves behind is the lifecycle
table in test_module_lifecycle.py."""

from __future__ import annotations

import ast
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from harness import cli, config, modules
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
