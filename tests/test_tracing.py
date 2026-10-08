"""OpenTelemetry session traces (#259): off by default, one trace per session, privacy allowlist."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from harness import telemetry
from harness.config import TelemetryConfig
from harness.llm import Completion
from harness.manager import Manager
from harness_modules.local_model.warmup import READY
from test_daemon import Script, call, make_cfg, wait_status
from waits import timeout_scale

ROOT = Path(__file__).resolve().parent.parent
SENTINEL = "zq-sentinel-7f3a91"
ON = TelemetryConfig(otlp_endpoint="http://127.0.0.1:4318/v1/traces")


@pytest.fixture(autouse=True)
def tracing_reset():
    yield
    telemetry.configure(None)


def _memory_tracer():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exporter = InMemorySpanExporter()
    telemetry.configure(ON, processor=SimpleSpanProcessor(exporter))
    return exporter


def _traced_manager(cfg, chat):
    cfg.telemetry = ON
    m = Manager(cfg, chat=chat)
    exporter = _memory_tracer()  # replaces the OTLP exporter Manager configured
    return m, exporter


# The longest un-spanned stretch a parent may have. Real work in these tests (a 150 ms model call, 200-300 ms waits)
# is far longer, so leaving any of it outside a child span fails; the few-ms bookkeeping between spans doesn't,
# however slow the runner. A summed-coverage ratio did fail there: many sub-5 ms gaps added up (#312).
# It scales with the CI factor from waits.py (50 ms locally, 250 ms on CI/coverage): hosted runners showed 119-147 ms
# bookkeeping gaps (#333). A 1 s gap still fails there.
MAX_GAP_NS = int(50_000_000 * max(1.0, timeout_scale() * 1.25))
# Some span edges come from float-second timestamps (about 256 ns of precision at today's epoch), so a shared edge
# can land a few hundred ns either way.
EDGE_NS = 1_000


def _gaps(parent, children) -> list[tuple[str, str, int]]:
    """Every uncovered stretch of `parent` as (before, after, ms)."""
    out, cursor, prev = [], parent.start_time, "start"
    for c in sorted(children, key=lambda c: c.start_time):
        if c.start_time > cursor:
            out.append((prev, c.name, (c.start_time - cursor) / 1_000_000))
        if c.end_time > cursor:
            cursor, prev = c.end_time, c.name
    if parent.end_time > cursor:
        out.append((prev, "end", (parent.end_time - cursor) / 1_000_000))
    return out


def _assert_tiled(parent, children, *, resume_boundary=None) -> None:
    """`children` account for all of `parent`'s time: each lies inside it, siblings don't overlap, and no gap
    between them is longer than MAX_GAP_NS, except an explicitly identified resume boundary."""
    assert children, f"{parent.name} has no child spans"
    ordered = sorted(children, key=lambda c: c.start_time)
    for c in ordered:
        assert c.start_time <= c.end_time, f"{c.name} ends before it starts"
        assert parent.start_time - EDGE_NS <= c.start_time and c.end_time <= parent.end_time + EDGE_NS,             f"{c.name} outside {parent.name}"
    for a, b in zip(ordered, ordered[1:]):
        assert a.end_time <= b.start_time + EDGE_NS, f"{a.name} overlaps {b.name} under {parent.name}"
    allowed = None
    if resume_boundary is not None:
        idle, setup = resume_boundary
        assert parent.name == "session" and idle.name == "idle" and setup.name == "run_setup"
        # Require both spans and their adjacency: removing setup must not exempt idle -> turn instead.
        assert any(a is idle and b is setup for a, b in zip(ordered, ordered[1:]))
        allowed = (idle.end_time, setup.start_time)
    gaps = _gaps(parent, ordered)
    cursor = parent.start_time
    for child in ordered:
        gap = (cursor, child.start_time)
        assert gap == allowed or child.start_time - cursor <= MAX_GAP_NS, (parent.name, gaps)
        cursor = max(cursor, child.end_time)
    assert parent.end_time - cursor <= MAX_GAP_NS, (parent.name, gaps)


# --- off by default ---

def test_runner_and_a_session_never_import_opentelemetry(tmp_path):
    # fastapi >= 0.142 depends on opentelemetry-api and imports it itself (Manager loads harness.apps, which
    # imports fastapi), so for a full session the check is that no harness code imports opentelemetry and that
    # the SDK and exporter never load. `import harness.runner` alone loads no opentelemetry module at all.
    script = f"""
