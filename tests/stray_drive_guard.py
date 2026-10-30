"""Issue #854: fail the run when it leaves a literal `D:` folder in the working directory. Off Windows a `D:/...` path is
relative, so anything writing to one lands in the checkout. Imported by conftest.py so its hooks apply to the suite."""

from __future__ import annotations

import os

import pytest

FOLDER = "D:"
_key = pytest.StashKey[bool]()


def _present() -> bool:
    return FOLDER in os.listdir(os.curdir)


def pytest_sessionstart(session):
    session.stash[_key] = _present()


def pytest_sessionfinish(session):
    if hasattr(session.config, "workerinput"):  # xdist workers share the controller's working directory
        return
    if _present() and not session.stash[_key]:
        pytest.exit(f"this run left a literal {FOLDER} folder in {os.getcwd()}; a Windows path was used off Windows",
                    returncode=1)
