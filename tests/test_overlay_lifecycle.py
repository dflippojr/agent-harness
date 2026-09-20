"""Model-based overlay lifecycle tests (issue #66 / PR #103).

An in-memory reference model of (active, pending, lkg, boot_tried) is driven
with the same operations as SettingsService. After every step the store files
must match the model, boot applies active only, a live PATCH cannot clobber a
pending restart key, rollback clears pending, restart_required follows
active-vs-applied daemon_restart values, shrinking the YAML baseline between
boots never prevents startup, and an unconfirmed boot that already tried the
candidate restores LKG.
"""

from __future__ import annotations

import copy
import random
from typing import Any

from harness.managed_config import OverlayCrash
from harness.overlay import OverlayRequest, OverlayState, effective_candidate, next_overlay
from harness.settings import RESET
from harness.settings_service import SettingsError, SettingsService
from harness.config import ModelConfig

from test_daemon import make_cfg

LIVE_KEYS = ("sessions.max_turns", "backup.keep_days", "backends.local.model")
RESTART_KEYS = ("web.enabled", "search.enabled")
ALL_KEYS = LIVE_KEYS + RESTART_KEYS
MODES = {key: "live" for key in LIVE_KEYS}
MODES.update({key: "daemon_restart" for key in RESTART_KEYS})
INHERITED = {
    "sessions.max_turns": 80,
    "backup.keep_days": 14,
    "backends.local.model": "fake",
    "web.enabled": False,
    "search.enabled": False,
}
GETTERS = {
    "sessions.max_turns": lambda cfg: cfg.max_turns,
    "backup.keep_days": lambda cfg: cfg.backup.keep_days,
    "backends.local.model": lambda cfg: cfg.default_model,
    "web.enabled": lambda cfg: cfg.web.enabled,
    "search.enabled": lambda cfg: cfg.search.enabled,
}
LIVE_VALUES = {
    "sessions.max_turns": (10, 20, 40, 50, 60, 70),
    "backup.keep_days": (3, 7, 14, 21, 30),
    "backends.local.model": ("fake", "qwen-a"),
}
BASE_MODELS = ("fake", "qwen-a")
N_SEQUENCES = 200
STEPS = 12
SEED = 66
UNSET = object()


def _mode(key: str) -> str:
    return MODES.get(key, "live")


def _snap(env) -> dict | None:
    if env is None:
        return None
    return {
        "revision": env.revision,
        "confirmed": bool(env.confirmed and not env.unconfirmed),
        "values": dict(env.values),
    }


def _restart_map(values: dict | None) -> dict:
    if not values:
        return {}
    return {key: values[key] for key in values if _mode(key) == "daemon_restart"}


def _distinct_pending_restart(model: OverlayModel) -> dict | None:
    """Restart keys pending would apply that active does not already hold."""
    if model.pending is None:
        return None
    pending_r = _restart_map(model.pending["values"])
    active_r = _restart_map(model.active["values"] if model.active else None)
    if pending_r != active_r:
        return pending_r
    return None


