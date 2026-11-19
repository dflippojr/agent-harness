"""Watch every event the Server's own code puts on the event bus during the test run (#1014).

`install()` wraps `EventBus.emit`, `aemit` and `ephemeral`. An event whose call site is in the Server's code (not in a
test that builds a fixture by emitting directly) is turned into its wire form, the JSON an SSE frame carries, and:

- checked against the documented schema (`harness.event_schema`); a mismatch fails the test that emitted it;
- with `HARNESS_RECORD_SSE_EVENTS=<dir>`, also appended to `<dir>/events-<pid>.jsonl`, the input of
  `tools/openapi/record_event_samples.py`, which refreshes `tests/fixtures/sse_event_samples.json`.

Nothing here changes what the Server emits: the wrappers call the originals and return what they return.
"""

from __future__ import annotations

import contextvars
import json
import os
import sys
from pathlib import Path

from harness import bus as bus_mod

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
# Plumbing between a call site and the bus: a frame here is never the origin of an event.
_PLUMBING = {Path(bus_mod.__file__).resolve(), (ROOT / "harness" / "db.py").resolve(),
             (ROOT / "harness" / "app_stores.py").resolve()}

problems: list[str] = []
# Set by the `aemit` wrapper for its insert, which runs on the database's writer thread in a copy of this context
# (`Database.awrite`): whether the event is the Server's. There the stack holds no call site to judge by.
_aemit_from_server: contextvars.ContextVar[bool | None] = contextvars.ContextVar("aemit_from_server", default=None)
_record_dir = os.environ.get("HARNESS_RECORD_SSE_EVENTS", "")


def _origin(frame) -> Path | None:
    """The first frame above the bus that is not plumbing or the standard library; None when there is none (the
    writer thread running an `aemit`'s insert)."""
    stdlib = Path(os.__file__).resolve().parent
    while frame is not None:
        path = Path(frame.f_code.co_filename).resolve()
        if path not in _PLUMBING and path != Path(__file__).resolve() and stdlib not in path.parents:
            return path
        frame = frame.f_back
    return None


def _from_server(frame) -> bool:
    origin = _origin(frame)
    if origin is None:
        return bool(_aemit_from_server.get())
    return TESTS not in origin.parents and origin != TESTS


def _wire(event: dict) -> dict:
    return json.loads(json.dumps(event))


def _seen(event: dict) -> None:
    wire = _wire(event)
    if _record_dir:
        out = Path(_record_dir) / f"events-{os.getpid()}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(wire, sort_keys=True) + "\n")
    from harness import event_schema
    error = event_schema.mismatch(wire)
    if error:
        problems.append(f"{wire.get('type')!r} event does not match its schema: {error}\n  {json.dumps(wire)[:2000]}")


def install() -> None:
    cls = bus_mod.EventBus
    if getattr(cls, "_sse_capture_installed", False):
        return
    emit, aemit, ephemeral = cls.emit, cls.aemit, cls.ephemeral

    def watched_emit(self, sid, type_, data):
        event = emit(self, sid, type_, data)
        if _from_server(sys._getframe(1)):
            _seen(event)
        return event

    async def watched_aemit(self, sid, type_, data):
        # The writer-side `emit` checks the event as it is inserted, so an awaiter cancelled after the commit (the
        # event is still delivered) cannot skip the check.
        token = _aemit_from_server.set(_from_server(sys._getframe(1)))
        try:
            return await aemit(self, sid, type_, data)
        finally:
            _aemit_from_server.reset(token)

    def watched_ephemeral(self, sid, type_, data):
        ephemeral(self, sid, type_, data)
        if _from_server(sys._getframe(1)):
            _seen({"seq": None, "session_id": sid, "type": type_, "data": data})

    cls.emit, cls.aemit, cls.ephemeral = watched_emit, watched_aemit, watched_ephemeral
    cls._sse_capture_installed = True
    _watch_sandbox_events()


def _watch_sandbox_events() -> None:
    """A Sandbox reports its events (setup result, disk limit) through `on_event`, and the runner emits them unchanged
    (`Runner._sandbox_event`). Sandbox tests pass their own callback, so see what the Sandbox hands it too."""
    from harness import sandbox

    init = sandbox.Sandbox.__init__

    def watched_init(self, *args, **kwargs):
        init(self, *args, **kwargs)
        report = self.on_event
        code = getattr(report, "__code__", None)
        # The runner's own callback emits on the bus, where the wrappers above see the event with its real envelope.
        if report is None or code is None or TESTS not in Path(code.co_filename).resolve().parents:
            return

        def on_event(type_, data):
            # The payload is the Sandbox's own; the envelope stands in for the one the bus would add.
            _seen({"seq": 0, "session_id": self.session_id, "ts": 0.0, "type": type_, "data": data})
            return report(type_, data)
        self.on_event = on_event

    sandbox.Sandbox.__init__ = watched_init
