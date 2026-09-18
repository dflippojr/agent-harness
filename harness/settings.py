"""Typed, allowlisted configuration registry (issue #66).

Every exposed setting has an explicit SettingSpec. Setters are named functions, never
reflection, dotted traversal, dataclass deserialization, or YAML merge. Unknown keys
are rejected. Installer-only entries may advertise safe metadata without a value.
"""

from __future__ import annotations

import copy
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Config

SCHEMA_VERSION = 1
RESET = object()
SCOPES = ("app", "admin")
APPLY_MODES = ("live", "daemon_restart", "installer_only")
VALUE_TYPES = ("bool", "int", "float", "string", "enum", "string_list")
SENSITIVITIES = ("public", "redact", "hidden")
APP_CAPABILITIES = ("web", "images", "search", "memory_library", "remote_control", "homelab")


def app_allows(defaults: dict, capability: str) -> bool:
    """None (unset) inherits every granted capability; a list is an explicit subset."""
    enabled = defaults.get("app.capabilities")
    return enabled is None or capability in enabled


NOTIFY_COMPLETION = ("inherit", "never")
EFFORTS = ("low", "medium", "high")
SECRET_KEY_MARKERS = ("secret", "token", "password", "credential", "api_key", "auth")
PATH_KEY_MARKERS = (".dir", ".path", "_dir", "_path", "pause_flag", "token_file", "api_key_file")

ConfigError = dict[str, Any]
Getter = Callable[[Config], Any]
Setter = Callable[[Config, Any], None]
LiveHook = Callable[[Any, Any, Any], None]  # (manager, old, new)
Validator = Callable[[Config, dict[str, Any]], list[ConfigError]]


@dataclass(frozen=True)
class Bounds:
    minimum: int | float | None = None
    maximum: int | float | None = None
    min_length: int | None = None
    max_length: int | None = None
    enum: tuple[str, ...] | None = None
    pattern: str | None = None


@dataclass
class SettingSpec:
    key: str
    label: str
    help: str
    category: str
    value_type: str
    default: Any
    scope: str
    apply_mode: str
    getter: Getter
    setter: Setter
    bounds: Bounds = field(default_factory=Bounds)
    sensitivity: str = "public"
    readable: bool = True
    writable: bool = True
    modules: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ("full", "service")
    platforms: tuple[str, ...] = ("win32", "linux", "darwin")
    docs: str = "/docs/config-registry.md"
    yaml_path: tuple[str, ...] = ()
    live_apply: LiveHook | None = None
    live_undo: LiveHook | None = None
    enable_check: Callable[[Config], list[str]] | None = None


@dataclass
class Registry:
    specs: dict[str, SettingSpec]
    validators: list[Validator] = field(default_factory=list)

    def get(self, key: str) -> SettingSpec:
        spec = self.specs.get(key)
        if spec is None:
            raise KeyError(key)
        return spec

    def admin(self) -> list[SettingSpec]:
        return [spec for spec in self.specs.values() if spec.scope == "admin"]

    def app(self) -> list[SettingSpec]:
        return [spec for spec in self.specs.values() if spec.scope == "app"]

    def writable_admin(self) -> list[SettingSpec]:
        return [spec for spec in self.admin() if spec.writable and spec.apply_mode != "installer_only"]


def unknown_key_error(key: str) -> dict[str, dict]:
    return {key: {"code": "unknown_key", "message": f"unknown setting {key!r}"}}


def looks_hidden(key: str, spec: SettingSpec | None = None) -> bool:
    if spec is not None and spec.sensitivity in ("redact", "hidden"):
        return True
    lowered = key.lower()
    return any(marker in lowered for marker in SECRET_KEY_MARKERS + PATH_KEY_MARKERS)


def redact_value(key: str, spec: SettingSpec | None, value: Any) -> Any:
    if value is RESET or value is None:
        return None
    if spec is not None and spec.apply_mode == "installer_only":
        return None
    if looks_hidden(key, spec):
        return None
    return copy.deepcopy(value)


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError("expected a boolean")


def parse_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected an integer")
    return value


def parse_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a number")
    return float(value)