class OverlayModel:
    """Independent spec of the overlay lifecycle. Does not call next_overlay."""

    def __init__(self, models=BASE_MODELS):
        self.active: dict | None = None
        self.pending: dict | None = None
        self.lkg: dict | None = None
        self.boot_tried = False
        self.models = set(models)

    def clone(self) -> "OverlayModel":
        other = OverlayModel(models=self.models)
        other.active = copy.deepcopy(self.active)
        other.pending = copy.deepcopy(self.pending)
        other.lkg = copy.deepcopy(self.lkg)
        other.boot_tried = self.boot_tried
        return other

    def snapshot(self) -> dict:
        return {
            "active": copy.deepcopy(self.active),
            "pending": copy.deepcopy(self.pending),
            "lkg": copy.deepcopy(self.lkg),
            "boot_tried": self.boot_tried,
            "models": set(self.models),
        }

    def restore(self, snap: dict) -> None:
        self.active = copy.deepcopy(snap["active"])
        self.pending = copy.deepcopy(snap["pending"])
        self.lkg = copy.deepcopy(snap["lkg"])
        self.boot_tried = snap["boot_tried"]
        self.models = set(snap["models"])

    def _confirmed(self) -> bool:
        return True if self.active is None else bool(self.active["confirmed"])

    def effective(self) -> dict:
        values = dict(self.active["values"] if self.active else {})
        if self.pending is not None:
            values.update(self.pending["values"])
        return values

    def _split(self, values: dict) -> tuple[dict, dict]:
        live, restart = {}, {}
        for key, value in values.items():
            if _mode(key) == "daemon_restart":
                restart[key] = value
            else:
                live[key] = value
        return live, restart

    def patch(self, changes: dict[str, Any]) -> str:
        candidate = dict(self.effective())
        for key, value in changes.items():
            if value is RESET:
                candidate.pop(key, None)
            else:
                candidate[key] = value
        live, restart_cand = self._split(candidate)
        _live, promoted = self._split(self.active["values"] if self.active else {})
        new_active_values = {**live, **promoted}
        _live, restart_active = self._split(new_active_values)
        stay_confirmed = self._confirmed()
        next_rev = (self.active["revision"] if self.active else 0) + 1
        if self._confirmed():
            self.lkg = {
                "revision": self.active["revision"] if self.active else 0,
                "confirmed": True,
                "values": dict(self.active["values"]) if self.active else {},
            }
        self.active = {
            "revision": next_rev,
            "confirmed": stay_confirmed,
            "values": new_active_values,
        }
        if restart_cand != restart_active:
            self.pending = {"revision": next_rev, "confirmed": False, "values": dict(candidate)}
        else:
            self.pending = None
        return "ok"

    def rollback(self) -> str:
        if self.lkg is None:
            return "nothing_to_rollback"
        next_rev = (self.active["revision"] if self.active else 0) + 1
        new_lkg = self.lkg
        if self._confirmed():
            new_lkg = {
                "revision": self.active["revision"] if self.active else 0,
                "confirmed": True,
                "values": dict(self.active["values"]) if self.active else {},
            }
        self.active = {
            "revision": next_rev,
            "confirmed": True,
            "values": dict(self.lkg["values"]),
        }
        self.lkg = new_lkg
        self.pending = None
        return "ok"

    def confirm_restart(self) -> str:
        if self.pending is None:
            return "nothing_to_promote" if self.active is None else "restart_current"
        if self.active is not None and self._confirmed():
            self.lkg = {
                "revision": self.active["revision"],
                "confirmed": True,
                "values": dict(self.active["values"]),
            }
        self.active = {
            "revision": self.pending["revision"],
            "confirmed": False,
            "values": dict(self.pending["values"]),
        }
        self.boot_tried = False
        return "ok"

    def confirm_startup(self) -> str:
        if self.active is None or self._confirmed():
            return "noop"
        self.active = {**self.active, "confirmed": True}
        self.pending = None
        self.boot_tried = False
        return "ok"

    def _applies(self, values: dict | None) -> bool:
        if not values:
            return True
        model = values.get("backends.local.model")
        if model is not None and model not in self.models:
            return False
        return True

    def _adopt_lkg_or_discard(self) -> str:
        if self.lkg is not None and self._applies(self.lkg["values"]):
            self.active = {
                "revision": self.lkg["revision"],
                "confirmed": True,
                "values": dict(self.lkg["values"]),
            }
            self.pending = None
            self.boot_tried = False
            return "lkg_restore"
        self.active = None
        self.pending = None
        self.lkg = None
        self.boot_tried = False
        return "overlay_quarantined"

    def reboot(self) -> str:
        if self.active is None:
            return "empty"
        if not self.active["confirmed"] and self.boot_tried:
            return self._adopt_lkg_or_discard()
        if not self._applies(self.active["values"]):
            return self._adopt_lkg_or_discard()
        if self.active["confirmed"]:
            if (self.pending is not None
                    and self.pending["revision"] == self.active["revision"]
                    and self.pending["values"] == self.active["values"]):
                self.pending = None
            self.boot_tried = False
            return "confirmed"
        self.boot_tried = True
        return "try_candidate"


def _commit_plan(old: dict, new: dict, *, boot_tried=UNSET,
                 active_first: bool = False) -> list[tuple[str, str, Any]]:
    """Match ManagedStore.commit order, including active-first startup confirmation."""
    steps: list[tuple[str, str, Any]] = []
    if new["lkg"] is not None and new["lkg"] != old["lkg"]:
        steps.append(("lkg", "lkg", new["lkg"]))
    active_step = None
    if new["active"] is None and old["active"] is not None:
        active_step = ("active", "active", None)
    elif new["active"] is not None and new["active"] != old["active"]:
        active_step = ("active", "active", new["active"])
    if active_first and active_step is not None:
        steps.append(active_step)
    if new["pending"] is None and old["pending"] is not None:
        steps.append(("pending", "pending", None))
    elif new["pending"] is not None and new["pending"] != old["pending"]:
        steps.append(("pending", "pending", new["pending"]))
    if boot_tried is not UNSET:
        steps.append(("boot_tried", "boot_tried", boot_tried))
    if not active_first and active_step is not None:
        steps.append(active_step)
    return steps


