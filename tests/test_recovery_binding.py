"""Class-level invariant for restart-recovery approval binding (#925).

After a restart a CLI regenerates an interrupted tool request under a new call id, and the runner binds it to an
existing approval by tool and args. Whatever else the session holds (the same command asked and decided before the
restart, approvals asked since, approvals a live request already waits on), the binding may only ever pick the single
approval the restart left unanswered, with that tool and args, that no live request waits on; it keeps any decision
made on it since the restart and binds it once. Otherwise the request is asked anew. This test checks that rule over
every session of up to three approvals drawn from each status, restart state, bound state and args match, rather
than one hand-picked case.
"""
from __future__ import annotations

import asyncio
import itertools

import pytest

from harness.manager import Manager
from test_daemon import make_cfg

TOOL, ARGS = "Bash", {"command": "pytest -q"}
OTHER = {"command": "ruff check ."}
STATUSES = ("pending", "approved", "denied", "cancelled")
# One approval's shape: (status now, left unanswered by the restart, bound to a live request, same args as the
# regenerated request). Unanswered and decided since: the owner answered the card before the request came back.
# Answered and pending: asked after the restart. Answered and decided: history from before the restart.
SHAPES = [(status, unanswered, bound, same) for status in STATUSES for unanswered in (False, True)
          for bound in (False, True) for same in (False, True)
          if not bound or status == "pending"]  # only a pending approval can have a live waiter


@pytest.fixture
def runner_db(tmp_path):
    m = Manager(make_cfg(tmp_path))
    yield m.runner, m.db
    m.db.close()


def _expected(session: list[tuple[str, str, bool, bool, bool]]) -> str | None:
    """The rule, stated independently of the runner: the one unanswered, unbound, same-args approval, else none."""
    candidates = [aid for aid, _status, unanswered, bound, same in session if unanswered and not bound and same]
    return candidates[0] if len(candidates) == 1 else None


@pytest.mark.parametrize("size", [1, 2, 3])
def test_recovery_binds_only_to_the_single_unanswered_unbound_twin(runner_db, size):
    runner, db = runner_db
    checked = 0
    for n, combo in enumerate(itertools.product(SHAPES, repeat=size)):
        sid = f"s-{size}-{n}"
        db.insert_session({"id": sid, "title": "t", "project": "p", "target": "tower", "model": "m",
                           "backend": "claude", "status": "waiting_approval", "workspace": "",
                           "created_at": 1.0, "updated_at": 1.0, "context": [], "run": {}})
        session = []
        unanswered_ids = set()
        for i, (status, unanswered, bound, same) in enumerate(combo):
            aid = f"a-{size}-{n}-{i}"
            db.insert_approval({"id": aid, "session_id": sid, "tool_call_id": f"tool-{i}", "tool": TOOL,
                                "args": ARGS if same else OTHER, "reason": "asks", "status": status})
            if unanswered:
                unanswered_ids.add(aid)
            if bound:
                runner.approval_events[aid] = asyncio.Event()
            session.append((aid, status, unanswered, bound, same))
        runner._recovery_unanswered[sid] = unanswered_ids
        bound_to = runner._recovery_approval(sid, TOOL, ARGS)
        got = bound_to["id"] if bound_to else None
        assert got == _expected(session), f"{combo}: bound to {got}, expected {_expected(session)}"
        if bound_to is not None:
            assert bound_to["args"] == ARGS
            assert bound_to["status"] == next(s for aid, s, *_ in session if aid == got)  # keeps its decision
            assert runner._recovery_approval(sid, TOOL, ARGS) is None, f"{combo}: {got} bound twice"
        for aid, *_ in session:
            runner.approval_events.pop(aid, None)
        checked += 1
    assert checked == len(SHAPES) ** size


def test_recovery_never_binds_a_different_tool(runner_db):
    runner, db = runner_db
    db.insert_session({"id": "s-tool", "title": "t", "project": "p", "target": "tower", "model": "m",
                       "backend": "claude", "status": "waiting_approval", "workspace": "",
                       "created_at": 1.0, "updated_at": 1.0, "context": [], "run": {}})
    db.insert_approval({"id": "a-tool", "session_id": "s-tool", "tool_call_id": "tool-1", "tool": "Write",
                        "args": ARGS, "reason": "asks"})
    runner._recovery_unanswered["s-tool"] = {"a-tool"}
    assert runner._recovery_approval("s-tool", TOOL, ARGS) is None
    assert runner._recovery_approval("s-tool", "Write", ARGS)["id"] == "a-tool"


def test_recovery_binds_nothing_outside_a_recovered_run(runner_db):
    """No snapshot (a run that was not recovered): even a lone pending twin is not bound by tool and args."""
    runner, db = runner_db
    db.insert_session({"id": "s-new", "title": "t", "project": "p", "target": "tower", "model": "m",
                       "backend": "claude", "status": "waiting_approval", "workspace": "",
                       "created_at": 1.0, "updated_at": 1.0, "context": [], "run": {}})
    db.insert_approval({"id": "a-new", "session_id": "s-new", "tool_call_id": "tool-1", "tool": TOOL,
                        "args": ARGS, "reason": "asks"})
    assert runner._recovery_approval("s-new", TOOL, ARGS) is None


def test_startup_snapshot_takes_hosted_sessions_pending_approvals_only(runner_db):
    runner, db = runner_db
    for sid, backend in (("s-hosted", "claude"), ("s-local", "local")):
        db.insert_session({"id": sid, "title": "t", "project": "p", "target": "tower", "model": "m",
                           "backend": backend, "status": "waiting_approval", "workspace": "",
                           "created_at": 1.0, "updated_at": 1.0, "context": [], "run": {}})
        for aid, status in ((f"a-{sid}-old", "denied"), (f"a-{sid}-now", "pending")):
            db.insert_approval({"id": aid, "session_id": sid, "tool_call_id": aid, "tool": TOOL, "args": ARGS,
                                "reason": "asks", "status": status})
        asyncio.run(runner.mark_recovered_place(db.get_session(sid)))
    assert runner._recovery_unanswered == {"s-hosted": {"a-s-hosted-now"}}
