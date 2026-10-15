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

from test_api import LOGIN, wait_for
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


@pytest.mark.parametrize("via", ["local", "claude"], ids=["local-fork", "hosted-fork"])
def test_a_forks_inherited_task_reaches_another_backend(tmp_path, via):
    """The task survives a fork through `via` and on to Codex: the digest has it though the middle fork's own event
    log starts later (a hosted one's does)."""
    async def body():
        m, s = await started(tmp_path)
        middle = await finished(m, (await m.fork(s["id"], 1, "Continue the task.", backend=via))["id"])
        moved = m.db.get_session((await m.fork(middle["id"], 1, "Continue the task.", backend="codex"))["id"])
        assert "User: go" in moved["context"][1]["content"]   # the original task, from the saved context
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


def test_override_routes_start_sessions_on_the_chosen_backend_and_rerun_still_takes_no_body(tmp_path):
    cfg = project_cfg(tmp_path, str(make_repo(tmp_path / "src")))
    cfg.backends["codex"] = BackendConfig(enabled=True, model="gpt-5.5", effort="medium")
    cfg.allowed_logins = [LOGIN]
    m = Manager(cfg, chat=two_edits())
    m.runner.codex_factory = _no_cli
    owner = {"Tailscale-User-Login": LOGIN}
    with TestClient(create_app(m)) as client:
        sid = client.post("/sessions", json={"prompt": "go", "project": "proj"}, headers=owner).json()["id"]
        original = wait_for(lambda: (s := m.db.get_session(sid))["status"] == "done" and s)

        def stored(response, backend, model, effort):
            assert response.status_code == 201, response.text
            row = m.db.get_session(response.json()["id"])
            assert (response.json()["backend"], response.json()["model"]) == (backend, model)
            assert (row["backend"], row["model"], row["effort"]) == (backend, model, effort)

        stored(client.post(f"/sessions/{sid}/rerun", headers=owner), "local", original["model"], original["effort"])
        stored(client.post(f"/sessions/{sid}/rerun", json={"backend": "codex"}, headers=owner),
               "codex", "gpt-5.5", "medium")
        stored(client.post(f"/sessions/{sid}/checkpoints/1/fork", headers=owner,
                           json={"prompt": "go", "backend": "codex", "model": "gpt-5.5"}), "codex", "gpt-5.5", "medium")
        refused = client.post(f"/sessions/{sid}/rerun", json={"backend": "nope"}, headers=owner)
        assert refused.status_code == 400 and "unknown backend" in refused.text


def test_audit_metadata_keeps_backend_and_model_names_only():
    assert namespace_audit.clean({"backend": "codex", "model": "gpt-5.5"}) == {"backend": "codex", "model": "gpt-5.5"}
    assert namespace_audit.clean({"backend": "a prompt with spaces", "model": 5}) == {}


def test_continuing_a_hosted_session_carries_its_unanswered_follow_up(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        sid = s["id"]
        m.db.update_session(sid, backend="claude", model="claude-opus-5")
        # A follow-up the run never answered: it hit the subscription limit before changing a file, so the cancel
        # took no checkpoint and checkpoint 2 is still the latest (#664).
        m.bus.emit(sid, "user_message", {"content": "add tests"})
        m.bus.emit(sid, "limit_waiting", {"backend": "claude", "resets_at": 0})
        moved = m.db.get_session((await m.fork(sid, 2, "Continue the task.", backend="codex"))["id"])
        digest = moved["context"][1]["content"]
        assert digest.index("User: add tests") < digest.index("New instruction:\nContinue the task.")
        await finished(m, moved["id"])
        earlier = m.db.get_session((await m.fork(sid, 1, "from one", backend="codex"))["id"])
        assert "add tests" not in earlier["context"][1]["content"]   # not of checkpoint 1's files
        await finished(m, earlier["id"])
        # A snapshot that failed since means the files changed after checkpoint 2, so its digest stops there
        m.bus.emit(sid, "checkpoint", {"status": "skipped", "reason": "the account is over its disk quota"})
        stale = m.db.get_session((await m.fork(sid, 2, "Continue the task.", backend="codex"))["id"])
        assert "add tests" not in stale["context"][1]["content"]
        await finished(m, stale["id"])

    asyncio.run(body())