import asyncio, builtins, sys, time
from pathlib import Path
HARNESS = {str(ROOT / 'harness')!r}
harness_imports = []
_import = builtins.__import__
def spy(name, globals=None, *args, **kwargs):
    if name.startswith("opentelemetry") and str((globals or {{}}).get("__file__", "")).startswith(HARNESS):
        harness_imports.append(name)
    return _import(name, globals, *args, **kwargs)
builtins.__import__ = spy
import harness.runner
assert not [n for n in sys.modules if n.split(".")[0] == "opentelemetry"], "harness.runner imported opentelemetry"
from harness import telemetry
from harness.config import Config, ModelConfig, Project, SandboxConfig
from harness.llm import Completion
from harness.manager import Manager

async def chat(model, messages, tools, *args, **kw):
    if not any(m["role"] == "assistant" for m in messages):
        return Completion(tool_calls=[{{"id": "c0", "type": "function",
                                        "function": {{"name": "write_file", "arguments": '{{"path": "a.txt", "content": "hi"}}'}}}}])
    return Completion(content="done")

async def body():
    tmp = Path({str(tmp_path)!r})
    cfg = Config(host="127.0.0.1", port=0, data_dir=tmp / "data", repos_dir=tmp / "repos", default_model="fake",
                 models={{"fake": ModelConfig(name="fake", base_url="http://unused")}},
                 sandbox=SandboxConfig(), projects={{"scratch": Project(name="scratch")}})
    m = Manager(cfg, chat=chat)
    assert isinstance(telemetry.tracer(), telemetry.NoopTracer)
    await m.start()
    sid = m.create("try")["id"]
    deadline = time.monotonic() + 60
    while m.db.get_session(sid)["status"] not in ("done", "failed") and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    s = m.db.get_session(sid)
    assert s["status"] == "done", s["status"]
    assert m.summary(s)["trace_id"] == "" and "trace_url" not in m.summary(s)
    assert "trace" not in s["run"]
    await m.stop()