def _apply_partial(model: OverlayModel, old: dict, steps: list[tuple[str, str, Any]], crash_at: str) -> None:
    model.restore(old)
    for name, field, value in steps:
        if field == "lkg":
            model.lkg = copy.deepcopy(value)
        elif field == "pending":
            model.pending = copy.deepcopy(value)
        elif field == "boot_tried":
            model.boot_tried = bool(value)
        elif field == "active":
            model.active = copy.deepcopy(value)
        if name == crash_at:
            return


def _make_cfg(tmp_path, models=BASE_MODELS):
    cfg = make_cfg(tmp_path)
    tokens = cfg.models["fake"].context_tokens if "fake" in cfg.models else 65536
    cfg.models = {name: ModelConfig(name=name, base_url="http://unused", context_tokens=tokens) for name in models}
    cfg.default_model = "fake" if "fake" in cfg.models else next(iter(cfg.models))
    return cfg


def _fresh(tmp_path, models=BASE_MODELS) -> SettingsService:
    return SettingsService(_make_cfg(tmp_path, models))


def _reboot(tmp_path, models=BASE_MODELS) -> SettingsService:
    svc = _fresh(tmp_path, models)
    svc.apply_overlay()
    return svc


def _assert_files(svc: SettingsService, model: OverlayModel, history: list[str]) -> None:
    assert _snap(svc.store.read_active()) == model.active, history
    assert _snap(svc.store.read_pending()) == model.pending, history
    assert _snap(svc.store.read_lkg()) == model.lkg, history
    assert svc.store.boot_tried() is model.boot_tried, "\n".join(history)


def _assert_boot_applies_active(tmp_path, svc: SettingsService, model: OverlayModel, history: list[str]) -> None:
    active = svc.store.read_active()
    pending = svc.store.read_pending()
    # Do not apply onto the live store when the candidate is unconfirmed: that would
    # mark boot-tried or restore LKG as a side effect of the assertion.
    if active is not None and not (active.confirmed and not active.unconfirmed):
        return
    probe_svc = _fresh(tmp_path, tuple(sorted(model.models)))
    probe_svc.apply_overlay()
    if active is not None:
        assert model.reboot() == "confirmed"
    if pending is None:
        return
    probe = probe_svc.cfg
    for key in ALL_KEYS:
        expected = active.values[key] if active is not None and key in active.values else INHERITED[key]
        assert GETTERS[key](probe) == expected, (history, key, GETTERS[key](probe), expected)
        if key in pending.values and pending.values[key] != expected:
            assert GETTERS[key](probe) != pending.values[key], (history, key)


def _expected_restart_required(svc: SettingsService, model: OverlayModel) -> bool:
    if model.pending is not None:
        return True
    active = model.active
    for key in RESTART_KEYS:
        disk = active["values"][key] if active is not None and key in active["values"] else INHERITED[key]
        if GETTERS[key](svc.cfg) != disk:
            return True
    return False


def _assert_invariants(tmp_path, svc: SettingsService, model: OverlayModel, last_op: str,
                       last_ok: bool, pending_restart_before: dict | None, history: list[str],
                       restored: bool, boot_result: str | None = None) -> None:
    _assert_files(svc, model, history)
    _assert_boot_applies_active(tmp_path, svc, model, history)
    assert svc.admin_view()["restart_required"] is _expected_restart_required(svc, model), history
    if last_op in ("reboot", "crash_reboot", "shrink_yaml"):
        active = svc.store.read_active()
        for key in ALL_KEYS:
            expected = active.values[key] if active is not None and key in active.values else INHERITED[key]
            assert GETTERS[key](svc.cfg) == expected, (history, key, GETTERS[key](svc.cfg), expected)
    if last_op == "patch_live" and last_ok and pending_restart_before:
        current = svc.store.read_pending()
        assert current is not None, ("live PATCH dropped a distinct pending candidate", history)
        assert _restart_map(current.values) == pending_restart_before, history
    if last_op == "rollback" and last_ok:
        assert svc.store.read_pending() is None, history
        assert model.pending is None, history
    if restored:
        lkg = svc.store.read_lkg()
        active = svc.store.read_active()
        if lkg is None:
            assert active is None, history
        else:
            assert active is not None and active.confirmed and not active.unconfirmed, history
            assert active.values == lkg.values, history
        assert svc.store.read_status().get("recovery") == "lkg_restore", history
    if boot_result == "overlay_quarantined":
        assert svc.store.read_active() is None, history
        assert svc.store.read_lkg() is None, history
        assert svc.store.read_pending() is None, history
        assert svc.store.read_status().get("recovery") == "overlay_quarantined", history
        assert svc.admin_view().get("warning"), history


