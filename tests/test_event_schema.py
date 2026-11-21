"""#1014 (agent-ops#637 stage 2): the SSE event streams' payloads are one discriminated union in the OpenAPI document.

- Every event type the Server's code emits is in the union, and every member of the union is still emitted.
- A real emitted payload of each type validates against its schema: `tests/fixtures/sse_event_samples.json` holds
  payloads recorded from the test run (`tools/openapi/record_event_samples.py`), and `tests/sse_event_capture.py`
  checks every event the Server emits in any test as it happens.
- The OpenAPI document lists the union under components and points each event stream at it.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

from harness import api, event_schema
from harness.api import create_app
from harness.manager import Manager

from test_daemon import make_cfg

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = Path(__file__).resolve().parent / "fixtures" / "sse_event_samples.json"
SOURCES = ("harness", "harness_modules")
BUS_METHODS = {"emit", "aemit", "ephemeral"}

# Call sites that pass the event type in a variable, and where its values come from. A new one fails
# test_every_bus_call_with_a_computed_type_is_resolved until it is listed here with a resolver below.
COMPUTED = {
    ("harness/manager.py", "deliver", "kind"),            # Manager.send(kind=...): `user_message`, `app_context`
    ("harness/runner.py", "_sandbox_event", "type_"),     # the sandbox's on_event("...", ...)
    ("harness/runner.py", "report", "event"),             # Runner._progress_reporter(sid, "...", ...)
}


def _modules():
    for folder in SOURCES:
        for path in sorted((ROOT / folder).rglob("*.py")):
            yield path.relative_to(ROOT).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _functions(tree):
    """(enclosing function name, call) for every call in the module."""
    def visit(node, owner):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else owner
            if isinstance(child, ast.Call):
                yield name, child
            yield from visit(child, name)
    yield from visit(tree, "")


def _literal(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _bus_call(call: ast.Call) -> bool:
    func = call.func
    return (isinstance(func, ast.Attribute) and func.attr in BUS_METHODS and len(call.args) >= 2
            and "bus" in ast.unparse(func.value))


def scan() -> tuple[set[str], set[tuple[str, str, str]]]:
    """The event types the Server's code emits, and the bus calls whose type is computed."""
    literal: set[str] = set()
    computed: set[tuple[str, str, str]] = set()
    kinds: set[str] = set()
    sandbox: set[str] = set()
    progress: set[str] = set()
    for path, tree in _modules():
        for owner, call in _functions(tree):
            func = call.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if _bus_call(call):
                type_ = _literal(call.args[1])
                if type_ is None:
                    computed.add((path, owner, ast.unparse(call.args[1])))
                else:
                    literal.add(type_)
            elif name == "send":
                kinds |= {_literal(k.value) for k in call.keywords if k.arg == "kind"} - {None}
            elif name == "on_event" and call.args and _literal(call.args[0]):
                sandbox.add(_literal(call.args[0]))
            elif name == "_progress_reporter" and len(call.args) >= 2 and _literal(call.args[1]):
                progress.add(_literal(call.args[1]))
        if path == "harness/manager.py":
            send = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "send")
            kinds.add(_literal(send.args.defaults[-1]))
    assert kinds and sandbox and progress, (kinds, sandbox, progress)
    return literal | kinds | sandbox | progress, computed


EMITTED, COMPUTED_SEEN = scan()


def samples() -> dict[str, list[dict]]:
    return json.loads(SAMPLES.read_text(encoding="utf-8"))


def test_every_bus_call_with_a_computed_type_is_resolved():
    assert COMPUTED_SEEN == COMPUTED


def test_every_emitted_event_type_is_in_the_union():
    assert sorted(EMITTED - set(event_schema.EVENTS)) == []


def test_every_type_in_the_union_is_still_emitted():
    assert sorted(set(event_schema.EVENTS) - EMITTED) == []


def test_the_list_union_is_what_the_list_streams_forward():
    assert event_schema.LIST_TYPES == api.GLOBAL_TYPES


