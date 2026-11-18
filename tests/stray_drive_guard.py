"""Issue #854: fail the run when a literal `D:` folder is in the working directory at its end. Off Windows a `D:/...` path
is relative, so anything writing to one lands in the checkout. Imported by conftest.py so its hook applies to the suite.
A folder left by an earlier run fails too: this run could have written into it."""

from __future__ import annotations

import os

import pytest

FOLDER = "D:"


def pytest_sessionfinish(session):
    if hasattr(session.config, "workerinput"):  # xdist workers share the controller's working directory
        return
    if FOLDER in os.listdir(os.curdir):
        pytest.exit(f"a literal {FOLDER} folder is in {os.getcwd()}; a Windows path was used off Windows", returncode=1)