def _pick_live_patch(rng: random.Random, models) -> dict[str, Any]:
    key = rng.choice(LIVE_KEYS)
    if key == "backends.local.model":
        choices = tuple(models) or ("fake",)
        return {key: rng.choice(choices)}
    return {key: rng.choice(LIVE_VALUES[key])}


def _pick_restart_patch(rng: random.Random) -> dict[str, Any]:
    key = rng.choice(RESTART_KEYS)
    return {key: rng.choice((True, False))}


def _pick_reset(rng: random.Random) -> dict[str, Any]:
    return {rng.choice(ALL_KEYS): RESET}


def _run_prod(svc: SettingsService, kind: str, payload: dict | None) -> str:
    try:
        if kind == "patch_live" or kind == "patch_restart" or kind == "patch_reset":
            changes = {key: (None if value is RESET else value) for key, value in payload.items()}
            svc.patch_admin(changes, svc.admin_view()["revision"])
            return "ok"
        if kind == "rollback":
            svc.rollback(svc.admin_view()["revision"])
            return "ok"
        if kind == "confirm_restart":
            svc.request_restart(None)
            return "ok"
        if kind == "confirm_startup":
            svc.confirm_startup()
            return "ok"
    except SettingsError as e:
        return e.code
    raise AssertionError(f"unhandled op {kind}")


def _run_model(model: OverlayModel, kind: str, payload: dict | None) -> str:
    if kind in ("patch_live", "patch_restart", "patch_reset"):
        return model.patch(payload)
    if kind == "rollback":
        return model.rollback()
    if kind == "confirm_restart":
        return model.confirm_restart()
    if kind == "confirm_startup":
        return model.confirm_startup()
    raise AssertionError(kind)


def test_next_overlay_live_patch_keeps_pending_restart_candidate():
    from harness.managed_config import Envelope
    active = Envelope(revision=1, confirmed=True, values={"web.enabled": False})
    pending = Envelope(revision=2, confirmed=False, unconfirmed=True, values={"web.enabled": True})
    new = next_overlay(
        OverlayState(active=active, pending=pending, lkg=active),
        OverlayRequest(action="patch", changes={"sessions.max_turns": 40}, next_revision=3),
        _mode,
    )
    assert new.active.values.get("web.enabled") is False
    assert new.pending is not None
    assert new.pending.values.get("web.enabled") is True
    assert new.pending.values.get("sessions.max_turns") == 40
    assert effective_candidate(new.active, new.pending)["web.enabled"] is True


def test_next_overlay_rollback_clears_pending_only_restart_key():
    from harness.managed_config import Envelope
    pending = Envelope(revision=1, confirmed=False, unconfirmed=True, values={"web.enabled": True})
    lkg = Envelope(revision=0, confirmed=True, values={})
    active = Envelope(revision=1, confirmed=True, values={})
    new = next_overlay(
        OverlayState(active=active, pending=pending, lkg=lkg),
        OverlayRequest(action="rollback", next_revision=2),
        _mode,
    )
    assert new.pending is None
    assert new.active is not None and "web.enabled" not in new.active.values


def _payload_for(kind: str, rng: random.Random, models=BASE_MODELS) -> dict | None:
    if kind == "patch_live":
        return _pick_live_patch(rng, models)
    if kind == "patch_restart":
        return _pick_restart_patch(rng)
    if kind == "patch_reset":
        return _pick_reset(rng)
    return None


