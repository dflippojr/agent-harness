"""Continue on another backend (#664): fork and rerun with a different backend or model. Fake model, no CLI or Docker."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness import member_keys, namespace_audit
from harness.accounts import AccountService
from harness.api import create_app
from harness.cli_backends import CliBackendError
from harness.config import BackendConfig
from harness.manager import BACKEND_OVERRIDE_UNSUPPORTED, HarnessError, Manager

from harness.llm import Completion

from test_api import make_client, wait_for
from test_checkpoints import two_edits
from test_daemon import events
from test_phase3 import finished, make_repo, project_cfg


def _no_cli(**_):
    raise CliBackendError("no real CLI in tests")


async def started(tmp_path):
    """A finished local session with checkpoints at turns 1 and 2, and Claude, Codex and Cursor configured."""
    cfg = project_cfg(tmp_path, str(make_repo(tmp_path / "src")))
    cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5", effort="high")
    cfg.backends["codex"] = BackendConfig(enabled=True, model="gpt-5.5", effort="medium")
    cfg.backends["cursor"] = BackendConfig(enabled=True, model="auto")
    cfg.allowed_logins = ["owner@example.com"]
    m = Manager(cfg, chat=two_edits())
    m.runner.cli_factory = m.runner.codex_factory = m.runner.cursor_factory = _no_cli
    await m.start(maintenance=False)
    return m, await finished(m, m.create("go", project="proj")["id"])


def test_fork_moves_a_claude_session_to_codex(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        sid = s["id"]
        m.db.update_session(sid, backend="claude", model="claude-opus-5", effort="high", tool_profile="bash-only",
                            run={**s["run"], "backend_auth": "api_key", "backend_session_id": "abc"})
        fork = m.db.get_session((await m.fork(sid, 1, "carry on", backend="codex"))["id"])
        assert (fork["backend"], fork["model"], fork["effort"]) == ("codex", "gpt-5.5", "medium")  # its own default, not the parent's
        assert fork["parent_id"] == sid and fork["fork_turn"] == 1
        assert "Summary of the earlier conversation" in fork["context"][1]["content"]
        assert "backend_auth" not in fork["run"] and not fork["run"].get("backend_session_id")
        # builtin Codex can't run bash-only, so the fork starts with the default tools and says so
        assert fork["tool_profile"] == "default"
        ws = Path(fork["workspace"])
        assert (ws / "app.py").read_text() == "VALUE = 2\n" and not (ws / "extra.txt").exists()
        forked = events(m, fork["id"], "forked")[0]
        assert (forked["backend"], forked["model"]) == ("codex", "gpt-5.5")
        assert forked["tool_profile_reset"] == {"from": "bash-only", "to": "default"}
        assert "moved from claude" in forked["summary_note"]
        [audit] = [a for a in m.db.main.audit_page(action="checkpoint.fork")["items"] if a["outcome"] == "ok"]
        assert audit["metadata"]["backend"] == "codex" and audit["metadata"]["model"] == "gpt-5.5"
        assert audit["metadata"]["resulting_session_id"] == fork["id"]
        await finished(m, fork["id"])

    asyncio.run(body())


def test_local_fork_to_hosted_uses_the_digest_and_same_backend_keeps_the_saved_context(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        hosted = m.db.get_session((await m.fork(s["id"], 1, "on claude", backend="claude"))["id"])
        assert (hosted["backend"], hosted["model"], hosted["effort"]) == ("claude", "claude-opus-5", "high")
        assert len(hosted["context"]) == 2 and "Summary of the earlier conversation" in hosted["context"][1]["content"]
        await finished(m, hosted["id"])
        same = m.db.get_session((await m.fork(s["id"], 1, "stay local", backend="local"))["id"])
        assert same["backend"] == "local" and same["model"] == s["model"]
        assert not any("Summary of the earlier" in str(x.get("content")) for x in same["context"])
        assert same["context"][-1]["content"] == "stay local"
        assert "summary_note" not in events(m, same["id"], "forked")[0]
        await finished(m, same["id"])

    asyncio.run(body())


def test_hosted_fork_to_local_keeps_the_digest_in_its_checkpoint(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        m.db.update_session(s["id"], backend="claude", model="claude-opus-5")
        fork = await finished(m, (await m.fork(s["id"], 1, "carry on locally", backend="local"))["id"])
        assert fork["backend"] == "local" and fork["model"] == s["model"]
        rewound = await m.rewind(fork["id"], 1)       # back to where the fork started: the earlier work is still known
        assert "Summary of the earlier conversation" in rewound["context"][1]["content"]
        assert "carry on locally" not in rewound["context"][1]["content"]

    asyncio.run(body())


def test_a_forks_inherited_task_reaches_another_backend(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        child = await finished(m, (await m.fork(s["id"], 1, "Continue the task."))["id"])
        moved = m.db.get_session((await m.fork(child["id"], 1, "Continue the task.", backend="codex"))["id"])
        digest = moved["context"][1]["content"]
        assert "User: go" in digest            # the original task, from the child's saved context, not its event log
        await finished(m, moved["id"])

    asyncio.run(body())


def test_a_hosted_forks_inherited_task_reaches_the_next_backend(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        hosted = await finished(m, (await m.fork(s["id"], 1, "Continue the task.", backend="claude"))["id"])
        moved = m.db.get_session((await m.fork(hosted["id"], 1, "Continue the task.", backend="codex"))["id"])
        assert "User: go" in moved["context"][1]["content"]   # inherited, though the Claude fork's log starts later
        await finished(m, moved["id"])

    asyncio.run(body())


def test_a_digest_fork_uses_the_checkpoints_system_prompt(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        saved = s["context"][0]
        m.db.update_session(s["id"], context=[{"role": "system", "content": "newer guidance"}, *s["context"][1:]])
        moved = m.db.get_session((await m.fork(s["id"], 1, "on codex", backend="codex"))["id"])
        assert moved["context"][0] == saved
        await finished(m, moved["id"])

    asyncio.run(body())


def test_rerun_on_another_backend(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        again = m.rerun(s["id"], backend="codex")
        assert (again["backend"], again["model"]) == ("codex", "gpt-5.5")
        picked = m.rerun(s["id"], backend="claude", model="claude-sonnet-5")
        assert (picked["backend"], picked["model"]) == ("claude", "claude-sonnet-5")
        plain = m.rerun(s["id"])
        assert (plain["backend"], plain["model"]) == ("local", s["model"])
        await asyncio.gather(*m.tasks.values(), return_exceptions=True)

    asyncio.run(body())


def test_member_reruns_a_hosted_session_on_the_local_model(tmp_path, monkeypatch):
    async def body():
        m, s = await started(tmp_path)
        alice = AccountService(m).create("owner", "alice@example.com", "Alice")
        monkeypatch.setattr(m.member_keys, "has", lambda uid, backend: True)
        m.db.update_session(s["id"], owner_id=alice["user_id"], project="scratch", backend="claude", model="claude-opus-5",
                            end_user=member_keys.end_user_id(alice["user_id"]))
        local = m.rerun(s["id"], backend="local")
        assert (local["backend"], local["end_user"]) == ("local", "")
        hosted = m.rerun(s["id"])
        assert hosted["end_user"] == member_keys.end_user_id(alice["user_id"])  # `create` sets the member's own again
        await asyncio.gather(*m.tasks.values(), return_exceptions=True)

    asyncio.run(body())


def test_invalid_member_and_app_overrides_are_refused(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        sid = s["id"]
        for call in (lambda: m.fork(sid, 1, "x", backend="nope"), lambda: _now(m.rerun(sid, backend="nope"))):
            with pytest.raises(HarnessError) as e:
                await call()
            assert e.value.status == 400 and "unknown backend" in str(e.value)
        with pytest.raises(HarnessError) as e:
            await m.fork(sid, 1, "x", model="no-such-model")
        assert e.value.status == 400

        alice = AccountService(m).create("owner", "alice@example.com", "Alice")
        m.db.update_session(sid, owner_id=alice["user_id"])
        with pytest.raises(HarnessError) as created:
            m.create("x", backend="cursor", owner_id=alice["user_id"])
        with pytest.raises(HarnessError) as forked:
            await m.fork(sid, 1, "x", backend="cursor")
        assert (forked.value.status, forked.value.code, str(forked.value)) == (
            created.value.status, created.value.code, str(created.value)) and created.value.status == 403

        m.db.update_session(sid, owner_id="owner", app_id="app-1")
        for call in (lambda: m.fork(sid, 1, "x", backend="codex"), lambda: _now(m.rerun(sid, model="m"))):
            with pytest.raises(HarnessError) as e:
                await call()
            assert (e.value.status, e.value.code) == (409, BACKEND_OVERRIDE_UNSUPPORTED)
        m.db.update_session(sid, app_id="")

    asyncio.run(body())


async def _now(value):
    return value


def test_routes_pass_the_override_and_rerun_still_takes_no_body(tmp_path, monkeypatch):
    client, m, _ = make_client(tmp_path, [Completion(content="done")])
    seen = []

    def rerun(sid, **kw):
        seen.append(kw)
        return m.db.get_session(sid)

    async def fork(sid, turn, prompt, **kw):
        seen.append(kw)
        return m.db.get_session(sid)
    monkeypatch.setattr(m, "rerun", rerun)
    monkeypatch.setattr(m, "fork", fork)
    with client:
        sid = client.post("/sessions", json={"prompt": "go"}).json()["id"]
        assert client.post(f"/sessions/{sid}/rerun").status_code == 201
        assert client.post(f"/sessions/{sid}/rerun", json={"backend": "codex"}).status_code == 201
        assert client.post(f"/sessions/{sid}/checkpoints/1/fork",
                           json={"prompt": "go", "backend": "codex", "model": "gpt-5.5"}).status_code == 201
        wait_for(lambda: m.db.get_session(sid)["status"] == "done")
    assert [(kw["backend"], kw["model"]) for kw in seen] == [(None, None), ("codex", None), ("codex", "gpt-5.5")]


def test_audit_metadata_keeps_backend_and_model_names_only():
    assert namespace_audit.clean({"backend": "codex", "model": "gpt-5.5"}) == {"backend": "codex", "model": "gpt-5.5"}
    assert namespace_audit.clean({"backend": "a prompt with spaces", "model": 5}) == {}
