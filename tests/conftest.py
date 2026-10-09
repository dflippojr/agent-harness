import re
import tempfile
from pathlib import Path

import pytest

from harness import backend_state, cli_domains, metrics, secret_scan
from harness_modules.local_model import service as gpu_guard
from harness_modules.local_model.warmup import READY, ModelWarmer


@pytest.fixture(autouse=True)
def isolated_local_supervision(monkeypatch, tmp_path):
    """Supervision tests use temporary flags and fake hardware/process readings only."""
    import psutil
    from types import SimpleNamespace
    from harness_modules.local_model import resources

    original_init = gpu_guard.ServerControl.__init__

    def init(self, cfg, model):
        original_init(self, cfg, model)
        if str(self.flag).replace('\\', '/').lower().startswith('c:/ai/'):
            self.flag = tmp_path / 'llama-server.paused'

    async def fake_command(*args, **kwargs):
        return 0, '', ''

    original_run = resources._run

    def run(args, timeout=5):
        if args[0] == 'nvidia-smi':
            return None
        return original_run(args, timeout)

    monkeypatch.setattr(gpu_guard.ServerControl, '__init__', init)
    monkeypatch.setattr(gpu_guard, 'run_cmd', fake_command)
    monkeypatch.setattr(psutil, 'process_iter', lambda *args, **kwargs: iter(()))
    monkeypatch.setattr(psutil, 'virtual_memory', lambda: SimpleNamespace(available=64 * 1024**3, total=128 * 1024**3))
    monkeypatch.setattr(resources, '_run', run)

# One pinned gitleaks per machine, fetched once and checked against harness/gitleaks/pin.json (issue #263).
SCANNER_TOOLS = Path(tempfile.gettempdir()) / "agent-harness-test-tools"
_real_download = secret_scan._download


@pytest.fixture(autouse=True)
def fresh_metrics(monkeypatch):
    """Tests read /metrics right after changing the database; the 10 s aggregate cache would hide the change."""
    monkeypatch.setattr(metrics, "CORE_CACHE_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def no_real_cli_volumes(monkeypatch):
    """No test reaches the server's hosted-CLI volumes (#371): preparing a domain is a no-op, and a docker call from
    cli_domains that names a standard harness volume fails. Container tests use their own uniquely named volumes."""
    real = cli_domains.run_cmd
    standard = re.compile(r"harness-(auth|login)-|harness-cli-\w+-app-")

    async def guarded(args, *rest, **kwargs):
        if any(standard.search(str(arg)) for arg in args):
            raise AssertionError(f"a test reached a real CLI volume: {args}")
        return await real(args, *rest, **kwargs)

    async def prepare(*_args, **_kwargs):
        return None
    monkeypatch.setattr(cli_domains, "run_cmd", guarded)
    monkeypatch.setattr(cli_domains, "prepare", prepare)
    # The login probe runs `docker run -v <login volume>`, which would also create a missing one.
    monkeypatch.setattr(backend_state, "_probe_subscription", lambda *_args, **_kwargs: False)


@pytest.fixture(autouse=True)
def plenty_of_ram(monkeypatch):
    """The resource guard's RAM check must not depend on how busy the test machine is."""
    gib = 1024 ** 3
    monkeypatch.setattr(gpu_guard, "memory_reading", lambda: {"available": 64 * gib, "total": 128 * gib,
                                                              "commit": 32 * gib, "commit_limit": 160 * gib})


@pytest.fixture(autouse=True)
def model_awake(monkeypatch):
    """Scripted-model tests have no llama-server to ask whether the model is asleep."""
    async def state(self, model):
        return READY
    monkeypatch.setattr(ModelWarmer, "state", state)


@pytest.fixture(scope="session")
def gitleaks_tools() -> Path:
    """A tools dir holding the pinned gitleaks. Review push/merge fail closed without it."""
    scanner = secret_scan.Scanner(SCANNER_TOOLS)
    problem = scanner.ensure(fetch=_real_download)
    assert not problem, f"tests need the pinned gitleaks: {problem}"
    return SCANNER_TOOLS


@pytest.fixture(autouse=True)
def pinned_scanner(monkeypatch, gitleaks_tools):
    """Every Manager scans with the shared pinned binary; no test downloads anything by accident."""
    monkeypatch.setattr(secret_scan, "tools_dir", lambda cfg: gitleaks_tools)

    def no_download(url):
        raise RuntimeError("tests don't download; use the gitleaks_tools fixture")
    monkeypatch.setattr(secret_scan, "_download", no_download)


@pytest.fixture(autouse=True, scope="session")
def local_owner_client():
    """A test client speaks for the owner on this machine: it sends the daemon's local owner token, as the CLI does.
    A test of a caller without it sets `client.local_owner = False`."""
    from starlette.testclient import TestClient
    from harness import local_owner

    original = TestClient.build_request

    def build_request(self, *args, **kwargs):
        request = original(self, *args, **kwargs)
        # TestClient's synthetic default stands in for the daemon's configured loopback listener. Explicit
        # hosts and custom base URLs are left intact so Host validation tests exercise the real guard.
        manager = getattr(getattr(self.app, "state", None), "manager", None)
        headers = kwargs.get("headers") or {}
        names = headers.keys() if hasattr(headers, "keys") else (key for key, _ in headers)
        if (manager and str(self.base_url).rstrip("/") == "http://testserver"
                and request.url.host == "testserver" and not any(key.lower() == "host" for key in names)
                and "host" not in self.headers):
            request.url = request.url.copy_with(host="127.0.0.1", port=manager.cfg.port)
            request.headers["Host"] = f"127.0.0.1:{manager.cfg.port}"
        token = getattr(manager, "local_owner_token", "")
        # Like the CLI, it sends no local token with another credential; a browser preflight never carries one.
        if (token and getattr(self, "local_owner", True) and local_owner.HEADER not in request.headers
                and "authorization" not in request.headers and request.method != "OPTIONS"):
            request.headers[local_owner.HEADER] = token
        return request

    # Its own patcher, so module-scoped clients get it and a test's `monkeypatch.undo()` keeps it.
    patcher = pytest.MonkeyPatch()
    patcher.setattr(TestClient, "build_request", build_request, raising=False)
    yield
    patcher.undo()


FAKE_TAILSCALE_DIR = Path("C:/Program Files/Tailscale")


def fake_tailscaled_check(**overrides):
    """A peer check whose connection table says every peer is tailscaled from the install directory."""
    from harness import tailscale_peer
    options = dict(owner=lambda client, server: 4242, exe=lambda pid: str(FAKE_TAILSCALE_DIR / "tailscaled.exe"),
                   dirs=lambda: [FAKE_TAILSCALE_DIR], platform_ok=lambda: True)
    options.update(overrides)
    return tailscale_peer.PeerCheck(**options)


@pytest.fixture(autouse=True, scope="session")
def tailscaled_peer():
    """A test that sends Tailscale identity headers stands in for `tailscale serve`, so its peer is tailscaled.
    Tests of other peers give the manager their own `tailscale_peer.PeerCheck`; none reads the live connection table."""
    from harness import tailscale_peer

    patcher = pytest.MonkeyPatch()  # session-wide, so module-scoped managers get it too
    patcher.setattr(tailscale_peer, "default_check", fake_tailscaled_check)
    yield
    patcher.undo()
