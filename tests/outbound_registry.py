"""Every outbound call site in the shipped code, with how to make it fail (#733).

A site is `path::Qualified.name`, the key tests/outbound_scan.py finds by reading the source. Each entry drives the
site, or the public caller that owns its failure handling, against tests/outbound_stub.py, and says how that caller
reports a failure. tests/test_outbound_failures.py runs every entry in every mode of outbound_stub.MODES and fails
when the scan finds a site that has no entry here.

An entry's `invoke(ctx)` may return a coroutine. Its `reported(outcome)` says whether the caller told the truth:
raised, returned a failure value, or answered with an error status. `ok` names the modes in which success is the
honest answer and why; `attempts` is how many requests a single call may make; `secret` says the call carries
`ctx.secret` upstream, so the test also proves the secret reached the stub and appears nowhere it shouldn't.
"""

from __future__ import annotations

import asyncio
import json
import time
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from harness import fake_instance, secret_scan
from harness.config import (
    GpuGuardConfig,
    HomelabConfig,
    ImagesConfig,
    ModelConfig,
    SkillsConfig,
    WebConfig,
)
from harness_modules.local_model.warmup import ModelWarmer

# The conftest replaces these for every test; the registry exercises the real ones.
_REAL_SECRET_DOWNLOAD = secret_scan._download
_REAL_WARMER_STATE = ModelWarmer.state


@dataclass
class Outcome:
    value: Any = None
    error: BaseException | None = None

    def text(self) -> str:
        """Everything the caller hands back: the value and the whole exception chain."""
        parts = [repr(self.value)]
        error, seen = self.error, set()
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            parts += [type(error).__name__, str(error), repr(error), repr(getattr(error, "args", ""))]
            error = error.__cause__ or error.__context__
        return "\n".join(parts)


@dataclass
class Ctx:
    """What an entry gets: the stub's address for the mode, a secret to send, and places to read back."""
    mode: str
    stub: Any
    tmp: Path
    monkeypatch: Any
    secret: str
    port: int                       # the stub's port, or a closed one in the refuse mode
    caplog: Any = None
    secrets: list[str] = field(default_factory=list)   # more values that must not leak (keys minted mid-call)
    stores: list[Callable[[], str]] = field(default_factory=list)  # audit rows and other stored text to check

    @property
    def url(self) -> str:
        """The stub as a base URL, for clients outbound_stub.redirect can't reroute (urllib, requests, sockets)."""
        return f"http://127.0.0.1:{self.port}"

    def file(self, name: str, text: str) -> Path:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return path

    def quick_sleeps(self, *modules) -> None:
        """Retry waits in `modules` take 10 ms, so a bounded retry finishes inside the test's time limit."""
        async def asleep(delay, result=None):
            await asyncio.sleep(min(delay, 0.01))
            return result

        for module in modules:
            if hasattr(module, "asyncio"):
                self.monkeypatch.setattr(module, "asyncio", _with(module.asyncio, sleep=asleep))
            if hasattr(module, "time"):
                self.monkeypatch.setattr(module, "time", _with(module.time, sleep=lambda d: time.sleep(min(d, 0.01))))


def _with(module, **overrides):
    """A stand-in for `module` with some attributes replaced, so a patch reaches only the module that uses it."""
    return types.SimpleNamespace(**{**vars(module), **overrides})


@dataclass(frozen=True)
class Outbound:
    site: str
    what: str
    invoke: Callable[[Ctx], Any]
    reported: Callable[[Outcome], bool] = lambda o: o.error is not None
    ok: dict[str, str] = field(default_factory=dict)
    attempts: int = 1
    secret: bool = False
    unbounded: str = ""             # why the call may wait with no read timeout (a long-lived event stream)
    xfail: dict[str, str] = field(default_factory=dict)  # mode -> filed issue, for a defect not fixed here


# ---- reading outcomes --------------------------------------------------------------------------------------

def raised(*kinds: type[BaseException]) -> Callable[[Outcome], bool]:
    return lambda o: o.error is not None and (not kinds or isinstance(o.error, kinds))


def returned(*values) -> Callable[[Outcome], bool]:
    return lambda o: o.error is None and o.value in values


def quiet(o: Outcome) -> bool:
    """A best-effort call: it must come back without raising and without claiming anything."""
    return o.error is None


