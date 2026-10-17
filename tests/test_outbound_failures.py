"""Every outbound integration reports an upstream failure honestly (#733).

Each tests/outbound_registry.py entry runs against a local stub that refuses the connection, never answers, answers
4xx or 5xx, or answers 200 with a malformed or truncated body. In each case the caller must report the failure (no
`ok/sent/success: true`, the right status on the API), give up after a bounded number of attempts, and keep the
secret it sent out of the error it returns, the log, its printed output and the database.

test_every_outbound_call_site_is_registered keeps the registry complete: a new client in harness/, harness_modules/
or sdk/ fails it until the new site has an entry here.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import secrets
import threading
import time

import outbound_stub
import pytest
from outbound_registry import REGISTRY, Ctx, Outcome
from outbound_scan import scan

CASE_SECONDS = 15  # a call still running after this is waiting or retrying without end

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@pytest.fixture(scope="module")
def stub():
    server = outbound_stub.Stub()
    yield server
    server.close()


def _run(entry, ctx: Ctx) -> tuple[Outcome, float]:
    """Call the entry in a thread, so a call that never returns fails this test instead of hanging the suite."""
    box: dict = {}

    def call():
        try:
            value = entry.invoke(ctx)
            box["outcome"] = Outcome(value=asyncio.run(value) if inspect.iscoroutine(value) else value)
        except BaseException as e:  # noqa: BLE001 - SystemExit and CancelledError are outcomes here too
            box["outcome"] = Outcome(error=e)

    started = time.monotonic()
    worker = threading.Thread(target=call, name=f"outbound-{entry.site}", daemon=True)
    worker.start()
    worker.join(CASE_SECONDS)
    assert not worker.is_alive(), f"{entry.site} was still running after {CASE_SECONDS} s ({ctx.mode})"
    return box["outcome"], time.monotonic() - started


@pytest.mark.parametrize("mode", outbound_stub.MODES)
@pytest.mark.parametrize("entry", REGISTRY, ids=[e.site for e in REGISTRY])
def test_outbound_failure_is_reported(entry, mode, stub, tmp_path, monkeypatch, caplog, capsys, request):
    if mode in entry.xfail:  # strict: once the linked fix lands the case passes, and that fails until the mark goes
        request.applymarker(pytest.mark.xfail(strict=True, reason=entry.xfail[mode]))
    if entry.site == "harness/telemetry.py::_span_processor":
        pytest.importorskip("opentelemetry.exporter.otlp.proto.http")
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    caplog.set_level(logging.INFO)
    stub.reset(mode)
    rerouted = outbound_stub.redirect(monkeypatch, stub)
    secret = f"sk-outbound-{secrets.token_hex(12)}"
    ctx = Ctx(mode=mode, stub=stub, tmp=tmp_path, monkeypatch=monkeypatch, secret=secret, caplog=caplog,
              port=outbound_stub.closed_port() if mode == "refuse" else stub.port)

    outcome, seconds = _run(entry, ctx)
    printed = capsys.readouterr()

    if isinstance(outcome.error, AssertionError):
        raise outcome.error
    if mode not in entry.ok:
        assert entry.reported(outcome), f"{entry.what} did not report the {mode} failure: {outcome.text()[:600]}"
    attempts = max(rerouted.count, stub.hits)
    assert attempts <= entry.attempts, f"{entry.what} tried {attempts} times ({mode}); the bound is {entry.attempts}"
    assert bool(rerouted.unbounded) <= bool(entry.unbounded), f"no read timeout on {rerouted.unbounded}"
    if entry.secret and mode != "refuse":
        assert stub.received(secret), "the call never sent its secret, so the leak checks below prove nothing"
    shown = {"the returned value or error": outcome.text(), "the log": caplog.text,
             "the printed output": printed.out + printed.err,
             **{f"stored text {i}": store() for i, store in enumerate(ctx.stores)}}
    for value in [secret, *ctx.secrets]:
        for where, text in shown.items():
            assert value not in text, f"{entry.what} leaked a secret into {where} ({mode})"
    assert seconds < CASE_SECONDS


def test_every_outbound_call_site_is_registered():
    """The registry and the scan of the source name the same call sites, so their counts are equal."""
    found = scan()
    registered = [entry.site for entry in REGISTRY]
    assert len(registered) == len(set(registered)), "a site is registered twice"
    missing = sorted(set(found) - set(registered))
    stale = sorted(set(registered) - set(found))
    assert not missing, ("outbound call sites without failure-path coverage; add each to tests/outbound_registry.py:"
                         + "".join(f"\n  {site} (line {', '.join(map(str, found[site]))})" for site in missing))
    assert not stale, "registered sites the scan no longer finds (renamed or removed?): " + ", ".join(stale)
    assert len(registered) == len(found)
    print(f"outbound call sites: {len(found)} found by the scan, {len(registered)} registered")


def test_the_scan_finds_each_kind_of_client(tmp_path):
    """The scan recognises aliased imports and every client family, and names nested scopes."""
    package = tmp_path / "harness"
    package.mkdir()
    (package / "clients.py").write_text(
        "import httpx as h\nimport urllib.request\nfrom socket import create_connection\n"
        "from requests import Session\nimport asyncio\n\n"
        "def a():\n    return h.get('x')\n\n"
        "class B:\n    async def c(self):\n        async with h.AsyncClient() as client:\n            pass\n"
        "    def d(self):\n        def inner():\n            urllib.request.urlopen('x')\n        return inner\n\n"
        "def e():\n    create_connection(('h', 1))\n    Session()\n\n"
        "async def f():\n    await asyncio.open_connection('h', 1)\n\n"
        "def not_network(client: h.Client):\n    return client.get('x')\n", encoding="utf-8")
    for other in ("harness_modules", "sdk"):
        (tmp_path / other).mkdir()
    assert sorted(scan(tmp_path)) == ["harness/clients.py::B.c", "harness/clients.py::B.d.inner",
                                      "harness/clients.py::a", "harness/clients.py::e", "harness/clients.py::f"]
