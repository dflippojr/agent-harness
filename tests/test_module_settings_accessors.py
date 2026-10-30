"""Each module setting's getter and setter read and write the Config field its key names (#839).

A module registers a getter and a setter per SettingSpec, and they are the only link between the registry and the
field the setting stores. For every SettingSpec from every module's specs(), this drives them directly: the field's
current value reads through the getter, a representative value writes through the setter, and the stored field keeps
the type its value type casts to. A key the owner cannot write refuses a write. The HTTP path is covered by
test_settings_round_trip.py.
"""

from __future__ import annotations

import pytest

from harness import modules

from test_daemon import make_cfg

# Values the bounds cannot name: a time of day, and a root the setter accepts only as a Windows drive path.
VALUES = {
    "backup.at": "04:15",
    "remote_control.discovery.roots": ["C:\\Games"],
}
TYPES = {"bool": bool, "int": int, "float": float, "string": str, "discovery_root_list": list}


def module_specs() -> list:
    return [spec for module in modules.discover() if module.settings for spec in module.settings()]


def section_of(cfg, spec):
    owner = cfg
    for name in spec.yaml_path[:-1]:
        owner = getattr(owner, name)
    return owner


def representative(spec, current):
    """A value inside the key's bounds. An int for a float key, so the setter's cast is exercised."""
    if spec.key in VALUES:
        return VALUES[spec.key]
    if spec.value_type == "bool":
        return not current
    if spec.value_type in ("int", "float"):
        return (spec.bounds.minimum + spec.bounds.maximum) // 2
    raise AssertionError(f"{spec.key} ({spec.value_type}) needs a value in VALUES")


def test_the_specs_under_test_include_every_kind_of_accessor():
    keys = {spec.key for spec in module_specs()}
    assert {"jobs.poll_seconds", "images.max_pixels", "backup.at", "remote_control.discovery.roots",
            "notify.topic"} <= keys


@pytest.mark.parametrize("spec", module_specs(), ids=lambda spec: spec.key)
def test_accessor_reads_and_writes_the_field_its_key_names(spec, tmp_path):
    cfg = make_cfg(tmp_path)
    if not spec.writable:
        assert spec.getter(cfg) is None
        with pytest.raises(ValueError):
            spec.setter(cfg, None)
        return
    owner, name = section_of(cfg, spec), spec.yaml_path[-1]
    current = getattr(owner, name)
    assert spec.getter(cfg) == current
    value = representative(spec, current)
    spec.setter(cfg, value)
    stored = getattr(owner, name)
    assert stored == value and type(stored) is TYPES[spec.value_type]
    assert spec.getter(cfg) == value