def status(*codes: int) -> Callable[[Outcome], bool]:
    return lambda o: o.error is None and o.value.status_code in codes


class Report:
    """doctor's Report, recording each line instead of printing it."""

    def __init__(self):
        self.lines: list[tuple[str, str, str]] = []
        self.not_started = False

    def ok(self, name, detail=""):
        self.lines.append(("ok", name, detail))

    def warn(self, name, detail):
        self.lines.append(("warn", name, detail))

    def fail(self, name, detail):
        self.lines.append(("fail", name, detail))

    def __repr__(self):
        return repr(self.lines)


def flagged(check: str) -> Callable[[Outcome], bool]:
    """The doctor reported `check` as a warning or failure, never as OK."""
    def read(o: Outcome) -> bool:
        kinds = [kind for kind, name, _ in o.value.lines if name == check] if o.error is None else []
        return bool(kinds) and "ok" not in kinds
    return read


def not_ok(o: Outcome) -> bool:
    return o.error is None and not any(kind == "ok" for kind, _, _ in o.value.lines)


MODEL = ModelConfig(name="stub-model", base_url="http://llama.example:8080")
BODY_UNREAD = "the call reads only the status line; a 2xx is the upstream's acceptance"
VERIFIED = "the bytes are returned unparsed and the caller checks their pinned SHA-256 before use"
LISTENING = "a listening port is what the probe reports"


# ---- harness/ -------------------------------------------------------------------------------------------

def cli_pair(ctx: Ctx):
    from harness import cli
    client, runner = ctx.tmp / "client.json", ctx.tmp / "runner.json"
    try:
        return cli.pair_native("https://tower.example", ctx.secret, client, runner)
    finally:
        assert not client.exists() and not runner.exists(), "a failed pairing wrote credentials"


def cli_api(ctx: Ctx):
    from harness import cli
    ctx.monkeypatch.setattr(cli, "BASE", "https://tower.example")
    ctx.monkeypatch.setattr(cli, "TOKEN", ctx.secret)
    ctx.quick_sleeps(cli)
    return cli.api("GET", "/me", retries=2)


def cli_server_version(ctx: Ctx):
    from harness import cli
    ctx.monkeypatch.setattr(cli, "BASE", "https://tower.example")
    return cli.server_version()


def cli_watch(ctx: Ctx):
    from harness import cli
    ctx.monkeypatch.setattr(cli, "BASE", "https://tower.example")
    ctx.monkeypatch.setattr(cli, "TOKEN", ctx.secret)
    ctx.monkeypatch.setattr(cli, "WATCH_RECONNECTS", 3)
    ctx.quick_sleeps(cli)
    return cli.watch("s1", SimpleNamespace(reasoning=False, full=False, no_prompt=True))


def doctor_get(ctx: Ctx):
    from harness import doctor, local_owner
    ctx.file(local_owner.TOKEN_FILE, ctx.secret)
    return doctor._get(SimpleNamespace(data_dir=ctx.tmp), "http://127.0.0.1:8100/health", timeout=5)


def doctor_optional(ctx: Ctx):
    from harness import doctor, modules
    ctx.monkeypatch.setattr(modules, "present", lambda cfg: [])
    ctx.monkeypatch.setattr(doctor, "run", lambda args, timeout=20: (1, ""))
    report = Report()
    doctor.check_optional(report, SimpleNamespace(web=WebConfig(enabled=True, searxng_url="http://searxng.example"),
                                                  port=8100))
    return report


def egress_proxy_forward(ctx: Ctx):
    """A plain HTTP request through the sandbox egress proxy (#671), which owns the failure: the client gets a 502,
    the upstream's own answer, or a closed connection. The resolver names a public address so the policy passes,
    and the connector sends that address to the stub."""
    from harness import egress_proxy as ep

    async def resolve(host, port):
        return ["8.8.8.8"]

    async def connect(address, port):
        return await ep.system_connect("127.0.0.1", ctx.port)

    async def go():
        proxy = ep.EgressProxy(ep.Policy(connect_timeout=5, idle_timeout=2), resolver=resolve, connector=connect)
        server = await proxy.serve("127.0.0.1", 0)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            writer.write(f"GET http://pkg.example/simple/ HTTP/1.1\r\nHost: pkg.example\r\n"
                         f"Authorization: Bearer {ctx.secret}\r\n\r\n".encode())
            data = await asyncio.wait_for(reader.read(), 10)
            writer.close()
            return data.split(b"\r\n", 1)[0].decode("latin-1")
        finally:
            server.close()
            await server.wait_closed()
    return go()


