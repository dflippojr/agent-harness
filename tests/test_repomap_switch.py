"""The repo-map switch (#264): off by default and byte-identical when off; on adds one map, refreshed on reset only."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from bakeoff import agent as bakeoff_agent
from harness import config as config_mod
from harness import repomap
from harness.config import RepoMapConfig
from harness.llm import Completion
from harness.manager import Manager
from harness.runner import SYSTEM_PROMPT
from test_daemon import Script, make_cfg, wait_status

needs_parsers = pytest.mark.skipif(
    not all(importlib.util.find_spec(m) for m in ("tree_sitter", "tree_sitter_python")),
    reason="requirements-repomap.txt not installed")


def test_off_by_default_in_config(tmp_path):
    assert RepoMapConfig().enabled is False
    assert make_cfg(tmp_path).repo_map.enabled is False
    cfg = config_mod.load(config_dir=Path(__file__).resolve().parent.parent / "config", data_dir=tmp_path)
    assert cfg.repo_map.enabled is False and cfg.repo_map.budget_tokens == 1500


def test_config_key_is_read(tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    base = (Path(__file__).resolve().parent.parent / "config" / "harness.yaml").read_text(encoding="utf-8")
    (cfg_dir / "harness.yaml").write_text(base + "\nrepo_map:\n  enabled: true\n  budget_tokens: 900\n", encoding="utf-8")
    (cfg_dir / "projects.yaml").write_text("projects: {}\n", encoding="utf-8")
    cfg = config_mod.load(config_dir=cfg_dir, data_dir=tmp_path / "data")
    assert cfg.repo_map.enabled is True and cfg.repo_map.budget_tokens == 900


def _workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "core.py").write_text("class Engine:\n    def run(self): pass\n", encoding="utf-8")
    (ws / "use.py").write_text("from core import Engine\n\ndef go():\n    return Engine()\n", encoding="utf-8")
    return ws


def test_bakeoff_off_path_prompt_is_byte_identical(tmp_path):
    agent = bakeoff_agent.Agent("http://unused", "m", bakeoff_agent.Workspace(_workspace(tmp_path), None))
    assert agent.system_prompt() == bakeoff_agent.SYSTEM_PROMPT


@needs_parsers
def test_bakeoff_on_path_adds_one_map(tmp_path):
    ws = bakeoff_agent.Workspace(_workspace(tmp_path), None)
    on = bakeoff_agent.Agent("http://unused", "m", ws, repo_map_budget=1500).system_prompt()
    assert on.startswith(bakeoff_agent.SYSTEM_PROMPT) and on.count(repomap.SECTION_OPEN) == 1
    assert "class Engine" in on
    assert on == bakeoff_agent.Agent("http://unused", "m", ws, repo_map_budget=1500).system_prompt()  # cacheable


def test_bakeoff_on_with_nothing_to_map_is_unchanged(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    agent = bakeoff_agent.Agent("http://unused", "m", bakeoff_agent.Workspace(empty, None), repo_map_budget=1500)
    assert agent.system_prompt() == bakeoff_agent.SYSTEM_PROMPT


async def _session(m):
    await m.start()
    s = await wait_status(m, m.create("task")["id"], "done")
    return s, Path(s["workspace"])


def _manager(tmp_path, enabled):
    cfg = make_cfg(tmp_path)
    cfg.repo_map = RepoMapConfig(enabled=enabled)
    return Manager(cfg, chat=Script([Completion(content="done")]))


def test_harness_off_leaves_system_prompt_unchanged(tmp_path):
    async def body():
        m = _manager(tmp_path, enabled=False)
        s, root = await _session(m)
        sid = s["id"]
        (root / "core.py").write_text("class Engine: pass\n", encoding="utf-8")
        before = m.db.get_session(sid)["context"][0]["content"]
        m.runner._repo_map_at_start(sid)
        m.runner._round_reset(m.db.get_session(sid), m.db.get_session(sid)["context"], 0, 3.0, 0)
        after = m.db.get_session(sid)["context"][0]["content"]
        assert after == before and repomap.SECTION_OPEN not in after
        assert "repo_map_applied" not in m.db.get_session(sid)["run"]
        await m.stop()
    asyncio.run(body())


@needs_parsers
def test_harness_on_adds_map_once_and_refreshes_only_on_reset(tmp_path):
    async def body():
        m = _manager(tmp_path, enabled=True)
        s, root = await _session(m)
        sid = s["id"]
        run = {**m.db.get_session(sid)["run"]}
        run.pop("repo_map_applied", None)
        m.db.update_session(sid, run=run)
        base = m.db.get_session(sid)["context"][0]["content"]  # the unmapped prompt this session started with
        assert base == SYSTEM_PROMPT
        (root / "core.py").write_text("class Engine:\n    def run(self): pass\n", encoding="utf-8")
        (root / "use.py").write_text("from core import Engine\n\ndef go():\n    return Engine()\n", encoding="utf-8")

        m.runner._repo_map_at_start(sid)
        first = m.db.get_session(sid)["context"][0]["content"]
        assert first.count(repomap.SECTION_OPEN) == 1 and "class Engine" in first
        assert repomap.strip_section(first) == m.db.get_session(sid)["context"][0]["content"].split("\n\n" + repomap.SECTION_OPEN)[0]

        # Files change mid-round: nothing refreshes the map, neither a second start nor ordinary turns.
        (root / "new.py").write_text("def brand_new(): pass\n", encoding="utf-8")
        m.runner._repo_map_at_start(sid)
        assert m.db.get_session(sid)["context"][0]["content"] == first

        # A round reset is the one refresh, and it keeps exactly one section.
        m.runner._round_reset(m.db.get_session(sid), m.db.get_session(sid)["context"], 0, 3.0, 0)
        refreshed = m.db.get_session(sid)["context"][0]["content"]
        assert refreshed.count(repomap.SECTION_OPEN) == 1 and "brand_new" in refreshed
        assert refreshed != first and repomap.strip_section(refreshed) == base
        await m.stop()
    asyncio.run(body())


@needs_parsers
def test_bakeoff_records_the_prompt_size_actually_sent(tmp_path):
    root = _workspace(tmp_path)
    agent = bakeoff_agent.Agent("http://unused", "m", bakeoff_agent.Workspace(root, None), repo_map_budget=1500)
    before = len(agent.system_prompt())

    def chat(messages, timeout):  # the task renames a mapped symbol, then answers
        (root / "core.py").write_text("class RenamedEngineWithALongerName:\n    def go(self): pass\n", encoding="utf-8")
        return {"choices": [{"message": {"content": "done"}}]}

    agent._chat = chat
    result = agent.run("x")
    assert len(agent.system_prompt()) != before  # the edited workspace now maps differently
    assert result.system_prompt_chars == before == len(result.messages[0]["content"])
