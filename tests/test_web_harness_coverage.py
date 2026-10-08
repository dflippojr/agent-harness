"""Every Web Node harness must be referenced by a pytest wrapper (#372)."""
from pathlib import Path


# Imported by the harnesses; this loader has no tests of its own.
HELPERS = {"web_app_loader.mjs", "web_stub_dom.mjs"}


def test_web_harnesses_have_pytest_wrappers():
    tests = Path(__file__).resolve().parent
    wrappers = [path.read_text(encoding="utf-8") for path in tests.glob("test_*.py")]
    missing = sorted(
        path.name for path in tests.glob("web_*.mjs")
        if path.name not in HELPERS and not any(path.name in source for source in wrappers)
    )
    assert not missing, f"Web harnesses missing pytest wrappers: {', '.join(missing)}"