# One POST per seeded session, chat and job, none retried.
SEED_REQUESTS = len(fake_instance.SEED_SESSIONS) + len(fake_instance.SEED_CHATS) + len(fake_instance.SEED_JOBS)


async def fake_instance_seed(ctx: Ctx):
    from harness import fake_instance
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8100", timeout=30, trust_env=False,
                                 headers={"Authorization": f"Bearer {ctx.secret}"}) as client:
        problems = await fake_instance.seed(client)
    return problems


def gcm_relay(ctx: Ctx):
    from harness import gcm_ui_helper
    ctx.monkeypatch.setenv("AGENT_HARNESS_GCM_RELAY", f"127.0.0.1:{ctx.port}")
    ctx.monkeypatch.setenv("AGENT_HARNESS_GCM_NONCE", ctx.secret)
    ctx.monkeypatch.setattr(gcm_ui_helper, "DEADLINE_SECONDS", 2)
    return gcm_ui_helper._serve(["device", "--code", "ABCD-1234", "--url", "https://github.com/login/device"])


def github_items(ctx: Ctx):
    from harness import github_tasks
    token = ctx.file("github-token", ctx.secret)
    github_tasks._CACHE.clear()
    return github_tasks.list_items(SimpleNamespace(data_dir=ctx.tmp, github=SimpleNamespace(token_file=str(token))),
                                   "owner/repo")


def google_discovery(ctx: Ctx):
    from harness.google_signin import GoogleOidc
    oidc = GoogleOidc()
    try:
        return oidc.discovery()
    finally:
        oidc.http().close()


def install_hub_probe(ctx: Ctx):
    from harness import install_hub
    return install_hub.daemon_available(ctx.port)


async def llm_chat(ctx: Ctx):
    from harness import llm
    return await llm.chat(MODEL, [{"role": "user", "content": "hi"}], timeout=30)


def member_key_probe(ctx: Ctx):
    from harness import member_keys
    return member_keys._probe("claude", ctx.secret)


async def runner_model_reachable(ctx: Ctx):
    from harness.runner import Runner
    return await Runner._model_reachable(MODEL)


def secret_scan_download(ctx: Ctx):
    return _REAL_SECRET_DOWNLOAD("https://github.com/gitleaks/gitleaks/releases/download/v8/gitleaks.tar.gz")


async def smart_review(ctx: Ctx):
    from harness import smart_approvals
    return await smart_approvals.hosted_complete(smart_approvals.SmartConfig(enabled=True, timeout_seconds=5),
                                                 ctx.secret, {"tool": "run", "args": {"cmd": "ls"}})


def otlp_export(ctx: Ctx):
    from opentelemetry.sdk.trace import TracerProvider

    from harness import telemetry
    ctx.monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "1")
    processor = telemetry._span_processor(f"{ctx.url}/v1/traces")
    span = TracerProvider().get_tracer("outbound").start_span("probe")
    span.end()
    try:
        return processor.span_exporter.export([span])
    finally:
        processor.shutdown()


def otlp_failed(o: Outcome) -> bool:
    from opentelemetry.sdk.trace.export import SpanExportResult
    return o.error is None and o.value == SpanExportResult.FAILURE


def updater_download(ctx: Ctx):
    from harness import updater
    real = updater.urllib.request.urlopen

    def urlopen(request, timeout=None):  # the timeout mode needs a short wait; the call is otherwise unchanged
        return real(request, timeout=min(timeout or 1, 0.5))
    ctx.monkeypatch.setattr(updater.urllib.request, "urlopen", urlopen)
    return updater._download(f"{ctx.url}/agent-harness-cli.tar.gz")


async def web_fixture_record(ctx: Ctx):
    from harness import web_fixture
    return await web_fixture.record(ctx.tmp / "fixture", ["query"], [], 0, "http://searxng.example:8888")


async def web_search(ctx: Ctx):
    from harness.web_tools import WebTools
    return await WebTools(WebConfig(enabled=True, searxng_url="http://searxng.example:8888")).web_search("query")


# ---- harness_modules/ -----------------------------------------------------------------------------------