def test_the_samples_cover_every_emitted_type():
    assert sorted(EMITTED - set(samples())) == [], "record them: see tools/openapi/record_event_samples.py"
    assert sorted(set(samples()) - EMITTED) == []


@pytest.mark.parametrize("type_", sorted(event_schema.EVENTS))
def test_a_real_payload_of_each_type_validates(type_):
    recorded = samples().get(type_, [])
    assert recorded, f"no recorded {type_} event: see tools/openapi/record_event_samples.py"
    for event in recorded:
        assert event["type"] == type_
        assert event_schema.mismatch(event) is None, event


def _sample(type_: str) -> dict:
    return copy.deepcopy(samples()[type_][0])


def test_a_type_left_out_of_the_union_is_reported(monkeypatch):
    event = _sample("checkpoint")
    assert event_schema.mismatch(event) is None
    monkeypatch.delitem(event_schema.EVENTS, "checkpoint")
    assert "'checkpoint' is not in SessionEvent" in event_schema.mismatch(event)


@pytest.mark.parametrize("type_,field,value", [
    ("tool_result", "ok", "yes"),                 # a bool sent as a string
    ("status", "status", None),                   # a required field sent as null
    ("queue", "position", "1"),
    ("session_created", "unexpected", 1),         # a field the schema doesn't list
])
def test_a_field_that_contradicts_its_schema_is_reported(type_, field, value):
    event = _sample(type_)
    event["data"][field] = value
    assert f"data.{field}" in event_schema.mismatch(event)


def test_a_missing_required_field_and_a_stored_event_without_ts_are_reported():
    event = _sample("assistant")
    del event["data"]["content"]
    assert "data.content" in event_schema.mismatch(event)
    event = _sample("status")
    del event["ts"]
    assert "ts" in event_schema.mismatch(event)


def test_an_ephemeral_event_has_no_seq():
    event = _sample("delta")
    event["seq"] = 7
    assert "seq" in event_schema.mismatch(event)


@pytest.fixture(scope="module")
def openapi(tmp_path_factory):
    manager = Manager(make_cfg(tmp_path_factory.mktemp("openapi")))
    try:
        return create_app(manager).openapi()
    finally:
        manager.db.close()


def test_the_openapi_document_lists_the_union_under_components(openapi):
    schemas = openapi["components"]["schemas"]
    union = schemas["SessionEvent"]
    assert union["discriminator"]["propertyName"] == "type"
    assert set(union["discriminator"]["mapping"]) == set(event_schema.EVENTS)
    assert len(union["oneOf"]) == len(event_schema.EVENTS)
    for type_, ref in union["discriminator"]["mapping"].items():
        member = schemas[ref.rsplit("/", 1)[-1]]
        assert member["properties"]["type"]["const"] == type_
        data = member["properties"]["data"]["$ref"].rsplit("/", 1)[-1]
        assert schemas[data]["type"] == "object"
    listed = schemas["SessionListEvent"]
    assert set(listed["discriminator"]["mapping"]) == set(event_schema.LIST_TYPES)
    assert {item["$ref"] for item in listed["oneOf"]} <= {item["$ref"] for item in union["oneOf"]}


def test_every_event_stream_in_the_openapi_document_points_at_its_union(openapi):
    streams = {path: operation["get"] for path, operation in openapi["paths"].items()
               if path.endswith("/events") and "get" in operation}
    assert set(event_schema.STREAMS) <= set(streams)
    assert len(streams) > len(event_schema.STREAMS)  # the admin aliases are documented too
    for path, operation in streams.items():
        content = operation["responses"]["200"]["content"]
        assert list(content) == ["text/event-stream"], path
        assert content["text/event-stream"]["schema"]["$ref"].rsplit("/", 1)[-1] in ("SessionEvent",
                                                                                         "SessionListEvent"), path


def test_the_event_schemas_take_no_existing_component_name():
    document = {"components": {"schemas": {"StatusEvent": {"type": "object"}}}, "paths": {}}
    with pytest.raises(RuntimeError, match="StatusEvent is taken"):
        event_schema.document(document)


