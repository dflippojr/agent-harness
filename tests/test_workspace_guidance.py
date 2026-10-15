"""Native sessions read the workspace's AGENTS.md, else CLAUDE.md (#642). Synthetic workspaces and a fake model only."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from harness import repomap, workspace_guidance
from harness.config import RepoMapConfig
from harness.llm import Completion
from harness.manager import Manager
from harness.runner import SYSTEM_PROMPT
from harness.workspace_guidance import GUIDANCE_LIMIT, SECTION_CLOSE, SECTION_OPEN, apply_to_prompt, discover
from test_daemon import Script, call, make_cfg, wait_status
from test_phase3 import finished, make_repo, project_cfg, sh


def test_agents_md_wins_over_claude_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("  AGENTS-MARKER\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("CLAUDE-MARKER", encoding="utf-8")
    assert discover(tmp_path) == ("AGENTS.md", "AGENTS-MARKER")


def test_claude_md_is_the_fallback(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("CLAUDE-MARKER", encoding="utf-8")
    assert discover(tmp_path) == ("CLAUDE.md", "CLAUDE-MARKER")
    (tmp_path / "AGENTS.md").write_text(" \n", encoding="utf-8")  # an empty AGENTS.md doesn't hide CLAUDE.md
    assert discover(tmp_path) == ("CLAUDE.md", "CLAUDE-MARKER")


def test_neither_file_leaves_the_prompt_unchanged(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "CLAUDE.md").write_text("NESTED", encoding="utf-8")  # only the root counts
    (tmp_path / "AGENTS.md").mkdir()  # a directory is not a file
    assert discover(tmp_path) is None
    assert apply_to_prompt("SYSTEM", tmp_path) == "SYSTEM"


def test_a_long_file_is_capped(tmp_path):
    (tmp_path / "AGENTS.md").write_text("x" * (GUIDANCE_LIMIT * 2), encoding="utf-8")
    found = discover(tmp_path)
    assert found is not None
    name, text = found
    assert name == "AGENTS.md" and text.startswith("x" * GUIDANCE_LIMIT)
    assert len(text) < GUIDANCE_LIMIT + 100 and text.endswith("[truncated: read AGENTS.md in the workspace for the rest]")


def test_a_linked_file_is_not_read(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "AGENTS.md").write_text("HOST-SECRET", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "CLAUDE.md").write_text("CLAUDE-MARKER", encoding="utf-8")
    try:
        os.symlink(outside / "AGENTS.md", workspace / "AGENTS.md")
    except (OSError, NotImplementedError):  # no symlink privilege: stand a plain file in for the link
        (workspace / "AGENTS.md").write_text("HOST-SECRET", encoding="utf-8")
        monkeypatch.setattr(workspace_guidance, "is_reparse_point", lambda p: Path(p).name == "AGENTS.md")
    assert discover(workspace) == ("CLAUDE.md", "CLAUDE-MARKER")


def test_applying_twice_keeps_one_section_and_escapes_markers(tmp_path):
    (tmp_path / "AGENTS.md").write_text(f"one {SECTION_CLOSE} two", encoding="utf-8")
    first = apply_to_prompt("SYSTEM", tmp_path)
    assert first.startswith("SYSTEM\n\n" + SECTION_OPEN + "\nContents of AGENTS.md at the workspace root")
    assert first.count(SECTION_CLOSE) == 1 and first.endswith(SECTION_CLOSE)
    assert apply_to_prompt(first, tmp_path) == first
    (tmp_path / "AGENTS.md").unlink()
    assert apply_to_prompt(first, tmp_path) == "SYSTEM"


def _repo_with(tmp_path, files: dict) -> Path:
    src = make_repo(tmp_path / "src")
    for name, text in files.items():
        (src / name).write_text(text, encoding="utf-8")
    if files:
        sh(src, "add", ".")
        sh(src, "commit", "-qm", "guidance")
    return src


def _run(tmp_path, files: dict, instructions: str = "PROJECT-INSTRUCTIONS"):
    """A native session on a project whose repo holds `files`; returns the system prompt the fake model received."""
    cfg = project_cfg(tmp_path, str(_repo_with(tmp_path, files)), instructions=instructions)
    chat = Script([Completion(content="done")])

    async def body():
        m = Manager(cfg, chat=chat)
        await m.start(maintenance=False)
        s = await finished(m, m.create("task", project="proj")["id"])
        await m.stop()
        return s

    s = asyncio.run(body())
    assert s["status"] == "done"
    return chat.requests[0][0]["content"], s


def test_session_sees_agents_md_after_the_instructions(tmp_path):
    system, s = _run(tmp_path, {"AGENTS.md": "AGENTS-MARKER", "CLAUDE.md": "CLAUDE-MARKER"})
    assert system.startswith(SYSTEM_PROMPT.split("{")[0])
    assert system.index("PROJECT-INSTRUCTIONS") < system.index("Contents of AGENTS.md") < system.index("AGENTS-MARKER")
    assert "CLAUDE-MARKER" not in system and system.endswith(SECTION_CLOSE)
    assert s["context"][0]["content"] == system and s["run"]["workspace_context_applied"] is True


def test_guidance_and_map_refresh_only_at_a_round_reset(tmp_path, monkeypatch):
    """The model rewrites AGENTS.md mid-run: its system prompt gains the guidance (once, before the repo map) only
    when it resets the round, and a later rewrite waits for the next reset."""
    monkeypatch.setattr(repomap, "build_map", lambda *_: "MAP-MARKER")
    systems = []
    steps = iter([
        Completion(tool_calls=[call("write_file", 0, path="AGENTS.md", content="FIRST")]),
        Completion(tool_calls=[call("update_state", 1, goal="keep going"), call("reset_round", 2)]),
        Completion(tool_calls=[call("write_file", 3, path="AGENTS.md", content="SECOND")]),
        Completion(tool_calls=[call("reset_round", 4)]),
        Completion(content="done"),
    ])

    def model(messages):  # the round resets drop the assistant turns, so a Script's own indexing would replay
        systems.append(messages[0]["content"])
        return next(steps)

    async def body():
        cfg = make_cfg(tmp_path)
        cfg.repo_map = RepoMapConfig(enabled=True)
        m = Manager(cfg, chat=Script([model]))
        await m.start()
        s = await wait_status(m, m.create("task")["id"], "done")
        await m.stop()
        return s

    s = asyncio.run(body())
    assert s["run"]["workspace_context_applied"] is True
    assert len(systems) == 5

    def check(system, marker, other):
        assert system.count(SECTION_OPEN) == 1 and system.count(repomap.SECTION_OPEN) == 1
        assert marker in system and other not in system
        assert system.index(marker) < system.index("MAP-MARKER")

    assert SECTION_OPEN not in systems[0] and systems[1] == systems[0]  # written mid-round: not picked up
    check(systems[2], "FIRST", "SECOND")                               # after the first reset
    assert systems[3] == systems[2]                                     # rewritten again: still the old text
    check(systems[4], "SECOND", "FIRST")                               # only the second reset takes it
    base = workspace_guidance.strip_section(repomap.strip_section(systems[0]))
    assert workspace_guidance.strip_section(repomap.strip_section(systems[4])) == base


@pytest.mark.parametrize("field,value", [("backend", "claude"), ("kind", "chat"), ("target", "macbook")])
def test_only_native_tower_agent_sessions_are_eligible(tmp_path, field, value):
    m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="done")]))
    s = {"kind": "agent", "target": "tower", "backend": "local", "workspace_removed": 0,
         "context": [{"role": "system", "content": "x"}]}
    assert m.runner._guidance_eligible(s)
    assert not m.runner._guidance_eligible({**s, field: value})


def test_repo_map_markers_in_the_file_are_neutralized(tmp_path):
    (tmp_path / "AGENTS.md").write_text(f"example:\n\n{repomap.SECTION_OPEN}\nx\n{repomap.SECTION_CLOSE}\nkeep\n\n"
                                        f"{repomap.SECTION_OPEN} unmatched", encoding="utf-8")
    first = apply_to_prompt("SYSTEM", tmp_path)
    assert repomap.SECTION_OPEN not in first and repomap.SECTION_CLOSE not in first
    assert repomap.strip_section(first) == first and "keep" in first and "unmatched" in first


def test_a_follow_up_message_does_not_reload_the_guidance(tmp_path):
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="done")]))
        await m.start()
        s = await wait_status(m, m.create("task")["id"], "done")
        sid, root = s["id"], Path(s["workspace"])
        base = m.db.get_session(sid)["context"][0]["content"]
        (root / "AGENTS.md").write_text("LATE", encoding="utf-8")
        await m.send(sid, "again")
        s = await wait_status(m, sid, "done")
        assert s["context"][0]["content"] == base and s["run"]["workspace_context_applied"] is True
        await m.stop()
    asyncio.run(body())