def restore_probe(ctx: Ctx):
    from harness_modules.backup import restore
    return restore.daemon_running(SimpleNamespace(port=8100, db_path=ctx.tmp / "harness.db", data_dir=ctx.tmp))


def backup_doctor(ctx: Ctx):
    from harness_modules.backup import runtime
    report = Report()
    runtime.doctor(report, SimpleNamespace(port=8100))
    return report


def endpoint_manager(ctx: Ctx):
    from test_daemon import Completion, Script, make_cfg

    from harness.manager import Manager
    cfg = make_cfg(ctx.tmp)
    cfg.runners = {}
    for section in (cfg.memory_library, cfg.remote_control, cfg.notify, cfg.gpu_guard):
        section.enabled = False
    cfg.endpoint.enabled = True
    m = Manager(cfg, chat=Script([Completion(content="unused")]))
    ctx.stores.append(lambda: _dump(m.db.conn))
    return m


def endpoint_proxy(ctx: Ctx):
    from fastapi.testclient import TestClient

    from harness.api import create_app
    m = endpoint_manager(ctx)
    with TestClient(create_app(m)) as client:
        key = client.post("/keys", json={"name": "outbound"}).json()["key"]
        ctx.secrets.append(key)
        response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                               json={"model": "fake", "messages": [{"role": "user", "content": "hi"}]})
        logged = [row[0] for row in m.db.conn.execute("SELECT status FROM endpoint_requests").fetchall()]
    assert logged == [response.status_code], f"the endpoint log says {logged}, the caller got {response.status_code}"
    return response


def homelab_query(ctx: Ctx):
    from harness_modules.homelab.service import Homelab
    return Homelab(HomelabConfig(prometheus_url="http://prometheus.example:9090")).prometheus_query("up")


def image_download(ctx: Ctx):
    from harness_modules.images import models
    dest = ctx.tmp / "models" / "flux.safetensors"
    try:
        return models.download_file("https://huggingface.co/org/repo/resolve/main/flux.safetensors", dest,
                                    "0" * 64, 4096)
    finally:
        assert not dest.exists(), "a failed download was promoted"


def comfy(ctx: Ctx):
    from harness_modules.images.service import ComfyProcess
    return ComfyProcess(ImagesConfig(port=8188))


async def comfy_ready(ctx: Ctx):
    return await comfy(ctx).ready()


async def comfy_stop(ctx: Ctx):
    return await comfy(ctx).stop()


async def comfy_interrupt(ctx: Ctx):
    return await comfy(ctx).interrupt()


def image_service(ctx: Ctx, port: int = 8188):
    from harness_modules.images.service import ComfyProcess, ImageService
    cfg = ImagesConfig(port=port, job_timeout_seconds=1)
    service = ImageService.__new__(ImageService)  # the ComfyUI client half only: no queue, GPU guard or archive
    service.cfg, service.comfy, service.transport, service._cancel, service.progress = (
        cfg, ComfyProcess(cfg), None, set(), {})
    return service


async def comfy_png(ctx: Ctx):
    return await image_service(ctx)._comfy_png("job-1", {"1": {"class_type": "KSampler"}}, time.time(), "generating")


async def comfy_progress(ctx: Ctx):
    service, applied = image_service(ctx, port=ctx.port), []
    service.apply_comfy_progress = lambda *args: applied.append(args)
    await service._listen_progress("job-1", time.time(), asyncio.Event())
    return applied


def guard_doctor(ctx: Ctx):
    from harness_modules.local_model import doctor
    report = Report()
    doctor.check_guard(report, SimpleNamespace(port=8100, gpu_guard=GpuGuardConfig(pause_flag=str(ctx.tmp / "p"))))
    return report


def model_doctor(ctx: Ctx):
    from harness_modules.local_model import doctor
    report = Report()
    doctor.check_model_server(report, SimpleNamespace(
        modules=SimpleNamespace(local_model=True), models={MODEL.name: MODEL}, default_model=MODEL.name,
        gpu_guard=GpuGuardConfig(enabled=False)))
    return report


async def plex(ctx: Ctx):
    from harness_modules.local_model import service
    ctx.monkeypatch.setattr(service, "plex_token", lambda: ctx.secret)
    detector = service.Detector(GpuGuardConfig(plex_url="http://plex.example:32400"))
    return await detector._plex(), detector.plex_error