def test_the_committed_document_carries_the_union():
    committed = json.loads((ROOT / "tools" / "openapi" / "openapi.json").read_text(encoding="utf-8"))
    assert set(committed["components"]["schemas"]["SessionEvent"]["discriminator"]["mapping"]) == set(
        event_schema.EVENTS)


def test_the_runner_reports_a_missing_api_key_in_the_documented_shape(tmp_path):
    """No other test reaches `backend_auth_required`: drive the runner's own emit, so the capture checks its payload
    (and a recording run keeps it as the sample)."""
    from fastapi.testclient import TestClient
    from harness.cli_backends import CliBackendError

    manager = Manager(make_cfg(tmp_path))
    manager._spawn = lambda *_a, **_k: None
    with TestClient(create_app(manager)) as client:
        sid = client.post("/api/v1/sessions", json={"prompt": "hosted"}).json()["id"]
        manager.runner._backend_credential = lambda _s: {"policy": "api_key", "source": "app", "key": ""}
        with pytest.raises(CliBackendError, match="no key is available"):
            manager.runner._cli_credentials(sid, manager.db.sessions.get_session(sid), "claude")
    event = manager.db.sessions.events(sid)[-1]
    assert event["type"] == "backend_auth_required"
    assert event["data"] == {"backend": "claude", "auth": "api_key"}
    assert event_schema.mismatch(json.loads(json.dumps(event))) is None


INNER = '''
import pytest
import sse_event_capture

BAD = {"seq": 1, "session_id": "s", "ts": 1.0, "type": "status", "data": {"status": 3}}


@pytest.fixture(scope="module")
def module_setup_emits_a_bad_event():
    sse_event_capture._seen(BAD)  # as if the Server emitted it while a module fixture set up
    yield
    sse_event_capture._seen({**BAD, "type": "queue"})  # and again in its teardown, after the last test


def test_first(module_setup_emits_a_bad_event):
    pass


def test_second():
    pass
'''


def test_an_off_schema_event_from_a_fixture_is_reported_not_dropped(tmp_path):
    """Review of #1016: a module-scoped fixture sets up before the per-test check, and tears down after the last one;
    neither may lose an off-schema event."""
    import os
    import subprocess
    import sys
    (tmp_path / "test_inner.py").write_text(INNER, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "tests"), str(ROOT)]), "PYTEST_ADDOPTS": ""}
    env.pop("HARNESS_RECORD_SSE_EVENTS", None)
    run = subprocess.run([sys.executable, "-m", "pytest", "-p", "conftest", "-p", "no:xdist", "-p", "no:cacheprovider",
                          "-q", "-rE", f"--basetemp={tmp_path / 'inner'}", str(tmp_path / "test_inner.py")],
                         cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    out = run.stdout + run.stderr
    assert run.returncode == 1, out
    assert "ERROR at teardown of test_first" in out and "status.data.status" in out, out
    assert "ERROR at teardown of test_second" in out and "after the last test" in out and "queue.data" in out, out


def test_an_aemit_whose_awaiter_is_cancelled_is_still_checked(tmp_path, monkeypatch):
    """Review of #1016: `Database.awrite` commits and delivers even when its awaiter is cancelled, so the check runs as
    the event is inserted, not when `aemit` returns."""
    import asyncio
    import sse_event_capture
    from harness.bus import EventBus
    from harness.db import Database

    monkeypatch.setattr(sse_event_capture, "TESTS", tmp_path / "elsewhere")  # this call site counts as the Server's
    db = Database(tmp_path / "events.sqlite3")
    bus = EventBus(db)

    async def body():
        task = asyncio.create_task(bus.aemit("s1", "status", {"status": 3}))
        await asyncio.sleep(0)  # submitted to the writer
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    try:
        asyncio.run(body())
        assert [e["type"] for e in db.sessions.events("s1")] == ["status"]  # committed despite the cancel
        found, sse_event_capture.problems[:] = list(sse_event_capture.problems), []
        assert any("status.data.status" in problem for problem in found), found
    finally:
        db.close()
