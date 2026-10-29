"""Issue #840: a module package's __init__ stays light, and its lazy hooks resolve to the same code.

The CLI imports every package under harness_modules to list commands, so importing one must not import its
runtime, routes, settings or doctor (or any other submodule). Each hook on MODULE imports its target only when
called; HOOKS records what each one reaches, so changing how the hooks are written can't change where they lead.
A new module adds its rows here."""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness import modules

ROOT = Path(__file__).resolve().parents[1]

# hook -> the arguments the core calls it with (the runtime hook also gets the Module)
CALLS = {"runtime": 2, "owner_routes": 0, "app_routes": 0, "public_routes": 0, "settings": 0, "doctor": 2}

# module -> hook -> what the hook reaches: ("calls", submodule, qualname) or ("returns", submodule, attribute)
HOOKS = {
    "backup": {
        "runtime": ("calls", "runtime", "BackupRuntime"),
        "owner_routes": ("returns", "routes", "owner_routes"),
        "settings": ("calls", "settings", "specs"),
        "doctor": ("calls", "runtime", "doctor"),
    },
    "endpoint": {
        "runtime": ("calls", "runtime", "EndpointRuntime"),
        "public_routes": ("returns", "routes", "public_routes"),
        "settings": ("calls", "settings", "specs"),
    },
    "homelab": {
        "runtime": ("calls", "runtime", "HomelabRuntime"),
    },
    "hub": {
        "runtime": ("calls", "runtime", "HubRuntime"),
        "settings": ("calls", "settings", "specs"),
        "doctor": ("calls", "entries", "load"),
    },
    "images": {
        "runtime": ("calls", "runtime", "ImagesRuntime"),
        "owner_routes": ("returns", "routes", "owner_routes"),
        "app_routes": ("returns", "routes", "app_routes"),
        "settings": ("calls", "settings", "specs"),
        "doctor": ("calls", "doctor", "run"),
    },
    "jobs": {
        "runtime": ("calls", "runtime", "JobsRuntime"),
        "owner_routes": ("returns", "routes", "owner_routes"),
        "settings": ("calls", "settings", "specs"),
    },
    "local_model": {
        "runtime": ("calls", "runtime", "LocalModelRuntime"),
        "owner_routes": ("returns", "routes", "owner_routes"),
        "app_routes": ("returns", "routes", "app_routes"),
        "settings": ("calls", "settings", "specs"),
        "doctor": ("calls", "doctor", "run"),
    },
    "mcp_client": {
        "runtime": ("calls", "runtime", "McpRuntime"),
    },
    "memory_library": {
        "runtime": ("calls", "runtime", "MemoryLibraryRuntime"),
        "owner_routes": ("returns", "routes", "owner_routes"),
        "settings": ("calls", "settings", "specs"),
    },
    "notifications": {
        "runtime": ("calls", "runtime", "NotificationsRuntime"),
        "owner_routes": ("returns", "routes", "owner_routes"),
        "settings": ("calls", "settings", "specs"),
    },
    "remote_control": {
        "runtime": ("calls", "runtime", "RemoteControlRuntime"),
        "owner_routes": ("returns", "routes", "owner_routes"),
        "app_routes": ("returns", "routes", "app_routes"),
        "settings": ("calls", "settings", "specs"),
    },
    "runners": {
        "runtime": ("calls", "runtime", "RunnersRuntime"),
        "owner_routes": ("returns", "routes", "owner_routes"),
        "public_routes": ("returns", "routes", "public_routes"),
    },
    "search": {
        "runtime": ("calls", "runtime", "SearchRuntime"),
        "owner_routes": ("returns", "routes", "owner_routes"),
        "app_routes": ("returns", "routes", "app_routes"),
        "settings": ("calls", "settings", "specs"),
    },
    "skills": {
        "runtime": ("calls", "runtime", "SkillsRuntime"),
        "owner_routes": ("returns", "routes", "owner_routes"),
        "settings": ("calls", "settings", "specs"),
    },
}

# Imports each package in a fresh interpreter, purging the namespace between them, and prints the submodules each
# one pulled in.
CLEAN_IMPORT = """
import importlib, json, sys
seen = {}
for package in sys.argv[1:]:
    for name in [name for name in sys.modules if name.startswith("harness_modules.")]:
        del sys.modules[name]
    importlib.import_module(package)
    seen[package] = sorted(name for name in sys.modules if name.startswith(package + "."))
print(json.dumps(seen))
"""


def test_every_discovered_module_has_hook_rows():
    assert sorted(module.name for module in modules.discover()) == sorted(HOOKS)


def test_importing_a_module_package_imports_none_of_its_submodules():
    packages = modules.namespace_packages()
    assert packages
    out = subprocess.run([sys.executable, "-c", CLEAN_IMPORT, *packages], cwd=ROOT, capture_output=True, text=True,
                         check=True, timeout=120).stdout
    assert json.loads(out) == {package: [] for package in packages}


def submodules(package: str) -> list:
    path = importlib.import_module(package).__path__
    return [importlib.import_module(f"{package}.{info.name}") for info in pkgutil.iter_modules(path)]


@pytest.mark.parametrize("module", modules.discover(), ids=lambda module: module.name)
def test_each_hook_reaches_the_same_code(module, monkeypatch):
    package = f"{modules.NAMESPACE}.{module.name}"
    subs = {sub.__name__.removeprefix(package + "."): sub for sub in submodules(package)}
    calls: list[tuple] = []
    # Every function and class a submodule defines records its call instead of running.
    for short, sub in subs.items():
        for name, value in list(vars(sub).items()):
            if (inspect.isfunction(value) or inspect.isclass(value)) and value.__module__ == sub.__name__:
                def record(*args, _where=("calls", short, value.__qualname__), **kwargs):
                    calls.append(_where)
                monkeypatch.setattr(sub, name, record)
    reached = {}
    for hook, arity in CALLS.items():
        fn = getattr(module, hook)
        if fn is None:
            continue
        calls.clear()
        result = fn(*((MagicMock(), module) if hook == "runtime" else [MagicMock()] * arity))
        reached[hook] = calls[0] if calls else next(
            ("returns", short, name) for short, sub in subs.items() for name, value in vars(sub).items()
            if value is result)
    assert reached == HOOKS[module.name]


def test_enabled_flag_reads_its_section_for_every_switch():
    hook = modules.enabled_flag("notify")
    assert hook(SimpleNamespace(notify=SimpleNamespace(enabled=1)), "notifications") is True
    assert hook(SimpleNamespace(notify=SimpleNamespace(enabled=0)), "other") is False


def test_lazy_resolves_relative_to_its_package_on_every_call(monkeypatch):
    from harness_modules.search import MODULE, routes
    assert MODULE.owner_routes() is routes.owner_routes
    replaced = object()
    monkeypatch.setattr(routes, "owner_routes", replaced)
    assert MODULE.owner_routes() is replaced