async def server_healthy(ctx: Ctx):
    from harness_modules.local_model.service import ServerControl
    return await ServerControl(GpuGuardConfig(pause_flag=str(ctx.tmp / "paused")), MODEL).healthy()


async def warmer_ping(ctx: Ctx):
    await ModelWarmer()._ping(MODEL, "warmed")
    return [r.getMessage() for r in ctx.caplog.records if r.levelname == "WARNING" and MODEL.name in r.getMessage()]


async def warmer_state(ctx: Ctx):
    from harness.modules import UNREACHABLE
    state = await _REAL_WARMER_STATE(ModelWarmer(), MODEL)
    return state == UNREACHABLE, state


def ntfy_cfg(ctx: Ctx):
    token = ctx.file("ntfy-token", ctx.secret)
    return SimpleNamespace(notify=SimpleNamespace(enabled=True, server="https://ntfy.example/", topic="t",
                                                  token_file=str(token)), public_url="")


class _OneEvent(asyncio.Queue):
    """The notifier's queue holding one notification; the second get ends the loop."""

    def __init__(self, item):
        super().__init__()
        self.put_nowait(item)

    async def get(self):
        if self.empty():
            raise asyncio.CancelledError
        return await super().get()


async def notifier_loop(ctx: Ctx):
    from harness_modules.notifications import service
    ctx.quick_sleeps(service)
    notifier = service.Notifier(ntfy_cfg(ctx), db=None)
    notifier.queue = _OneEvent({"payload": {"topic": "t", "title": "probe", "message": "hello"}})
    try:
        await notifier._run()
    except asyncio.CancelledError:
        pass
    return [r.getMessage() for r in ctx.caplog.records if r.name == service.log.name]


def undelivered(o: Outcome) -> bool:
    return o.error is None and any("not delivered" in line for line in o.value)


def notify_test_route(ctx: Ctx):
    from fastapi.testclient import TestClient
    from test_daemon import Completion, Script, make_cfg

    from harness.api import create_app
    from harness.manager import Manager
    from harness_modules.notifications import service
    ctx.quick_sleeps(service)
    cfg = make_cfg(ctx.tmp)
    m = Manager(cfg, chat=Script([Completion(content="unused")]))
    ntfy = ntfy_cfg(ctx).notify
    cfg.notify.enabled, cfg.notify.server, cfg.notify.topic, cfg.notify.token_file = (
        True, ntfy.server, ntfy.topic, ntfy.token_file)
    ctx.stores.append(lambda: _dump(m.db.conn))
    with TestClient(create_app(m)) as client:
        return client.post("/notify/test")


def canary_alert_test_route(ctx: Ctx):
    """POST /canary/alert-test sends its sample through Notifier.deliver (#732)."""
    from fastapi.testclient import TestClient
    from test_daemon import Completion, Script, make_cfg

    from harness.api import create_app
    from harness.manager import Manager
    from harness_modules.notifications import service
    ctx.quick_sleeps(service)
    cfg = make_cfg(ctx.tmp)
    m = Manager(cfg, chat=Script([Completion(content="unused")]))
    ntfy = ntfy_cfg(ctx).notify
    cfg.notify.enabled, cfg.notify.server, cfg.notify.topic, cfg.notify.token_file = (
        True, ntfy.server, ntfy.topic, ntfy.token_file)
    ctx.stores.append(lambda: _dump(m.db.conn))
    with TestClient(create_app(m)) as client:
        return client.post("/canary/alert-test", json={"kind": "silence"})

async def skills_review(ctx: Ctx):
    from harness_modules.skills.runtime import SkillsRuntime
    key = ctx.file("reviewer-key", ctx.secret)
    owner = SimpleNamespace(cfg=SimpleNamespace(skills=SkillsConfig(
        reviewer_base_url="https://reviewer.example", reviewer_model="m", reviewer_api_key_file=str(key))))
    return await SkillsRuntime.hosted_review(owner, [{"role": "user", "content": "review"}])


# ---- sdk/ -----------------------------------------------------------------------------------------------

BASE = "https://tower.example"


def sdk_client(ctx: Ctx):
    from sdk.harness_client import Harness
    with Harness(BASE, token=ctx.secret, timeout=5) as harness:
        return harness.info()