def _apply_success(svc, model, kind, payload, history, pending_before, seq_dir) -> None:
    expected = _run_model(model.clone(), kind, payload)
    got = _run_prod(svc, kind, payload)
    if expected == "nothing_to_rollback":
        assert got == "nothing_to_rollback", history
        history.append(f"{kind}:nothing_to_rollback")
        _assert_invariants(seq_dir, svc, model, kind, False, pending_before, history, False)
        return
    if expected == "nothing_to_promote":
        assert got == "nothing_to_restart", history
        history.append(f"{kind}:nothing_to_restart")
        _assert_invariants(seq_dir, svc, model, kind, False, pending_before, history, False)
        return
    assert got == "ok", (history, kind, got, expected)
    assert expected in ("ok", "restart_current", "noop"), (history, expected)
    _run_model(model, kind, payload)
    history.append(f"{kind}:{payload}")
    _assert_invariants(seq_dir, svc, model, kind, True, pending_before, history, False)


def _crash_and_reboot(seq_dir, svc, model, kind, payload, rng, history, crash_at=None):
    old = model.snapshot()
    projected = model.clone()
    result = _run_model(projected, kind, payload)
    if result in ("nothing_to_rollback", "nothing_to_promote"):
        got = _run_prod(svc, kind, payload)
        assert got in ("nothing_to_rollback", "nothing_to_restart"), history
        return svc, False
    if result == "restart_current":
        got = _run_prod(svc, kind, payload)
        assert got == "ok", history
        _run_model(model, kind, payload)
        return svc, False
    boot_tried = False if kind in ("confirm_restart", "confirm_startup") and result == "ok" else UNSET
    plan = _commit_plan(
        old, projected.snapshot(), boot_tried=boot_tried,
        active_first=kind == "confirm_startup",
    )
    if not plan:
        got = _run_prod(svc, kind, payload)
        assert got == "ok", history
        _run_model(model, kind, payload)
        return svc, False
    crash_at = crash_at or rng.choice([name for name, _field, _value in plan])
    assert crash_at in [name for name, _field, _value in plan], (crash_at, plan)
    history.append(f"crash:{kind}@{crash_at}:{payload}")
    svc.store.crash_at = crash_at
    try:
        _run_prod(svc, kind, payload)
        raise AssertionError(f"expected OverlayCrash at {crash_at}: {history} plan={plan}")
    except OverlayCrash:
        pass
    finally:
        svc.store.crash_at = None
    _apply_partial(model, old, plan, crash_at)
    svc = _reboot(seq_dir, tuple(sorted(model.models)))
    boot_result = model.reboot()
    _assert_invariants(seq_dir, svc, model, "crash_reboot", True, None, history,
                       boot_result == "lkg_restore", boot_result)
    return svc, True


