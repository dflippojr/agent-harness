"""Event-loop and SQLite-lock timing (#257) and opt-in OpenTelemetry session traces (#259).

`TimedLock` replaces `Database.lock` and records how long each outermost `with db.lock:` block holds the lock,
labelled by the calling method. `LoopLagProbe` records how late a fixed-interval sleep wakes up, which is the
time the event loop was unable to run other callbacks.

Tracing: `configure(cfg.telemetry)` picks the tracer. With `otlp_endpoint` empty (the default), or the
`opentelemetry` packages missing, every call site gets the no-op shim and nothing imports `opentelemetry`. Only
attribute keys in `ALLOWED_ATTRIBUTES` are ever recorded (ids, names, sizes, counts, timings); see
docs/observability.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import os
import secrets
import sys
import threading
import time

log = logging.getLogger(__name__)

LOCK_BUCKETS = (0.0005, 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5)
LOOP_BUCKETS = LOCK_BUCKETS
PROBE_INTERVAL = 0.01
SLOW_CALLBACK_SECONDS = 0.05


class Histogram:
    def __init__(self, buckets: tuple[float, ...] = LOCK_BUCKETS):
        self.buckets = tuple(buckets)
        self._lock = threading.Lock()
        self._counts = [0] * len(self.buckets)
        self._sum = 0.0
        self._count = 0

    def observe(self, value: float) -> None:
        with self._lock:
            self._sum += value
            self._count += 1
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    self._counts[i] += 1
                    break

    def snapshot(self) -> tuple[list[tuple[float, int]], float, int]:
        """Cumulative (le, count) pairs, sum, and count."""
        with self._lock:
            running, cumulative = 0, []
            for bound, n in zip(self.buckets, self._counts):
                running += n
                cumulative.append((bound, running))
            return cumulative, self._sum, self._count

    def quantile(self, q: float) -> float:
        """Upper bound of the bucket holding the q-quantile (inf past the last bucket, 0 when empty)."""
        cumulative, _, count = self.snapshot()
        if not count:
            return 0.0
        target = q * count
        for bound, n in cumulative:
            if n >= target:
                return bound
        return float("inf")


class HistogramFamily:
    """Histograms keyed by one label value."""

    def __init__(self, buckets: tuple[float, ...] = LOCK_BUCKETS):
        self.buckets = buckets
        self._lock = threading.Lock()
        self._by_label: dict[str, Histogram] = {}

    def observe(self, label: str, value: float) -> None:
        h = self._by_label.get(label)
        if h is None:
            with self._lock:
                h = self._by_label.setdefault(label, Histogram(self.buckets))
        h.observe(value)

    def items(self) -> list[tuple[str, Histogram]]:
        with self._lock:
            return sorted(self._by_label.items())


lock_held = HistogramFamily(LOCK_BUCKETS)
loop_stall = Histogram(LOOP_BUCKETS)

# Lock-taking helpers that are not themselves the operation: `Database.tx()` and the contextlib frames that
# drive it. The label skips past them to the method that called `with db.tx():`.
_PASS_THROUGH = frozenset({"tx"})
_CONTEXTLIB = contextlib.__file__


def _caller_method(depth: int = 2) -> str:
    frame = sys._getframe(depth)
    while frame.f_back is not None and (
            frame.f_code.co_name in _PASS_THROUGH or frame.f_code.co_filename == _CONTEXTLIB):
        frame = frame.f_back
    return frame.f_code.co_name


class TimedLock:
    """Re-entrant lock that records the hold time of each outermost acquisition, per calling method."""

    def __init__(self, family: HistogramFamily = lock_held):
        self._lock = threading.RLock()
        self._family = family
        self._owner = threading.local()

    def __enter__(self):
        method = _caller_method()
        self._lock.acquire()
        owner = self._owner
        depth = getattr(owner, "depth", 0)
        if depth == 0:
            owner.method, owner.start = method, time.perf_counter()
        owner.depth = depth + 1
        return self

    def __exit__(self, *exc):
        owner = self._owner
        owner.depth -= 1
        if owner.depth == 0:
            self._family.observe(owner.method, time.perf_counter() - owner.start)
        self._lock.release()
        return False

    def acquire(self, *args, **kwargs):
        return self._lock.acquire(*args, **kwargs)

    def release(self):
        self._lock.release()


class LoopLagProbe:
    """Sleeps PROBE_INTERVAL repeatedly and records how much later than that it woke up."""

    def __init__(self, hist: Histogram = loop_stall, interval: float = PROBE_INTERVAL):
        self.hist, self.interval = hist, interval
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            # wait() never raises the task's CancelledError, but still propagates ours if stop() itself is cancelled
            await asyncio.wait({task})

    async def _run(self) -> None:
        while True:
            before = time.perf_counter()
            await asyncio.sleep(self.interval)
            self.hist.observe(max(0.0, time.perf_counter() - before - self.interval))


def asyncio_debug_requested() -> bool:
    return os.environ.get("HARNESS_ASYNCIO_DEBUG", "").strip().lower() in ("1", "true", "yes")


def enable_asyncio_debug() -> bool:
    """Turn on loop debug with a 50 ms slow-callback threshold when HARNESS_ASYNCIO_DEBUG is set. Call on the loop."""
    if not asyncio_debug_requested():
        return False
    loop = asyncio.get_running_loop()
    loop.set_debug(True)
    loop.slow_callback_duration = SLOW_CALLBACK_SECONDS
    log.info("asyncio debug enabled (slow_callback_duration=%.3fs)", SLOW_CALLBACK_SECONDS)
    return True


# --- OpenTelemetry traces (#259) ---

SPAN_NAMES = frozenset({"session", "idle", "turn", "chat", "execute_tool", "approval_wait", "sandbox_exec",
                        "gpu_slot_wait", "compaction", "image_job", "hosted_cli_turn", "run_setup", "run_end"})
# The privacy allowlist: any other key is dropped. Never prompts, arguments, outputs, paths or error text.
ALLOWED_ATTRIBUTES = frozenset({
    "harness.session_id", "harness.backend", "harness.status", "harness.recovered", "harness.turn",
    "gen_ai.operation.name", "gen_ai.request.model", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
    "harness.cache_read_tokens", "harness.prompt_ms", "harness.decode_ms",
    "gen_ai.tool.name", "gen_ai.tool.call.id", "harness.policy_decision", "harness.parallel", "harness.ok",
    "harness.output_chars", "harness.approval_status", "harness.queue_front",
    "harness.compaction_tier", "harness.tokens_before", "harness.tokens_after",
    "error.type",
})
_MAX_STRING = 200

_current: contextvars.ContextVar = contextvars.ContextVar("harness_span", default=None)
_forced_ids: contextvars.ContextVar = contextvars.ContextVar("harness_forced_ids", default=None)


def clean_attributes(attrs: dict | None) -> dict:
    """Keep allowlisted keys with scalar values; everything else is dropped."""
    out = {}
    for key, value in (attrs or {}).items():
        if key not in ALLOWED_ATTRIBUTES or value is None or not isinstance(value, (bool, int, float, str)):
            continue
        out[key] = value[:_MAX_STRING] if isinstance(value, str) else value
    return out


def _ns(ts: float) -> int:
    return int(ts * 1e9)


class NoopSpan:
    trace_id = ""

    def set(self, attrs: dict) -> None:
        pass

    def fail(self, exc: BaseException) -> None:
        pass

    def end(self, at: float | None = None) -> None:
        pass


NOOP_SPAN = NoopSpan()


class NoopTracer:
    """Tracing off: every helper is a cheap no-op and nothing imports opentelemetry."""
    enabled = False

    def new_trace(self) -> dict:
        return {}

    @contextlib.contextmanager
    def span(self, name: str, attrs: dict | None = None):
        yield NOOP_SPAN

    def start(self, name: str, attrs: dict | None = None) -> NoopSpan:
        return NOOP_SPAN

    @contextlib.contextmanager
    def activate(self, span):
        yield span

    def record(self, name: str, start: float, end: float, attrs: dict | None = None) -> None:
        pass

    @contextlib.contextmanager
    def session_run(self, trace: dict, started_at: float, last_stop: float | None = None,
                    attrs: dict | None = None):
        yield NOOP_SPAN

    def current(self) -> NoopSpan:
        return NOOP_SPAN

    def shutdown(self) -> None:
        pass


class _Span:
    """An OpenTelemetry span behind the allowlist."""

    def __init__(self, span, status_cls) -> None:
        self._span = span
        self._status_cls = status_cls

    @property
    def trace_id(self) -> str:
        return format(self._span.get_span_context().trace_id, "032x")

    def set(self, attrs: dict) -> None:
        cleaned = clean_attributes(attrs)
        if cleaned:
            self._span.set_attributes(cleaned)

    def fail(self, exc: BaseException) -> None:
        """Mark the span failed with the error class only, never the message."""
        from opentelemetry.trace import StatusCode
        self.set({"error.type": type(exc).__name__})
        self._span.set_status(self._status_cls(StatusCode.ERROR))

    def end(self, at: float | None = None) -> None:
        self._span.end(end_time=_ns(at) if at is not None else None)


class OtelTracer:
    """Tracing on. Spans only start under a session (`session_run`); work outside a session is not traced."""
    enabled = True

    def __init__(self, tcfg, processor=None) -> None:
        from opentelemetry import context as otel_context
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
        from opentelemetry.trace import Status

        class _Ids(RandomIdGenerator):
            """The session span reuses the ids persisted on the session, so resumed runs join the same trace."""

            def generate_span_id(self) -> int:
                forced = _forced_ids.get()
                return forced[1] if forced else super().generate_span_id()

            def generate_trace_id(self) -> int:
                forced = _forced_ids.get()
                return forced[0] if forced else super().generate_trace_id()

        self._context = otel_context
        self._trace = otel_trace
        self._status = Status
        self.provider = TracerProvider(resource=Resource.create({"service.name": tcfg.service_name}),
                                       id_generator=_Ids())
        self.provider.add_span_processor(processor or _span_processor(tcfg.otlp_endpoint))
        self.tracer = self.provider.get_tracer("agent-harness")

    def new_trace(self) -> dict:
        return {"trace_id": format(secrets.randbits(128) or 1, "032x"),
                "span_id": format(secrets.randbits(64) or 1, "016x")}

    def _start(self, name: str, parent: _Span, attrs: dict | None, start: float | None = None) -> _Span:
        span = self.tracer.start_span(name, context=self._trace.set_span_in_context(parent._span),
                                      start_time=_ns(start) if start is not None else None)
        wrapped = _Span(span, self._status)
        wrapped.set(attrs or {})
        return wrapped

    @contextlib.contextmanager
    def span(self, name: str, attrs: dict | None = None):
        parent = _current.get()
        if parent is None:
            yield NOOP_SPAN
            return
        span = self._start(name, parent, attrs)
        token = _current.set(span)
        try:
            yield span
        except BaseException as e:
            if not isinstance(e, (GeneratorExit, asyncio.CancelledError)):
                span.fail(e)
            raise
        finally:
            _current.reset(token)
            span.end()

    def start(self, name: str, attrs: dict | None = None):
        parent = _current.get()
        return NOOP_SPAN if parent is None else self._start(name, parent, attrs)

    @contextlib.contextmanager
    def activate(self, span):
        if span is NOOP_SPAN:
            yield span
            return
        token = _current.set(span)
        try:
            yield span
        finally:
            _current.reset(token)

    def record(self, name: str, start: float, end: float, attrs: dict | None = None) -> None:
        parent = _current.get()
        if parent is not None:
            self._start(name, parent, attrs, start=start).end(at=end)

    @contextlib.contextmanager
    def session_run(self, trace: dict, started_at: float, last_stop: float | None = None,
                    attrs: dict | None = None):
        """One run of a session under its persistent root span. The root is re-exported, with the same ids, at the
        end of every run so its end time follows the latest run; `last_stop` adds the `idle` gap before this run."""
        try:
            ids = (int(trace["trace_id"], 16), int(trace["span_id"], 16))
        except (KeyError, TypeError, ValueError):
            yield NOOP_SPAN
            return
        forced = _forced_ids.set(ids)
        try:
            root = _Span(self.tracer.start_span("session", context=self._context.Context(),
                                                start_time=_ns(started_at)), self._status)
        finally:
            _forced_ids.reset(forced)
        root.set(attrs or {})
        token = _current.set(root)
        try:
            if last_stop is not None:
                self.record("idle", last_stop, time.time())
            yield root
        except BaseException as e:
            if not isinstance(e, (GeneratorExit, asyncio.CancelledError)):
                root.fail(e)
            raise
        finally:
            _current.reset(token)
            root.end()

    def current(self):
        return _current.get() or NOOP_SPAN

    def shutdown(self) -> None:
        self.provider.shutdown()


def _span_processor(endpoint: str):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    return BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))


_tracer: NoopTracer | OtelTracer = NoopTracer()


def configure(tcfg, processor=None) -> NoopTracer | OtelTracer:
    """Install the tracer for `tcfg` (a TelemetryConfig). Missing packages log one warning and leave tracing off."""
    global _tracer
    _tracer.shutdown()
    _tracer = NoopTracer()
    endpoint = (getattr(tcfg, "otlp_endpoint", "") or "").strip()
    if not endpoint:
        return _tracer
    try:
        _tracer = OtelTracer(tcfg, processor=processor)
    except ImportError as e:
        log.warning("telemetry.otlp_endpoint is set but OpenTelemetry is not installed (%s); tracing is off. "
                    "Install requirements-telemetry.txt to enable it.", type(e).__name__)
    return _tracer


def tracer() -> NoopTracer | OtelTracer:
    return _tracer


def span(name: str, attrs: dict | None = None):
    """Context manager: a child of the current span (a no-op outside a traced session)."""
    return _tracer.span(name, attrs)


def annotate(attrs: dict) -> None:
    """Add allowlisted attributes to the current span."""
    _tracer.current().set(attrs)


def tool_span(name: str, call_id: str = "", parallel: bool = False):
    """The `execute_tool` span for one tool call; shared by every tool source (built-in, app, MCP #260).
    `parallel` marks calls that overlap their siblings under the same `turn`."""
    attrs = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "gen_ai.tool.call.id": call_id}
    if parallel:
        attrs["harness.parallel"] = True
    return _tracer.span("execute_tool", attrs)


def trace_url(template: str, trace_id: str) -> str:
    """The Info tab link: `template` with {trace_id} filled in, or "" without a template or id."""
    if not template or not trace_id or "{trace_id}" not in template:
        return ""
    return template.replace("{trace_id}", trace_id)