def sdk_pair(ctx: Ctx):
    from sdk.harness_client import Harness
    return Harness.pair(BASE, ctx.secret, "https://app.example", timeout=5)


def sdk_redeem(ctx: Ctx):
    from sdk.harness_client import Harness, PairingRequest
    pairing = PairingRequest(BASE, "req-1", "pending", "1234", "2026-01-01T00:00:00Z", "", ctx.secret)
    return Harness.redeem_pairing(pairing, wait=1, poll_interval=0.01, timeout=5)


def sdk_request_pairing(ctx: Ctx):
    from sdk.harness_client import Harness
    return Harness.request_pairing(BASE, "outbound", timeout=5)


def _dump(conn) -> str:
    """Every row of every table, as text: an audit row, log row or stored error must not hold a secret."""
    out = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        out += [json.dumps(row, default=str) for row in conn.execute(f'SELECT * FROM "{table}"').fetchall()]
    return "\n".join(out)


REGISTRY: list[Outbound] = [
    Outbound("harness/cli.py::pair_native", "CLI: redeem a runner pairing code", cli_pair, secret=True),
    Outbound("harness/cli.py::api", "CLI: owner API request", cli_api, raised(SystemExit, ValueError), attempts=3,
             secret=True),
    Outbound("harness/cli.py::server_version", "CLI: daemon /health", cli_server_version,
             raised(RuntimeError, ValueError)),
    Outbound("harness/cli.py::_iter_sse", "CLI: `harness watch` event stream", cli_watch, raised(SystemExit),
             attempts=3, secret=True,
             unbounded="an event stream waits between events; the daemon sends keepalives while a run is quiet"),
    Outbound("harness/doctor.py::_get", "doctor: daemon status as the local owner", doctor_get, secret=True),
    Outbound("harness/doctor.py::check_optional", "doctor: SearXNG web search", doctor_optional,
             flagged("Web search")),
    Outbound("harness/egress_proxy.py::system_connect", "sandbox egress proxy: forward to the upstream",
             egress_proxy_forward, lambda o: o.error is None and not o.value.startswith("HTTP/1.1 2"),
             ok={m: "a transparent relay: the upstream's 200 and its bytes reach the client as sent"
                 for m in ("malformed", "truncated")}, secret=True),
    Outbound("harness/fake_instance.py::serve", "fake instance: seed the API", fake_instance_seed,
             lambda o: o.error is not None or bool(o.value), attempts=SEED_REQUESTS, secret=True),
    Outbound("harness/gcm_ui_helper.py::_serve", "GCM helper: relay a device code to the daemon", gcm_relay, quiet,
             secret=True),
    Outbound("harness/github_tasks.py::_get", "GitHub API: issue and PR picker", github_items,
             lambda o: getattr(o.error, "status", 200) in (403, 502, 503), secret=True),
    Outbound("harness/google_signin.py::GoogleOidc.http", "Google sign-in: OpenID discovery", google_discovery,
             lambda o: getattr(o.error, "status", 200) == 503),
    Outbound("harness/install_hub.py::daemon_available", "installer: is the daemon listening", install_hub_probe,
             returned(False), ok={m: LISTENING for m in ("timeout", "4xx", "5xx", "malformed", "truncated")}),
    Outbound("harness/llm.py::chat", "local model server: chat completion", llm_chat, raised(Exception)),
    Outbound("harness/member_keys.py::_probe", "member key check: provider /v1/models", member_key_probe,
             lambda o: o.error is None and o.value[0] is not True, ok={"malformed": BODY_UNREAD}, secret=True),
    Outbound("harness/runner.py::Runner._model_reachable", "runner: model /health", runner_model_reachable,
             returned(False), ok={"malformed": BODY_UNREAD}),
    Outbound("harness/secret_scan.py::_download", "secret scanner: fetch the pinned gitleaks", secret_scan_download,
             ok={"malformed": VERIFIED}),
    Outbound("harness/smart_approvals.py::hosted_complete", "smart approvals: hosted reviewer", smart_review,
             secret=True),
    Outbound("harness/telemetry.py::_span_processor", "telemetry: OTLP trace export", otlp_export, otlp_failed,
             ok={"malformed": BODY_UNREAD}, attempts=6),
    Outbound("harness/updater.py::_download", "updater: fetch a CLI release", updater_download,
             ok={"malformed": VERIFIED}),
    Outbound("harness/web_fixture.py::record", "web fixture recorder: SearXNG", web_fixture_record),
    Outbound("harness/web_tools.py::WebTools._client", "web search tool: SearXNG", web_search),
    Outbound("harness_modules/backup/restore.py::daemon_running", "restore: is the daemon running", restore_probe,
             returned(""), ok={m: LISTENING for m in ("4xx", "5xx", "malformed")}),
    Outbound("harness_modules/backup/runtime.py::doctor", "doctor: backup status", backup_doctor, not_ok),
    Outbound("harness_modules/endpoint/service.py::proxy", "inference endpoint: relay to llama-server",
             endpoint_proxy, lambda o: o.error is None and o.value.status_code in (403, 502, 503),
             ok={"malformed": "a transparent relay: the model server's 200 and its bytes reach the caller as sent"}),
    Outbound("harness_modules/homelab/service.py::Homelab.prometheus_query", "homelab: Prometheus query",
             homelab_query),
    Outbound("harness_modules/images/models.py::download_file", "image models: download weights", image_download),
    Outbound("harness_modules/images/service.py::ComfyProcess.interrupt", "ComfyUI: interrupt", comfy_interrupt,
             quiet),
    Outbound("harness_modules/images/service.py::ComfyProcess.ready", "ComfyUI: /system_stats", comfy_ready,
             returned(False), ok={"malformed": BODY_UNREAD}),
    Outbound("harness_modules/images/service.py::ComfyProcess.stop", "ComfyUI: free memory before stopping",
             comfy_stop, quiet),
    Outbound("harness_modules/images/service.py::ImageService._comfy_png", "ComfyUI: queue a workflow", comfy_png),
    Outbound("harness_modules/images/service.py::ImageService._listen_progress", "ComfyUI: progress websocket",
             comfy_progress, returned([])),
    Outbound("harness_modules/local_model/doctor.py::check_guard", "doctor: resource guard /gpu", guard_doctor,
             flagged("Resource guard")),
    Outbound("harness_modules/local_model/doctor.py::check_model_server", "doctor: llama-server /props",
             model_doctor, flagged("Model server")),
    Outbound("harness_modules/local_model/service.py::Detector._plex", "GPU guard: Plex transcodes", plex,
             lambda o: o.error is None and o.value[0] == [] and bool(o.value[1]), secret=True),
    Outbound("harness_modules/local_model/service.py::ServerControl.healthy", "GPU guard: llama-server /health",
             server_healthy, returned(False), ok={"malformed": BODY_UNREAD}),
    Outbound("harness_modules/local_model/warmup.py::ModelWarmer._ping", "warm-up: keep the model loaded",
             warmer_ping, lambda o: o.error is None and bool(o.value),
             ok={"malformed": "any 200 resets llama-server's idle timer; the body is not read"}),
    Outbound("harness_modules/local_model/warmup.py::ModelWarmer.state", "warm-up: llama-server /props",
             warmer_state, lambda o: o.error is None and o.value[0]),
    Outbound("harness_modules/notifications/routes.py::notify_test", "push notifications: POST /notify/test",
             notify_test_route, status(502), ok={"malformed": BODY_UNREAD}, attempts=3, secret=True),
    Outbound("harness_modules/notifications/service.py::Notifier._run", "push notifications: delivery loop",
             notifier_loop, undelivered, ok={"malformed": BODY_UNREAD}, attempts=3, secret=True),
    Outbound("harness_modules/notifications/service.py::Notifier.deliver", "canary: POST /canary/alert-test",
             canary_alert_test_route, status(502), ok={"malformed": BODY_UNREAD}, attempts=3, secret=True),
    Outbound("harness_modules/skills/runtime.py::SkillsRuntime.hosted_review", "skills: hosted reviewer",
             skills_review, secret=True),
    Outbound("sdk/harness_client.py::Harness.__init__", "SDK: API request", sdk_client, secret=True),
    Outbound("sdk/harness_client.py::Harness._pairing_post", "SDK: redeem a pairing request", sdk_redeem,
             secret=True),
    Outbound("sdk/harness_client.py::Harness._require_feature", "SDK: feature check before pairing",
             sdk_request_pairing, attempts=2),
    Outbound("sdk/harness_client.py::Harness.pair", "SDK: redeem a browser pairing code", sdk_pair, secret=True),
]