asyncio.run(body())
assert harness_imports == [], harness_imports
sdk = sorted(n for n in sys.modules if n.startswith(("opentelemetry.sdk", "opentelemetry.exporter")))
assert not sdk, sdk
print("clean")
"""
    out = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert "clean" in out.stdout


def test_only_telemetry_module_mentions_opentelemetry():
    offenders = [p.name for p in (ROOT / "harness").rglob("*.py")
                 if p.name != "telemetry.py" and "opentelemetry" in p.read_text(encoding="utf-8")]
    assert offenders == []
    source = (ROOT / "harness" / "telemetry.py").read_text(encoding="utf-8")
    top_level = [line for line in source.splitlines() if line.startswith(("import opentelemetry",
                                                                          "from opentelemetry"))]
    assert top_level == []  # imported lazily, inside functions only


def test_call_sites_get_the_noop_shim_when_off():
    tracer = telemetry.configure(TelemetryConfig())
    assert isinstance(tracer, telemetry.NoopTracer) and not tracer.enabled
    with telemetry.span("turn") as span, telemetry.tool_span("read_file", "c1") as tool:
        assert span is telemetry.NOOP_SPAN and tool is telemetry.NOOP_SPAN
    assert tracer.new_trace() == {}
    with tracer.session_run({"trace_id": "1" * 32, "span_id": "2" * 16}, 0.0) as root:
        assert root is telemetry.NOOP_SPAN


def test_missing_packages_warn_once_and_session_runs(tmp_path, monkeypatch, caplog):
    # The realistic gap: opentelemetry-api is there (fastapi depends on it) but requirements-telemetry.txt isn't.
    blocked = ["opentelemetry.sdk", "opentelemetry.exporter"]
    blocked += [n for n in sys.modules if n.startswith(tuple(b + "." for b in blocked))]
    for name in blocked:
        monkeypatch.setitem(sys.modules, name, None)  # importing it raises ImportError
    cfg = make_cfg(tmp_path)
    cfg.telemetry = ON
    caplog.set_level(logging.WARNING, logger="harness.telemetry")

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="done")]))
        await m.start()
        s = await wait_status(m, m.create("try")["id"], "done")
        assert m.summary(s)["trace_id"] == ""
        await m.stop()
    asyncio.run(body())
    warnings = [r for r in caplog.records if "OpenTelemetry is not installed" in r.getMessage()]
    assert len(warnings) == 1


# --- on (in-memory exporter) ---

def test_allowlist_drops_unknown_keys_and_non_scalars():
    exporter = _memory_tracer()
    tracer = telemetry.tracer()
    trace = tracer.new_trace()
    with tracer.session_run(trace, 1.0):
        with telemetry.span("chat", {"gen_ai.prompt": "secret", "harness.ok": True, "gen_ai.tool.name": ["x"],
                                     "harness.prompt_ms": None}) as span:
            span.set({"tool.arguments": "secret", "gen_ai.usage.input_tokens": 3})
    chat = next(s for s in exporter.get_finished_spans() if s.name == "chat")
    assert dict(chat.attributes) == {"harness.ok": True, "gen_ai.usage.input_tokens": 3}
    assert telemetry.clean_attributes({"error.message": "x", "error.type": "ValueError"}) == {
        "error.type": "ValueError"}


def test_errors_record_the_class_only():
    exporter = _memory_tracer()
    tracer = telemetry.tracer()
    with pytest.raises(ValueError):
        with tracer.session_run(tracer.new_trace(), 1.0):
            with telemetry.span("sandbox_exec"):
                raise ValueError(SENTINEL)
    for span in exporter.get_finished_spans():
        assert span.attributes.get("error.type") == "ValueError"
        assert SENTINEL not in (span.status.description or "")
        assert not span.events


def test_spans_outside_a_session_are_not_recorded():
    exporter = _memory_tracer()
    with telemetry.span("image_job") as span:
        assert span is telemetry.NOOP_SPAN
    assert exporter.get_finished_spans() == ()


def test_scripted_session_produces_one_covering_trace(tmp_path):
    cfg = make_cfg(tmp_path, rules=[{"tool": "write_file", "path": "secret/*", "action": "ask", "reason": "x"}])
    cfg.elide_at = 0       # compact before every model call
    cfg.summarize_at = 99
    cfg.reset_at = 99
    base = Script([
        Completion(tool_calls=[call("write_file", 0, path="secret/a.txt", content="x")],
                   prompt_tokens=50, completion_tokens=5, cache_tokens=10, prompt_ms=12.5, decode_ms=40.0),
        Completion(content="first answer", prompt_tokens=60, completion_tokens=3),
        Completion(content="second answer", prompt_tokens=70, completion_tokens=3),
    ])

    async def slow_chat(*args, **kwargs):
        await asyncio.sleep(0.15)  # model time dominates the bookkeeping between spans
        return await base(*args, **kwargs)

    async def body():
        m, exporter = _traced_manager(cfg, slow_chat)
        await m.start()
        await m.scheduler.acquire("blocker")  # another session holds the GPU: a real gpu_slot_wait

        async def free_gpu():
            await asyncio.sleep(0.3)
            m.scheduler.release("blocker")
        releaser = asyncio.create_task(free_gpu())
        s = m.create("write the secret", project="guarded")
        assert len(m.summary(s)["trace_id"]) == 32
        s = await wait_status(m, s["id"], "waiting_approval")
        await asyncio.sleep(0.3)
        approval = m.db.pending_approvals(s["id"])[0]
        m.decide(s["id"], approval["id"], True)
        await wait_status(m, s["id"], "done")
        await releaser
        await asyncio.sleep(0.3)  # idle between runs
        await m.send(s["id"], "and again")
        await asyncio.sleep(0.05)
        s = await wait_status(m, s["id"], "done")
        trace_id = m.summary(s)["trace_id"]
        await m.stop()
        return trace_id, exporter.get_finished_spans()

    trace_id, spans = asyncio.run(body())
    assert {format(sp.context.trace_id, "032x") for sp in spans} == {trace_id}
    names = {sp.name for sp in spans}
    assert {"session", "idle", "turn", "chat", "execute_tool", "approval_wait", "sandbox_exec", "gpu_slot_wait",
            "compaction"} <= names
    assert names <= telemetry.SPAN_NAMES
    roots = [sp for sp in spans if sp.name == "session"]
    assert len({r.context.span_id for r in roots}) == 1 and len(roots) == 2  # re-exported once per run
    root = max(roots, key=lambda r: r.end_time)
    assert all(r.parent is None for r in roots)

    children = [sp for sp in spans if sp.parent is not None and sp.parent.span_id == root.context.span_id]
    assert {c.name for c in children} >= {"turn", "idle", "gpu_slot_wait"}
    _assert_tiled(root, children)
    for turn in (sp for sp in spans if sp.name == "turn"):
        kids = [sp for sp in spans if sp.parent is not None and sp.parent.span_id == turn.context.span_id]
        _assert_tiled(turn, kids)

    chat = next(sp for sp in spans if sp.name == "chat" and sp.attributes.get("gen_ai.usage.input_tokens") == 50)
    assert chat.attributes["gen_ai.request.model"] == "fake"
    assert chat.attributes["gen_ai.usage.output_tokens"] == 5
    assert chat.attributes["harness.cache_read_tokens"] == 10
    assert chat.attributes["harness.prompt_ms"] == 12.5 and chat.attributes["harness.decode_ms"] == 40.0
    tool = next(sp for sp in spans if sp.name == "execute_tool")
    assert tool.attributes["gen_ai.tool.name"] == "write_file"
    assert tool.attributes["harness.policy_decision"] == "ask"
    tool_kids = {sp.name for sp in spans if sp.parent is not None and sp.parent.span_id == tool.context.span_id}
    assert {"approval_wait", "sandbox_exec"} <= tool_kids
    wait = next(sp for sp in spans if sp.name == "approval_wait")
    assert wait.attributes["harness.approval_status"] == "approved"
    # The snapshot after write_file runs in the background; its span is where the run waited for it.
    ckpt = next(sp for sp in spans if sp.name == "checkpoint")
    assert ckpt.parent.span_id in {sp.context.span_id for sp in spans}
    assert ckpt.attributes["harness.turn"] == 1 and ckpt.attributes["harness.files"] >= 1
    assert ckpt.attributes["harness.bytes"] >= 1 and "harness.skipped_reason" not in ckpt.attributes


@pytest.mark.parametrize("delay_resume", [False, True], ids=["normal", "delayed-resume"])
def test_recovered_pending_calls_run_under_a_resumed_turn(tmp_path, monkeypatch, delay_resume):
    """A daemon restart mid-approval resolves the pending call under a `turn`, not straight under `session`."""
    cfg = make_cfg(tmp_path, rules=[{"tool": "write_file", "path": "secret/*", "action": "ask", "reason": "x"}])
    script = Script([
        Completion(tool_calls=[call("write_file", 0, path="secret/a.txt", content="x")]),
        Completion(content="done writing"),
    ])

    async def slow_chat(*args, **kwargs):
        await asyncio.sleep(0.15)  # model time dominates the bookkeeping between spans
        return await script(*args, **kwargs)

    async def ready(model):
        return READY

    def daemon():
        m, exporter = _traced_manager(cfg, slow_chat)
        m.warmer.state = ready  # skip the un-spanned probe of the fake model's unresolvable base_url
        return m, exporter

    async def first():
        m, exporter = daemon()
        await m.start()
        s = m.create("write the secret", project="guarded")
        await wait_status(m, s["id"], "waiting_approval")
        await m.stop()
        m.db.close()
        return s["id"], exporter.get_finished_spans()

    async def second(sid):
        m, exporter = daemon()
        if delay_resume:
            emit = m.bus.aemit
            held = asyncio.Event()

            async def delayed_emit(session_id, event, data):
                if event == "resumed":
                    # Force the recovery boundary beyond the tolerance, even on CI/coverage. The test releases
                    # it, so the length of the gap is a floor rather than a race against the rest of the run.
                    held.set()
                    await release.wait()
                await emit(session_id, event, data)

            release = asyncio.Event()
            monkeypatch.setattr(m.bus, "aemit", delayed_emit)
        await m.start()
        if delay_resume:
            await asyncio.wait_for(held.wait(), 30)
            await asyncio.sleep(MAX_GAP_NS / 1_000_000_000 + 0.1)
            release.set()
        # Decide only once the resumed turn is parked on the approval, so the span tree never depends on
        # whether the decision landed before or after the wait began.
        while not m.runner.approval_events:
            await asyncio.sleep(0.01)
        m.decide(sid, None, approve=True)
        await wait_status(m, sid, "done")
        await m.stop()
        return exporter.get_finished_spans()

    sid, before = asyncio.run(first())
    after = asyncio.run(second(sid))
    spans = before + after
    tools = [sp for sp in after if sp.name == "execute_tool"]
    by_id = {sp.context.span_id: sp for sp in spans}
    assert tools
    for tool in tools:
        assert by_id[tool.parent.span_id].name == "turn"
    resumed = by_id[tools[0].parent.span_id]
    assert resumed.attributes["harness.resumed"] is True
    for turn in (sp for sp in spans if sp.name == "turn"):
        kids = [sp for sp in spans if sp.parent is not None and sp.parent.span_id == turn.context.span_id]
        _assert_tiled(turn, kids)
    root = next(sp for sp in after if sp.name == "session")
    children = [sp for sp in spans if sp.parent is not None and sp.parent.span_id == root.context.span_id]
    assert not {c.name for c in children} & {"execute_tool", "approval_wait", "sandbox_exec"}
    idle = next(sp for sp in after if sp.name == "idle")
    setup = next(sp for sp in after if sp.name == "run_setup")
    # session_run records idle before recovery emits the async resumed event. Scheduling that event is
    # allowed to take time; it is not model/tool work. Only this exact boundary may exceed the tolerance.
    boundary = (idle, setup)
    _assert_tiled(root, children, resume_boundary=boundary)
    if delay_resume:
        with pytest.raises(AssertionError):
            _assert_tiled(root, children)  # injected delay really exceeded the original tolerance
    for missing in (idle, setup):
        with pytest.raises(AssertionError):
            _assert_tiled(root, [c for c in children if c is not missing], resume_boundary=boundary)


def test_sentinel_never_reaches_a_span(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.elide_at = 0
    cfg.summarize_at = 99
    cfg.reset_at = 99
    script = Script([
        Completion(tool_calls=[call("write_file", 0, path=f"{SENTINEL}.txt", content=f"body {SENTINEL}")]),
        Completion(tool_calls=[call("read_file", 1, path=f"{SENTINEL}.txt")]),
        Completion(tool_calls=[call("read_file", 2, path=f"missing-{SENTINEL}.txt")]),
        Completion(tool_calls=[call("finish", 3, answer=f"answer {SENTINEL}")]),
    ])

    async def body():
        m, exporter = _traced_manager(cfg, script)
        await m.start()
        s = await wait_status(m, m.create(f"prompt {SENTINEL}", title=f"t {SENTINEL}")["id"], "done")
        await m.stop()
        return s, exporter.get_finished_spans()

    s, spans = asyncio.run(body())
    assert SENTINEL in s["answer"]
    assert {sp.name for sp in spans} >= {"session", "turn", "chat", "execute_tool", "sandbox_exec"}
    for sp in spans:
        assert SENTINEL not in sp.name
        for key, value in sp.attributes.items():
            assert key in telemetry.ALLOWED_ATTRIBUTES
            assert SENTINEL not in key and SENTINEL not in str(value)
        assert SENTINEL not in (sp.status.description or "")
        assert not sp.events


def test_trace_url_from_template():
    template = "https://grafana/explore?left={trace_id}"
    assert telemetry.trace_url(template, "ab" * 16) == "https://grafana/explore?left=" + "ab" * 16
    assert telemetry.trace_url(template, "") == ""
    assert telemetry.trace_url("", "ab" * 16) == ""
    assert telemetry.trace_url("https://grafana/explore", "ab" * 16) == ""


def test_summary_returns_trace_id_and_url_when_on(tmp_path):
    cfg = make_cfg(tmp_path)

    async def body():
        m, _ = _traced_manager(cfg, Script([Completion(content="done")]))
        m.cfg.telemetry = TelemetryConfig(otlp_endpoint=ON.otlp_endpoint, trace_url_template="https://g/x?q={trace_id}")
        await m.start()
        s = await wait_status(m, m.create("try")["id"], "done")
        out = m.summary(s)
        await m.stop()
        return out
    out = asyncio.run(body())
    assert len(out["trace_id"]) == 32 and out["trace_url"] == "https://g/x?q=" + out["trace_id"]


def test_ops_observability_files_are_valid_and_documented():
    import json

    import yaml
    ops = ROOT / "ops" / "observability"
    tempo = yaml.safe_load((ops / "tempo.yaml").read_text(encoding="utf-8"))
    assert "otlp" in tempo["distributor"]["receivers"]
    compose = yaml.safe_load((ops / "docker-compose.tempo.yml").read_text(encoding="utf-8"))
    assert compose["services"]["tempo"]["ports"] == ["127.0.0.1:4318:4318"]  # loopback only
    datasource = yaml.safe_load((ops / "grafana-datasource-tempo.yaml").read_text(encoding="utf-8"))
    assert datasource["datasources"][0]["type"] == "tempo" and datasource["datasources"][0]["uid"] == "tempo"
    dashboard = json.loads((ops / "dashboard-agent-harness-traces.json").read_text(encoding="utf-8"))
    assert dashboard["panels"] and all(p["datasource"]["uid"] == "tempo" for p in dashboard["panels"])
    docs = (ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    for name in ("tempo.yaml", "ops/observability", "ALLOWED_ATTRIBUTES", "trace_url_template"):
        assert name in docs or name in (ops / "README.md").read_text(encoding="utf-8")
    for span in telemetry.SPAN_NAMES:
        assert span in docs
    for key in telemetry.ALLOWED_ATTRIBUTES:
        assert key in docs
