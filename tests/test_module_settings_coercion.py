"""Module setters keep the coercion their hand-written version had (#839).

A converted setter casts the value to the type its getter returns, as the replaced function did. Two discovery setters
never cast: they stored the value they were given, so the consolidation must store it unchanged too.
"""

from __future__ import annotations

import pytest

from test_daemon import make_cfg
from test_module_settings_accessors import module_specs, section_of

# A value that differs from its cast: int(2.75) is 2 and bool(2.75) is True.
VALUE = 2.75
CASTS = {"bool": bool, "int": int, "float": float}
STORED_AS_GIVEN = ("remote_control.discovery.enabled", "remote_control.discovery.max_depth")


@pytest.mark.parametrize(
    "spec",
    [spec for spec in module_specs() if spec.writable and spec.value_type in CASTS and spec.key not in STORED_AS_GIVEN],
    ids=lambda spec: spec.key,
)
def test_setter_casts_the_value_to_its_settings_type(spec, tmp_path):
    cfg = make_cfg(tmp_path)
    spec.setter(cfg, VALUE)
    stored = getattr(section_of(cfg, spec), spec.yaml_path[-1])
    assert stored == CASTS[spec.value_type](VALUE)
    assert type(stored) is CASTS[spec.value_type]


@pytest.mark.parametrize("key", STORED_AS_GIVEN)
def test_discovery_setter_stores_the_value_it_is_given(key, tmp_path):
    cfg = make_cfg(tmp_path)
    spec = next(spec for spec in module_specs() if spec.key == key)
    spec.setter(cfg, VALUE)
    stored = getattr(section_of(cfg, spec), spec.yaml_path[-1])
    assert stored == VALUE and type(stored) is float