def parse_string(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a string")
    return value


def parse_string_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("expected a list of strings")
    return list(value)


def parse_value(spec: SettingSpec, value: Any) -> Any:
    if value is RESET or value is None:
        return RESET
    parsers = {
        "bool": parse_bool,
        "int": parse_int,
        "float": parse_float,
        "string": parse_string,
        "enum": parse_string,
        "string_list": parse_string_list,
    }
    parsed = parsers[spec.value_type](value)
    bounds = spec.bounds
    if spec.value_type in ("int", "float"):
        if bounds.minimum is not None and parsed < bounds.minimum:
            raise ValueError(f"must be >= {bounds.minimum}")
        if bounds.maximum is not None and parsed > bounds.maximum:
            raise ValueError(f"must be <= {bounds.maximum}")
    if spec.value_type in ("string", "enum"):
        if bounds.min_length is not None and len(parsed) < bounds.min_length:
            raise ValueError(f"must be at least {bounds.min_length} characters")
        if bounds.max_length is not None and len(parsed) > bounds.max_length:
            raise ValueError(f"must be at most {bounds.max_length} characters")
        if bounds.enum is not None and parsed not in bounds.enum:
            raise ValueError(f"must be one of {', '.join(bounds.enum)}")
        if bounds.pattern is not None:
            import re
            if not re.fullmatch(bounds.pattern, parsed):
                raise ValueError(f"must match {bounds.pattern}")
    if spec.value_type == "string_list":
        if bounds.enum is not None:
            unknown = [item for item in parsed if item not in bounds.enum]
            if unknown:
                raise ValueError(f"unknown values {unknown}; known: {', '.join(bounds.enum)}")
        if spec.key == "app.capabilities":
            if len(parsed) != len(set(parsed)):
                raise ValueError("capabilities must not repeat")
    return parsed


def module_installed(cfg: Config, name: str) -> bool:
    installed = getattr(cfg, "installed", None)
    if installed is not None:
        return bool(getattr(installed, name, False))
    return bool(getattr(cfg.modules, name, False))


def spec_available(cfg: Config, spec: SettingSpec) -> bool:
    if cfg.profile not in spec.profiles:
        return False
    platform = "win32" if sys.platform == "win32" else ("darwin" if sys.platform == "darwin" else "linux")
    if platform not in spec.platforms:
        return False
    return all(module_installed(cfg, name) for name in spec.modules)


def schema_entry(spec: SettingSpec, cfg: Config | None = None) -> dict[str, Any]:
    available = True if cfg is None else spec_available(cfg, spec)
    bounds = spec.bounds
    return {
        "key": spec.key,
        "label": spec.label,
        "help": spec.help,
        "category": spec.category,
        "type": spec.value_type,
        "default": None if spec.apply_mode == "installer_only" else copy.deepcopy(spec.default),
        "scope": spec.scope,
        "apply": spec.apply_mode,
        "sensitivity": spec.sensitivity,
        "readable": spec.readable and spec.apply_mode != "installer_only",
        "writable": bool(spec.writable and spec.apply_mode != "installer_only" and available),
        "modules": list(spec.modules),
        "capabilities": list(spec.capabilities),
        "profiles": list(spec.profiles),
        "platforms": list(spec.platforms),
        "docs": spec.docs,
        "minimum": bounds.minimum,
        "maximum": bounds.maximum,
        "enum": list(bounds.enum) if bounds.enum else None,
        "available": available,
        "file_only": spec.apply_mode == "installer_only",
        "guidance": ("managed in local configuration" if spec.apply_mode == "installer_only" else ""),
    }


def yaml_has(raw: dict, path: tuple[str, ...]) -> bool:
    cur: Any = raw
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def yaml_get(raw: dict, path: tuple[str, ...]) -> Any:
    cur: Any = raw
    for part in path:
        cur = cur[part]
    return cur


def inherited_source(spec: SettingSpec, files: dict[str, dict]) -> str:
    """Provenance of the YAML-inherited value. profile.yaml is not a generic overlay."""
    path = spec.yaml_path
    if not path:
        return "default"
    local = files.get("local") or {}
    base = files.get("base") or {}
    profile = files.get("profile") or {}
    if spec.key.startswith("backends.") and spec.key != "backends.local.model":
        name = spec.key.split(".")[1]
        if yaml_has(local, ("backends", name, path[-1])):
            return "local"
        if yaml_has(base, ("backends", name, path[-1])):
            return "file"
        if yaml_has(profile, ("backends", name, path[-1])) and not yaml_has(base, ("backends", name)):
            return "profile"
        return "default"
    if yaml_has(local, path):
        return "local"
    if yaml_has(base, path):
        return "file"
    if path[0] in ("profile", "modules") and yaml_has(profile, path):
        return "profile"
    return "default"


def supervised_restart_supported() -> bool:
    return os.environ.get("HARNESS_SUPERVISED", "").strip() in ("1", "true", "yes")


def copy_cfg(cfg: Config) -> Config:
    return copy.deepcopy(cfg)


def apply_spec(cfg: Config, spec: SettingSpec, value: Any) -> None:
    spec.setter(cfg, value)
