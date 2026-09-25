"""Single overlay state transition for the managed-config lifecycle (issue #66).

Every writer (PATCH, rollback, confirm-restart, confirm-startup, LKG restore)
goes through ``next_overlay``. The only merge is ``effective_candidate``:
confirmed ``active`` overlaid by ``pending`` (pending wins per key). Boot
applies ``active`` only — never this candidate — until the owner confirms a
restart that copies pending onto active.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal, cast

from .managed_config import UNSET, Envelope, SCHEMA_VERSION
from .settings import RESET

ApplyMode = Callable[[str], str]
Action = Literal["patch", "rollback", "confirm_restart", "confirm_startup", "restore_lkg"]


@dataclass(frozen=True)
class OverlayState:
    active: Envelope | None = None
    pending: Envelope | None = None
    lkg: Envelope | None = None


@dataclass(frozen=True)
class OverlayRequest:
    action: Action
    changes: dict[str, Any] = field(default_factory=dict)
    next_revision: int = 0
    now: float = 0.0
    migrated_backend_prefs: bool = True


def effective_candidate(active: Envelope | None, pending: Envelope | None) -> dict[str, Any]:
    """Confirmed active overlaid by pending. Pending wins for keys it mentions."""
    values = dict(active.values) if active is not None else {}
    if pending is not None:
        values.update(dict(pending.values))
    return values


def apply_changes(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    values = dict(base)
    for key, value in changes.items():
        if value is RESET:
            values.pop(key, None)
        else:
            values[key] = value
    return values


def next_overlay(state: OverlayState, request: OverlayRequest, apply_mode: ApplyMode) -> OverlayState:
    """Return the next (active, pending, lkg) triple. Pure: does not touch disk."""
    active = _copy(state.active)
    pending = _copy(state.pending)
    lkg = _copy(state.lkg)
    if request.action == "patch":
        return _patch(active, pending, lkg, request, apply_mode)
    if request.action == "rollback":
        return _rollback(active, pending, lkg, request)
    if request.action == "confirm_restart":
        return _confirm_restart(active, pending, lkg, request)
    if request.action == "confirm_startup":
        return _confirm_startup(active, pending, lkg, request)
    if request.action == "restore_lkg":
        return _restore_lkg(lkg)
    raise ValueError(f"unknown overlay action {request.action!r}")


def overlay_commit_args(old: OverlayState, new: OverlayState, *, boot_tried=UNSET) -> dict[str, Any]:
    """Translate a state transition into ``ManagedStore.commit`` kwargs.

    Unchanged files are left ``UNSET`` so commit order stays crash-safe:
    clearing pending happens only when the next state has no pending, and
    never as a silent extra step before replacing active.
    """
    args: dict[str, Any] = {}
    if new.active is None:
        if old.active is not None:
            args["unlink_active"] = True
    elif not _same_payload(old.active, new.active):
        args["active"] = new.active
    if new.pending is None:
        if old.pending is not None:
            args["pending"] = None
    elif old.pending is None or not _same_payload(old.pending, new.pending):
        args["pending"] = new.pending
    if new.lkg is not None and not _same_payload(old.lkg, new.lkg):
        args["lkg"] = new.lkg
    if boot_tried is not UNSET:
        args["boot_tried"] = boot_tried
    return args


def envelope_payload(env: Envelope | None) -> tuple | None:
    if env is None:
        return None
    return (
        env.revision,
        bool(env.confirmed and not env.unconfirmed),
        tuple(sorted(env.values.items())),
    )


def _same_payload(left: Envelope | None, right: Envelope | None) -> bool:
    return envelope_payload(left) == envelope_payload(right)


def _copy(env: Envelope | None) -> Envelope | None:
    if env is None:
        return None
    return cast(Envelope, replace(env, values=dict(env.values)))


def is_confirmed(env: Envelope | None) -> bool:
    if env is None:
        return True
    return bool(env.confirmed and not env.unconfirmed)


def _split(values: dict[str, Any], apply_mode: ApplyMode) -> tuple[dict[str, Any], dict[str, Any]]:
    live: dict[str, Any] = {}
    restart: dict[str, Any] = {}
    for key, value in values.items():
        mode = apply_mode(key)
        if mode == "daemon_restart":
            restart[key] = value
        elif mode == "installer_only":
            continue
        else:
            live[key] = value
    return live, restart


def _envelope(*, revision: int, confirmed: bool, values: dict[str, Any],
              previous_revision: int | None, now: float, migrated: bool) -> Envelope:
    unconfirmed = not confirmed
    return Envelope(
        schema_version=SCHEMA_VERSION,
        revision=revision,
        confirmed=confirmed and not unconfirmed,
        values=dict(values),
        updated_at=now,
        migrated_backend_prefs=migrated,
        previous_revision=previous_revision,
        unconfirmed=unconfirmed,
    )


def _empty_confirmed() -> Envelope:
    return Envelope(revision=0, confirmed=True, unconfirmed=False, values={},
                    migrated_backend_prefs=True)


def _snapshot_confirmed(active: Envelope | None) -> Envelope:
    source = active if active is not None else _empty_confirmed()
    return cast(Envelope, replace(source, values=dict(source.values), confirmed=True, unconfirmed=False))


def _patch(active: Envelope | None, pending: Envelope | None, lkg: Envelope | None,
           request: OverlayRequest, apply_mode: ApplyMode) -> OverlayState:
    candidate = apply_changes(effective_candidate(active, pending), request.changes)
    live, restart_cand = _split(candidate, apply_mode)
    _ignored, promoted = _split(active.values if active is not None else {}, apply_mode)
    new_active_values = {**live, **promoted}
    _ignored, restart_active = _split(new_active_values, apply_mode)
    stay_confirmed = is_confirmed(active)
    prev_rev = active.revision if active is not None and active.revision else None
    new_active = _envelope(
        revision=request.next_revision,
        confirmed=stay_confirmed,
        values=new_active_values,
        previous_revision=prev_rev,
        now=request.now,
        migrated=request.migrated_backend_prefs,
    )
    if restart_cand != restart_active:
        new_pending = _envelope(
            revision=request.next_revision,
            confirmed=False,
            values=dict(candidate),
            previous_revision=prev_rev,
            now=request.now,
            migrated=request.migrated_backend_prefs,
        )
    else:
        new_pending = None
    new_lkg = lkg
    if is_confirmed(active):
        new_lkg = _snapshot_confirmed(active)
    return OverlayState(active=new_active, pending=new_pending, lkg=new_lkg)


def _rollback(active: Envelope | None, pending: Envelope | None, lkg: Envelope | None,
              request: OverlayRequest) -> OverlayState:
    if lkg is None:
        return OverlayState(active=active, pending=pending, lkg=None)
    prev_rev = active.revision if active is not None and active.revision else None
    new_active = _envelope(
        revision=request.next_revision,
        confirmed=True,
        values=dict(lkg.values),
        previous_revision=prev_rev,
        now=request.now,
        migrated=True,
    )
    new_lkg = lkg
    if is_confirmed(active):
        new_lkg = _snapshot_confirmed(active)
    return OverlayState(active=new_active, pending=None, lkg=new_lkg)


def _confirm_restart(active: Envelope | None, pending: Envelope | None, lkg: Envelope | None,
                     request: OverlayRequest) -> OverlayState:
    if pending is None:
        return OverlayState(active=active, pending=None, lkg=lkg)
    new_lkg = lkg
    if active is not None and is_confirmed(active):
        new_lkg = _snapshot_confirmed(active)
    new_active = replace(
        pending,
        values=dict(pending.values),
        confirmed=False,
        unconfirmed=True,
        previous_revision=active.revision if active is not None else None,
        updated_at=request.now or pending.updated_at,
    )
    # Leave pending in place until confirm_startup. Clearing it is a commit
    # step *before* replacing active; a crash there would drop the candidate
    # while the old confirmed generation is still in effect.
    return OverlayState(active=new_active, pending=_copy(pending), lkg=new_lkg)


def _confirm_startup(active: Envelope | None, pending: Envelope | None,
                     lkg: Envelope | None, request: OverlayRequest) -> OverlayState:
    if active is None or is_confirmed(active):
        return OverlayState(active=active, pending=pending, lkg=lkg)
    new_active = replace(
        active,
        values=dict(active.values),
        confirmed=True,
        unconfirmed=False,
        updated_at=request.now or active.updated_at,
    )
    return OverlayState(active=new_active, pending=None, lkg=lkg)


def _restore_lkg(lkg: Envelope | None) -> OverlayState:
    if lkg is None:
        return OverlayState(active=None, pending=None, lkg=None)
    new_active = replace(lkg, values=dict(lkg.values), confirmed=True, unconfirmed=False)
    return OverlayState(active=new_active, pending=None, lkg=lkg)