def test_overlay_lifecycle_property(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_SUPERVISED", "1")
    rng = random.Random(SEED)
    mutating = ("patch_live", "patch_restart", "patch_reset", "rollback", "confirm_restart")
    crashable = mutating + ("confirm_startup",)
    kinds = mutating + ("confirm_startup", "reboot", "crash_reboot", "shrink_yaml")
    for seq in range(N_SEQUENCES):
        seq_dir = tmp_path / f"s{seq}"
        seq_dir.mkdir()
        svc = _fresh(seq_dir)
        model = OverlayModel()
        history: list[str] = []
        for _step in range(STEPS):
            kind = rng.choice(kinds)
            crash = kind == "crash_reboot"
            if crash:
                kind = rng.choice(crashable)
            payload = _payload_for(kind, rng, model.models)
            pending_before = _distinct_pending_restart(model)
            if crash:
                svc, _did = _crash_and_reboot(seq_dir, svc, model, kind, payload, rng, history)
                continue
            if kind == "shrink_yaml":
                history.append("shrink_yaml")
                model.models.discard("qwen-a")
                svc = _reboot(seq_dir, tuple(sorted(model.models)))
                boot_result = model.reboot()
                _assert_invariants(seq_dir, svc, model, "shrink_yaml", True, None, history,
                                   boot_result == "lkg_restore", boot_result)
                continue
            if kind == "reboot":
                history.append("reboot")
                svc = _reboot(seq_dir, tuple(sorted(model.models)))
                boot_result = model.reboot()
                _assert_invariants(seq_dir, svc, model, "reboot", True, None, history,
                                   boot_result == "lkg_restore", boot_result)
                continue
            _apply_success(svc, model, kind, payload, history, pending_before, seq_dir)


def test_overlay_property_corpus_includes_reported_interleavings(tmp_path, monkeypatch):
    """The 22:45Z and 23:49Z findings must fail the model invariants if they regress."""
    monkeypatch.setenv("HARNESS_SUPERVISED", "1")
    svc = _fresh(tmp_path)
    model = OverlayModel()
    history: list[str] = []

    def step(kind, payload=None):
        pending_before = _distinct_pending_restart(model)
        _apply_success(svc, model, kind, payload, history, pending_before, tmp_path)

    step("patch_restart", {"web.enabled": False})
    step("confirm_restart")
    history.append("reboot")
    booted = _reboot(tmp_path)
    restored = model.reboot() == "lkg_restore"
    assert restored is False
    booted.confirm_startup()
    assert model.confirm_startup() == "ok"
    _assert_invariants(tmp_path, booted, model, "confirm_startup", True, None, history, False)

    pending_before = _distinct_pending_restart(model)
    _apply_success(booted, model, "patch_restart", {"web.enabled": True}, history, pending_before, tmp_path)
    pending_before = _distinct_pending_restart(model)
    _apply_success(booted, model, "patch_live", {"sessions.max_turns": 40}, history, pending_before, tmp_path)

    other = tmp_path / "pending-only"
    other.mkdir()
    svc2 = _fresh(other)
    model2 = OverlayModel()
    history2: list[str] = []
    pending_before = _distinct_pending_restart(model2)
    _apply_success(svc2, model2, "patch_restart", {"web.enabled": True}, history2, pending_before, other)
    pending_before = _distinct_pending_restart(model2)
    _apply_success(svc2, model2, "rollback", None, history2, pending_before, other)

    confirmed = tmp_path / "confirmed-restart-rollback"
    confirmed.mkdir()
    svc3 = _fresh(confirmed)
    model3 = OverlayModel()
    history3: list[str] = []
    pending_before = _distinct_pending_restart(model3)
    _apply_success(svc3, model3, "patch_restart", {"web.enabled": True}, history3, pending_before, confirmed)
    pending_before = _distinct_pending_restart(model3)
    _apply_success(svc3, model3, "confirm_restart", None, history3, pending_before, confirmed)
    history3.append("reboot")
    svc3 = _reboot(confirmed)
    boot_result = model3.reboot()
    assert boot_result != "lkg_restore"
    svc3.confirm_startup()
    assert model3.confirm_startup() == "ok"
    _assert_invariants(confirmed, svc3, model3, "confirm_startup", True, None, history3, False)
    pending_before = _distinct_pending_restart(model3)
    _apply_success(svc3, model3, "rollback", None, history3, pending_before, confirmed)
    assert svc3.cfg.web.enabled is True
    assert svc3.store.read_active().values.get("web.enabled") is not True
    assert svc3.admin_view()["restart_required"] is True

    shrink = tmp_path / "shrink-yaml"
    shrink.mkdir()
    svc4 = _fresh(shrink)
    model4 = OverlayModel()
    history4: list[str] = []
    pending_before = _distinct_pending_restart(model4)
    _apply_success(svc4, model4, "patch_live", {"backends.local.model": "qwen-a"}, history4, pending_before, shrink)
    pending_before = _distinct_pending_restart(model4)
    _apply_success(svc4, model4, "patch_live", {"sessions.max_turns": 40}, history4, pending_before, shrink)
    history4.append("shrink_yaml")
    model4.models.discard("qwen-a")
    svc4 = _reboot(shrink, tuple(sorted(model4.models)))
    boot_result = model4.reboot()
    assert boot_result == "overlay_quarantined"
    _assert_invariants(shrink, svc4, model4, "shrink_yaml", True, None, history4, False, boot_result)
    assert svc4.cfg.default_model == "fake"
    assert svc4.cfg.max_turns == 80

    confirm_crash = tmp_path / "confirm-crash"
    confirm_crash.mkdir()
    svc5 = _fresh(confirm_crash)
    model5 = OverlayModel()
    history5: list[str] = []
    _apply_success(
        svc5, model5, "patch_restart", {"web.enabled": True}, history5, None, confirm_crash,
    )
    _apply_success(svc5, model5, "confirm_restart", None, history5, None, confirm_crash)
    history5.append("reboot")
    svc5 = _reboot(confirm_crash)
    assert model5.reboot() == "try_candidate"
    svc5, crashed = _crash_and_reboot(
        confirm_crash, svc5, model5, "confirm_startup", None, random.Random(0), history5,
        crash_at="pending",
    )
    assert crashed is True
    assert svc5.cfg.web.enabled is True
    assert svc5.store.read_pending() is None
    assert svc5.store.boot_tried() is False
    assert svc5.store.read_status().get("recovery") != "lkg_restore"
