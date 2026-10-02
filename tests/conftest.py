import tempfile
from pathlib import Path

import pytest

from harness import secret_scan
from harness.warmup import READY, ModelWarmer

# One pinned gitleaks per machine, fetched once and checked against harness/gitleaks/pin.json (issue #263).
SCANNER_TOOLS = Path(tempfile.gettempdir()) / "agent-harness-test-tools"
_real_download = secret_scan._download


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
