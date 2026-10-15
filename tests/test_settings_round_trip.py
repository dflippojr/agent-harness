"""Every writable admin setting round-trips over HTTP (#708): PATCH a valid value, GET it back as `effective`.

This replaces the per-module "set the dataclass field, read it back" tests: it goes through the registry, the overlay
store and the live-apply hooks the way the owner's CLI does, for every key a present module registers. A key added
without a valid value here fails the test, so a new setting cannot skip the round trip.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.llm import Completion
from harness.manager import Manager
from harness.settings import looks_hidden

from test_daemon import Script, make_cfg

CONFIG = "/api/admin/v1/config"
# Folder discovery validates its roots against the real Windows filesystem; test_folder_discovery covers them.
FILESYSTEM_BOUND = {"remote_control.discovery.enabled", "remote_control.discovery.roots"}
# Values the bounds cannot name: patterned strings and lists, and thresholds that must keep their order.
VALUES = {
    "compaction.elide_at": 0.5,
    "compaction.reset_at": 0.62,
    "compaction.summarize_at": 0.7,
    "compaction.keep_recent": 0.1,
    "backup.at": "04:15",
    "smart_approvals.model": "gpt-4.1",
}


def valid_value(item: dict):
    """A value the key accepts that differs from the one it has now, derived from its bounds."""
    key, kind, current = item["key"], item["type"], item["effective"]
    if key in VALUES:
        return VALUES[key]
    if kind == "bool":
        return not current
    if kind == "enum":
        return next((choice for choice in item["enum"] if choice != current), current)
    if kind in ("int", "float"):
        low, high = item["minimum"], item["maximum"]
        pick = low if current != low else high
        return int(pick) if kind == "int" else float(pick)
    raise AssertionError(f"{key} ({kind}) needs a value in VALUES")


@pytest.fixture
def daemon(tmp_path):
    """Every module is installed; the three whose live hooks reach a running service are switched on."""
    cfg = make_cfg(tmp_path)
    cfg.runners = {}
    cfg.jobs.enabled = cfg.endpoint.enabled = True
    cfg.backup.enabled, cfg.backup.dir = True, str(tmp_path / "backups")
    cfg.installed.image_edit = True  # the opt-in module two images keys belong to
    manager = Manager(cfg, chat=Script([Completion(content="hi")]))
    # Enable checks probe the host (ComfyUI, model weights, folders); each module's tests cover them.
    registry = manager.settings.registry
    registry.validators[:] = [v for v in registry.validators if v.__name__ != "validate_enables"]
    with TestClient(create_app(manager)) as client:
        yield manager, client
    manager.db.close()


def test_every_writable_setting_round_trips_and_live_keys_apply(daemon):
    manager, client = daemon
    view = client.get(CONFIG).json()
    items = [item for item in view["settings"] if item["writable"] and item["sensitivity"] != "hidden"
             and item["key"] not in FILESYSTEM_BOUND and not looks_hidden(item["key"])]
    assert {"jobs.poll_seconds", "endpoint.max_waiting", "backup.at", "images.max_pixels"} <= {i["key"] for i in items}
    # Switches last: a restart-only switch changes what the live hooks above it can reach.
    items.sort(key=lambda item: item["key"].endswith(".enabled"))
    revision, backup_task = view["revision"], manager.backup._backup_task
    assert backup_task is not None
    sent = {}
    for item in items:
        value = sent[item["key"]] = valid_value(item)
        response = client.patch(CONFIG, json={"revision": revision, "changes": {item["key"]: value}})
        assert response.status_code == 200, (item["key"], value, response.text)
        revision = response.json()["revision"]
    after = client.get(CONFIG).json()["settings"]
    effective = {item["key"]: item["effective"] for item in after}
    pending = {item["key"]: item["pending"] for item in after}
    applies = {item["key"]: item["apply"] for item in after}
    assert {key: effective[key] for key in sent if applies[key] == "live"} == {
        key: value for key, value in sent.items() if applies[key] == "live"}
    # A restart-only switch keeps running as it was and reports the new value as pending.
    assert {key: pending[key] for key in sent if applies[key] != "live"} == {
        key: value for key, value in sent.items() if applies[key] != "live"}
    # Live keys reach the running services without a restart.
    assert manager.jobs.poll_seconds == sent["jobs.poll_seconds"]
    assert (manager.runner.gate.max_waiting, manager.runner.gate.fair_seconds) == (
        sent["endpoint.max_waiting"], sent["endpoint.agent_fair_seconds"])
    assert manager.backup._backup_task is not backup_task

