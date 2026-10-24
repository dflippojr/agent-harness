"""Issue #789: the session free-space guard reads the disk the workspace lands on. The suite's result must not depend on
how much room the temp filesystem has, and the guard must still refuse when the disk really is low."""

from __future__ import annotations

import asyncio
import shutil
from types import SimpleNamespace

import pytest

from conftest import pin_session_free_space
from harness.llm import Completion
from harness.manager import HarnessError, Manager
from test_daemon import Script, make_cfg


def disk_with_free_gb(free_gb: float):
    free = int(free_gb * 2**30)
    return lambda path: SimpleNamespace(total=free, used=0, free=free)


def test_a_low_temp_filesystem_does_not_refuse_sessions(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "disk_usage", disk_with_free_gb(1))
    m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="done")]))

    async def body():
        sid = m.create("hello")["id"]
        assert m.db.get_session(sid) is not None
        await m.stop()
    asyncio.run(body())


def test_a_session_disk_that_is_really_low_is_refused(monkeypatch, tmp_path):
    pin_session_free_space(monkeypatch, 1)
    m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="done")]))

    async def body():
        with pytest.raises(HarnessError, match="only 1.0 GB free on the data drive") as refused:
            m.create("hello")
        assert refused.value.status == 507
        await m.stop()
    asyncio.run(body())
