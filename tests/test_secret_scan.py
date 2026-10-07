"""Secret scanning before Review push/merge (issue #263): pinned gitleaks over the session's added lines."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from pathlib import Path

import pytest

from harness import doctor, secret_scan, transcript
from harness.changes import repo_diffs
from harness.config import Project
from harness.llm import Completion
from harness.manager import HarnessError, Manager

from test_api import make_client, wait_for
from test_daemon import Script, call
from test_phase3 import edit_steps, finished, make_repo, project_cfg, sh

# Built at runtime so the repository itself holds no credential-shaped string. Not EXAMPLE-suffixed:
# the pinned aws-access-token rule allowlists AWS's documentation keys.
KEY = "AKIA" + "Z3MPLEK3YT3STQ7Q"
GENERIC = "Zx81pQr7" + "Tm3Kw9Lb2Vn6Hy4Jd"


def diff_of(path: str, added: list[str], context: list[str] = (), removed: list[str] = ()) -> str:
    body = [f" {c}" for c in context] + [f"-{r}" for r in removed] + [f"+{a}" for a in added]
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,{len(context) + len(removed)} +1,{len(context) + len(added)} @@\n" + "\n".join(body) + "\n")


def scanner(tools: Path) -> secret_scan.Scanner:
    return secret_scan.Scanner(tools)


def test_pin_matches_the_shipped_rules():
    assert secret_scan.rules_sha256() == secret_scan.PIN["rules"]["sha256"]
    assert secret_scan.asset_key() in secret_scan.PIN["assets"]
    assert all(len(a["sha256"]) == 64 for a in secret_scan.PIN["assets"].values())


def test_detects_masks_and_redacts_added_lines(gitleaks_tools):
    diff = (diff_of("conf.py", [f'aws = "{KEY}"', "ok = 1"], context=["x = 0"])
            + diff_of("web/client.js", [f'const API_KEY = "{GENERIC}";']))
    result = scanner(gitleaks_tools).scan([{"path": ".", "head": "h1", "diff": diff}], "s1")
    assert result["status"] == "ok"
    found = {(f["file"], f["line"], f["rule"]) for f in result["findings"]}
    assert found == {("conf.py", 2, "aws-access-token"), ("web/client.js", 1, "generic-api-key")}
    aws = next(f for f in result["findings"] if f["rule"] == "aws-access-token")
    assert aws["preview"] == f"{KEY[:2]}…{KEY[-2:]}"
    public = json.dumps(secret_scan.public(result))
    assert KEY not in public and GENERIC not in public and "_spans" not in public
    masked = secret_scan.redact(diff, 0, result["findings"])
    assert KEY not in masked and GENERIC not in masked
    assert f'+aws = "[secret {aws["preview"]}]"' in masked
    assert 'const API_KEY = "[secret Zx…Jd]";' in masked  # only the value is masked, not the assignment


def test_only_added_lines_are_scanned(gitleaks_tools):
    diff = diff_of("old.py", ["clean = True"], context=[f"kept = '{KEY}'"], removed=[f"gone = '{KEY}'"])
    result = scanner(gitleaks_tools).scan([{"path": ".", "head": "h", "diff": diff}], "s1")
    assert result["status"] == "ok" and result["findings"] == []


def test_cache_by_head_diff_and_pin(gitleaks_tools):
    s = scanner(gitleaks_tools)
    repos = [{"path": ".", "head": "h1", "diff": diff_of("a.py", [f"k = '{KEY}'"])}]
    first = s.scan(repos, "s1")
    again = s.scan([dict(r) for r in repos], "s1")
    assert not first["cached"] and again["cached"]
    assert again["findings"] == first["findings"]
    moved = s.scan([{**repos[0], "head": "h2"}], "s1")
    assert not moved["cached"]
    # the fingerprint follows the value and file, not the line, so a dismissal survives later heads
    shifted = s.scan([{"path": ".", "head": "h3", "diff": diff_of("a.py", ["# new", f"k = '{KEY}'"])}], "s1")
    assert shifted["findings"][0]["line"] == 2
    assert shifted["findings"][0]["fingerprint"] == first["findings"][0]["fingerprint"]
    other = s.scan(repos, "s2")
    assert other["findings"][0]["fingerprint"] != first["findings"][0]["fingerprint"]  # salted per session


def test_workspace_gitleaks_config_has_no_effect(gitleaks_tools, tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "ws")
    base = sh(repo, "rev-parse", "HEAD")
    allow_all = "[allowlist]\nregexes = ['''.*''']\npaths = ['''.*''']\n[extend]\nuseDefault = true\n"
    (repo / ".gitleaks.toml").write_text(allow_all)
    (repo / "creds.py").write_text(f"KEY = '{KEY}'  # gitleaks:allow\n")
    (repo / ".gitleaksignore").write_text(":aws-access-token:2\n:aws-access-token:3\ncreds.py:aws-access-token:1\n")
    sh(repo, "add", ".")
    sh(repo, "commit", "-qm", "add creds")
    env_cfg = tmp_path / "env.toml"
    env_cfg.write_text(allow_all)
    monkeypatch.setenv("GITLEAKS_CONFIG", str(env_cfg))
    monkeypatch.setenv("GITLEAKS_CONFIG_TOML", allow_all)
    monkeypatch.chdir(repo)  # gitleaks would find .gitleaksignore in its working directory by default
    result = scanner(gitleaks_tools).scan(repo_diffs(repo, base), "s1")
    assert [(f["file"], f["rule"]) for f in result["findings"]] == [("creds.py", "aws-access-token")]


def test_missing_or_broken_scanner_is_reported(tmp_path):
    missing = scanner(tmp_path / "none").scan([{"path": ".", "head": "h", "diff": diff_of("a", ["x"])}], "s")
    assert missing["status"] == "unavailable" and "not installed" in missing["message"]
    broken = scanner(tmp_path / "broken")
    broken.problem = lambda: ""
    broken.binary = Path(sys.executable)  # exits non-zero on gitleaks arguments
    result = broken.scan([{"path": ".", "head": "h", "diff": diff_of("a", ["x"])}], "s")
    assert result["status"] == "error" and "exited" in result["message"]
    assert not broken._cache  # errors are not cached


def test_install_rejects_a_bad_checksum_and_doctor_warns(tmp_path, monkeypatch, capsys):
    s = scanner(tmp_path / "tools")
    problem = s.install(fetch=lambda url: b"not the release")
    assert "checksum mismatch" in problem
    assert not s.binary.exists()
    assert "checksum mismatch" in s.bootstrap_error()
    monkeypatch.setattr(secret_scan, "tools_dir", lambda cfg: tmp_path / "tools")
    report = doctor.Report()
    doctor.check_secret_scanner(report, object())
    out = capsys.readouterr().out
    assert report.warned == 1 and "not installed" in out and "checksum mismatch" in out


def test_doctor_ok_with_the_pinned_binary(gitleaks_tools, capsys):
    report = doctor.Report()
    doctor.check_secret_scanner(report, object())
    assert report.warned == 0 and secret_scan.SCANNER in capsys.readouterr().out


# Review gate on a real local project
def _leaks(m: Manager, sid: str, caplog) -> list[str]:
    """Every place the fixture value must never reach."""
    places = {
        "events": json.dumps(m.db.events(sid)),
        "audit": json.dumps(m.db.list_audit()),
        "transcript": transcript.render(m.db, sid),
        "logs": caplog.text,
    }
    files = list(m.cfg.transcripts_dir.glob("*")) if m.cfg.transcripts_dir.exists() else []
    places.update({f"file:{p.name}": p.read_text(encoding="utf-8", errors="replace") for p in files if p.is_file()})
    return [name for name, text in places.items() if KEY in text]


def test_committed_key_blocks_merge_until_dismissed(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    src = make_repo(tmp_path / "src")
    cfg = project_cfg(tmp_path, str(src))

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        s = await finished(m, m.create("bump", project="proj")["id"])
        sid, ws = s["id"], Path(s["workspace"])
        (ws / "settings.py").write_text(f"AWS_ACCESS_KEY_ID = '{KEY}'\n")  # e.g. copied from .env
        sh(ws, "add", "settings.py")
        sh(ws, "commit", "-qm", "settings")

        with pytest.raises(HarnessError) as e:
            await m.review(sid, "merge")
        assert e.value.status == 409 and e.value.code == "secret_findings"
        assert "1 possible secret (aws-access-token: 1)" in str(e.value)
        assert e.value.details == {"findings": 1, "rules": {"aws-access-token": 1}}
        assert KEY not in str(e.value)
        assert (src / "app.py").read_text() == "VALUE = 1\n"  # nothing merged

        data = await m.changes(sid)
        scan = data["secret_scan"]
        assert scan["open"] == 1 and scan["cached"]  # the gate's scan of this head is reused
        finding = scan["findings"][0]
        assert (finding["file"], finding["line"], finding["dismissed"]) == ("settings.py", 1, False)
        assert KEY not in json.dumps(data)
        assert f"[secret {finding['preview']}]" in data["repos"][0]["diff"]

        with pytest.raises(HarnessError) as e:
            await m.dismiss_secret_finding(sid, finding["fingerprint"], "   ", "owner")
        assert e.value.status == 400
        with pytest.raises(HarnessError) as e:
            await m.dismiss_secret_finding(sid, "nope", "test fixture", "owner")
        assert e.value.status == 404
        await m.dismiss_secret_finding(sid, finding["fingerprint"], "test fixture, not a real key", "owner")
        audit = [a for a in m.db.list_audit() if a["action"] == "secret_finding_dismiss"]
        assert len(audit) == 1 and audit[0]["actor_id"] == "owner" and audit[0]["target_id"] == sid
        detail = json.loads(audit[0]["detail"])
        assert detail == {"session": sid, "rule": "aws-access-token", "repo": ".", "file": "settings.py", "line": 1,
                          "fingerprint": finding["fingerprint"], "reason": "test fixture, not a real key"}

        # a later head that moves the line keeps the dismissal
        (ws / "settings.py").write_text(f"# settings\nAWS_ACCESS_KEY_ID = '{KEY}'\n")
        scan = (await m.changes(sid))["secret_scan"]
        assert scan["findings"][0]["line"] == 2 and scan["findings"][0]["dismissed"] and scan["open"] == 0

        s = await m.review(sid, "merge")
        assert s["review"] == "merged"
        assert _leaks(m, sid, caplog) == []
        await m.stop()
    asyncio.run(body())


def test_removing_the_key_from_the_diff_unblocks_merge(tmp_path):
    src = make_repo(tmp_path / "src")
    cfg = project_cfg(tmp_path, str(src))

    async def body():
        m = Manager(cfg, chat=edit_steps(f"VALUE = 2\nKEY = '{KEY}'\n"))
        await m.start(maintenance=False)
        s = await finished(m, m.create("bump", project="proj")["id"])
        with pytest.raises(HarnessError) as e:
            await m.review(s["id"], "merge")
        assert e.value.status == 409
        (Path(s["workspace"]) / "app.py").write_text("VALUE = 2\n")
        s = await m.review(s["id"], "merge")
        assert s["review"] == "merged"
        assert KEY not in (src / "app.py").read_text()
        await m.stop()
    asyncio.run(body())


def test_uncommitted_key_blocks_push(tmp_path, caplog):
    remote = make_repo(tmp_path / "remote.git", bare=True)
    cfg = project_cfg(tmp_path, remote.as_uri())

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        s = await finished(m, m.create("bump", project="proj")["id"])
        (Path(s["workspace"]) / "notes.txt").write_text(f"aws {KEY}\n")  # never committed by the agent
        with pytest.raises(HarnessError) as e:
            await m.review(s["id"], "push")
        assert e.value.status == 409 and e.value.code == "secret_findings"
        assert sh(remote, "branch", "--list", s["branch"]) == ""  # nothing pushed
        assert _leaks(m, s["id"], caplog) == []
        await m.stop()
    asyncio.run(body())


def _commit_then_remove_key(ws: Path) -> str:
    """Commit a key, then a commit that removes it: the net diff is clean, the first commit is not."""
    (ws / "settings.py").write_text(f"AWS_ACCESS_KEY_ID = '{KEY}'\n")
    sh(ws, "add", "settings.py")
    sh(ws, "commit", "-qm", "add key")
    added = sh(ws, "rev-parse", "HEAD").strip()
    (ws / "settings.py").write_text("import os\nAWS_ACCESS_KEY_ID = os.environ['AWS_ACCESS_KEY_ID']\n")
    sh(ws, "commit", "-qam", "read it from the environment")
    return added


def test_key_removed_by_a_later_commit_blocks_push_until_dismissed(tmp_path, caplog):
    remote = make_repo(tmp_path / "remote.git", bare=True)
    cfg = project_cfg(tmp_path, remote.as_uri())

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        s = await finished(m, m.create("bump", project="proj")["id"])
        sid, ws = s["id"], Path(s["workspace"])
        added = _commit_then_remove_key(ws)

        with pytest.raises(HarnessError) as e:
            await m.review(sid, "push")
        assert e.value.status == 409 and e.value.code == "secret_findings"
        assert e.value.details == {"findings": 1, "rules": {"aws-access-token": 1}}
        assert sh(remote, "branch", "--list", s["branch"]) == ""  # nothing pushed

        data = await m.changes(sid)
        assert KEY not in json.dumps(data)
        [finding] = data["secret_scan"]["findings"]
        assert (finding["file"], finding["line"], finding["commit"]) == ("settings.py", 1, added[:12])
        assert finding["fingerprint"] != _net_fingerprint(m, sid, ws)  # the commit is part of the fingerprint

        await m.dismiss_secret_finding(sid, finding["fingerprint"], "rotated; history is fine", "owner")
        s = await m.review(sid, "push")
        assert s["review"] == "pushed"
        assert _leaks(m, sid, caplog) == []
        await m.stop()
    asyncio.run(body())


def _user_messages(m: Manager, sid: str) -> list[str]:
    return [e["data"]["content"] for e in m.db.events(sid) if e["type"] == "user_message"]


def test_ask_fix_on_a_commit_only_finding_asks_for_a_history_rewrite(tmp_path, caplog):
    """Owner decision on #263: a value only in an earlier commit is not skipped; the agent is asked to rewrite
    the branch's own commits, and Push is allowed once no commit since the base has it."""
    remote = make_repo(tmp_path / "remote.git", bare=True)
    cfg = project_cfg(tmp_path, remote.as_uri())

    async def body():
        m = Manager(cfg, chat=Script([*edit_steps().steps, Completion(content="Rewrote the branch.")]))
        await m.start(maintenance=False)
        s = await finished(m, m.create("bump", project="proj")["id"])
        sid, ws, base = s["id"], Path(s["workspace"]), s["base_commit"]
        added = _commit_then_remove_key(ws)
        before = _user_messages(m, sid)

        result = await m.secret_findings_fix(sid)
        assert result["drafts"] == [] and result["already_drafted"] == 0 and result["pushed"] == []
        assert [f["commit"] for f in result["rewrite"]] == [added[:12]]
        assert result["message"].startswith("Asked the agent to remove 1 finding from the branch's earlier commits")
        assert "already have draft" not in result["message"]
        [sent] = _user_messages(m, sid)[len(before):]
        assert f"commit {added[:12]}: settings.py line 1, rule aws-access-token" in sent
        assert f"{base[:12]}..HEAD" in sent and "do not push" in sent and KEY not in sent
        await finished(m, sid)

        # The agent's rewrite: the same changes, with no commit since the base adding the value.
        sh(ws, "reset", "-q", "--soft", base)
        sh(ws, "commit", "-qm", "bump, reading the key from the environment")
        assert KEY not in sh(ws, "log", "-p", f"{base}..HEAD")
        assert (await m.changes(sid))["secret_scan"]["findings"] == []
        assert (await m.review(sid, "push"))["review"] == "pushed"
        assert _leaks(m, sid, caplog) == []
        await m.stop()
    asyncio.run(body())


def test_ask_fix_on_a_commit_already_pushed_says_dismiss_instead(tmp_path):
    """Nothing at or before the remote branch's tip is rewritten: that needs a force-push, so only dismiss is left."""
    remote = make_repo(tmp_path / "remote.git", bare=True)
    cfg = project_cfg(tmp_path, remote.as_uri())

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        s = await finished(m, m.create("bump", project="proj")["id"])
        sid, ws, branch = s["id"], Path(s["workspace"]), s["branch"]
        _commit_then_remove_key(ws)
        sh(ws, "push", "-q", "origin", f"{branch}:refs/heads/{branch}")  # pushed outside the gate
        sent = _user_messages(m, sid)

        result = await m.secret_findings_fix(sid)
        assert result["rewrite"] == [] and len(result["pushed"]) == 1 and result["drafts"] == []
        assert "force-push" in result["message"] and "dismiss them with a reason" in result["message"]
        assert _user_messages(m, sid) == sent  # nothing asked of the agent

        # A push the harness recorded counts too (a member GitHub push leaves no remote-tracking ref here).
        sh(ws, "update-ref", "-d", f"refs/remotes/origin/{branch}")
        assert (await m.secret_findings_fix(sid))["rewrite"] != []  # the remote-tracking ref was the evidence
        await finished(m, sid)
        m.bus.emit(sid, "review", {"action": "push", "state": "pushed", "detail": "",
                                   "head": sh(ws, "rev-parse", "HEAD")[:12]})
        result = await m.secret_findings_fix(sid)
        assert result["rewrite"] == [] and len(result["pushed"]) == 1
        with pytest.raises(HarnessError) as e:
            await m.review(sid, "push")
        assert e.value.code == "secret_findings"
        await m.stop()
    asyncio.run(body())


def test_key_moved_to_another_file_still_blocks_push_for_its_commit(tmp_path, caplog):
    """dev.env gets the key and loses it again; prod.env has it at HEAD. Dismissing the prod.env finding
    must not let the dev.env commit through, so the commit's finding is not folded into it."""
    remote = make_repo(tmp_path / "remote.git", bare=True)
    cfg = project_cfg(tmp_path, remote.as_uri())

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        s = await finished(m, m.create("bump", project="proj")["id"])
        sid, ws = s["id"], Path(s["workspace"])
        (ws / "dev.env").write_text(f"AWS_ACCESS_KEY_ID={KEY}\n")
        sh(ws, "add", "dev.env")
        sh(ws, "commit", "-qm", "dev key")
        added = sh(ws, "rev-parse", "HEAD").strip()
        (ws / "dev.env").write_text("AWS_ACCESS_KEY_ID=\n")
        sh(ws, "commit", "-qam", "drop dev key")
        (ws / "prod.env").write_text(f"AWS_ACCESS_KEY_ID={KEY}\n")

        findings = (await m.changes(sid))["secret_scan"]["findings"]
        assert sorted((f["file"], f.get("commit", "")) for f in findings) == [("dev.env", added[:12]), ("prod.env", "")]
        prod = next(f for f in findings if f["file"] == "prod.env")
        await m.dismiss_secret_finding(sid, prod["fingerprint"], "test fixture", "owner")
        with pytest.raises(HarnessError) as e:
            await m.review(sid, "push")
        assert e.value.code == "secret_findings" and e.value.details["findings"] == 1
        assert sh(remote, "branch", "--list", s["branch"]) == ""
        assert _leaks(m, sid, caplog) == []
        await m.stop()
    asyncio.run(body())


def _net_fingerprint(m: Manager, sid: str, ws: Path) -> str:
    """The fingerprint the same value would have in the working diff."""
    diffs = [{"path": ".", "head": "x", "diff": diff_of("settings.py", [f"AWS_ACCESS_KEY_ID = '{KEY}'"])}]
    return m.secret_scanner.scan(diffs, sid)["findings"][0]["fingerprint"]


def test_key_removed_by_a_later_commit_does_not_block_merge(tmp_path):
    src = make_repo(tmp_path / "src")
    cfg = project_cfg(tmp_path, str(src))

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        s = await finished(m, m.create("bump", project="proj")["id"])
        _commit_then_remove_key(Path(s["workspace"]))
        assert (await m.changes(s["id"]))["secret_scan"]["open"] == 1  # shown, but a squash merge drops it
        s = await m.review(s["id"], "merge")
        assert s["review"] == "merged"
        assert KEY not in (src / "settings.py").read_text()
        await m.stop()
    asyncio.run(body())


def test_clean_multi_commit_push_passes(tmp_path):
    remote = make_repo(tmp_path / "remote.git", bare=True)
    cfg = project_cfg(tmp_path, remote.as_uri())

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        s = await finished(m, m.create("bump", project="proj")["id"])
        ws = Path(s["workspace"])
        for n in range(3):
            (ws / f"step{n}.py").write_text(f"STEP = {n}\n")
            sh(ws, "add", ".")
            sh(ws, "commit", "-qm", f"step {n}")
        scan = (await m.changes(s["id"]))["secret_scan"]
        assert scan["status"] == "ok" and scan["findings"] == []
        s = await m.review(s["id"], "push")
        assert s["review"] == "pushed"
        assert sh(remote, "branch", "--list", s["branch"]).strip()
        await m.stop()
    asyncio.run(body())


def test_cache_covers_the_commit_range(gitleaks_tools):
    sc = scanner(gitleaks_tools)
    clean = {"path": ".", "head": "h", "diff": diff_of("a.py", ["x = 1"])}
    first = sc.scan([{**clean, "commits": [{"sha": "a" * 40, "diff": diff_of("a.py", ["x = 1"])}]}], "salt")
    assert first["findings"] == [] and not first["cached"]
    leaky = {"sha": "b" * 40, "diff": diff_of("k.py", [f"key = '{KEY}'"])}
    second = sc.scan([{**clean, "commits": [{"sha": "a" * 40, "diff": diff_of("a.py", ["x = 1"])}, leaky]}], "salt")
    assert not second["cached"] and [f["commit"] for f in second["findings"]] == ["b" * 12]


def test_scanner_failure_blocks_push_and_merge(tmp_path):
    src = make_repo(tmp_path / "src")
    cfg = project_cfg(tmp_path, str(src))

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        s = await finished(m, m.create("bump", project="proj")["id"])
        m.secret_scanner = secret_scan.Scanner(tmp_path / "no-tools")
        for action in ("merge", "push"):
            with pytest.raises(HarnessError) as e:
                await m.review(s["id"], action)
            assert e.value.status == 503 and e.value.code == "secret_scan_unavailable"
            assert "not installed" in str(e.value)
        assert (src / "app.py").read_text() == "VALUE = 1\n"
        assert (await m.changes(s["id"]))["secret_scan"]["status"] == "unavailable"
        assert (await m.review(s["id"], "discard"))["review"] == "discarded"  # discard needs no scan
        await m.stop()
    asyncio.run(body())


def test_daemon_start_fetches_a_missing_scanner(tmp_path, gitleaks_tools, monkeypatch):
    cfg = project_cfg(tmp_path, str(make_repo(tmp_path / "src")))
    monkeypatch.setattr(secret_scan, "tools_dir", lambda cfg: tmp_path / "tools")
    archive = {}

    def fetch(url):
        archive["url"] = url
        return _real_archive(gitleaks_tools)
    monkeypatch.setattr(secret_scan, "_download", fetch)

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        await m._scanner_waited()
        assert m.secret_scanner.problem() == ""
        assert archive["url"].endswith(secret_scan.PIN["assets"][secret_scan.asset_key()]["name"])
        await m.stop()
    asyncio.run(body())


def test_stop_during_the_start_up_fetch_returns_and_installs_nothing(tmp_path, gitleaks_tools, monkeypatch):
    cfg = project_cfg(tmp_path, str(make_repo(tmp_path / "src")))
    monkeypatch.setattr(secret_scan, "tools_dir", lambda cfg: tmp_path / "tools")
    fetching, release = threading.Event(), threading.Event()

    def fetch(url):
        fetching.set()
        release.wait(30)
        return _real_archive(gitleaks_tools)
    monkeypatch.setattr(secret_scan, "_download", fetch)

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        assert await asyncio.to_thread(fetching.wait, 10)
        started = time.monotonic()
        await asyncio.wait_for(m.stop(), 10)
        assert time.monotonic() - started < 5
        assert m._scanner_boot.done()
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []
        return m

    started = time.monotonic()
    m = asyncio.run(body())
    assert time.monotonic() - started < 15  # asyncio.run doesn't wait for the fetch either
    release.set()
    for t in [t for t in threading.enumerate() if t.name == "secret-scanner-fetch"]:
        t.join(30)
    # the fetch finished after stop(): nothing was installed or recorded
    assert not m.secret_scanner.binary.exists() and not m.secret_scanner.error_file.exists()
    assert not list((tmp_path / "tools").rglob("*.part"))


def _real_archive(tools: Path) -> bytes:
    """The pinned release archive, kept next to the shared test binary so only one download ever happens."""
    asset = secret_scan.PIN["assets"][secret_scan.asset_key()]
    cached = tools / asset["name"]
    if not cached.exists():
        from conftest import _real_download
        cached.write_bytes(_real_download(secret_scan.PIN["url"].format(name=asset["name"])))
    return cached.read_bytes()


# API: Ask agent to fix, Dismiss (owner only)
def test_api_fix_drafts_and_owner_only_dismiss(tmp_path):
    steps = [Completion(tool_calls=[call("write_file", 0, path="app.py", content="VALUE = 2\n")]),
             Completion(content="done"), Completion(content="removed it")]
    client, m, _ = make_client(tmp_path, steps)
    src = make_repo(tmp_path / "src")
    m.cfg.projects["proj"] = Project(name="proj", repo=str(src))
    with client:
        s = client.post("/sessions", json={"prompt": "bump", "project": "proj"}).json()
        sid = s["id"]
        wait_for(lambda: m.db.get_session(sid)["status"] == "done" and sid not in m.tasks)
        ws = Path(m.db.get_session(sid)["workspace"])
        (ws / "deploy.sh").write_text(f"#!/bin/sh\nexport AWS_ACCESS_KEY_ID={KEY}\n")

        changes = client.get(f"/sessions/{sid}/changes")
        assert changes.status_code == 200 and KEY not in changes.text
        finding = changes.json()["secret_scan"]["findings"][0]
        merge = client.post(f"/sessions/{sid}/review/merge")
        assert merge.status_code == 409 and merge.json()["error"]["code"] == "secret_findings"
        assert KEY not in merge.text

        made = client.post(f"/sessions/{sid}/secret-findings/fix")
        assert made.status_code == 201 and len(made.json()["drafts"]) == 1
        assert made.json()["message"].startswith("Drafted 1 review comment;")
        draft = made.json()["drafts"][0]
        assert (draft["path"], draft["start_line"], draft["side"]) == ("deploy.sh", 2, "new")
        assert "aws-access-token" in draft["comment"] and KEY not in made.text
        again = client.post(f"/sessions/{sid}/secret-findings/fix").json()
        assert again["drafts"] == [] and again["already_drafted"] == 1  # no duplicate drafts
        assert again["message"].startswith("The findings in the diff already have draft comments")
        assert client.post(f"/sessions/{sid}/review-comments/send").status_code == 200
        # Final git/checkpoint work continues after the terminal status is written.
        wait_for(lambda: m.db.get_session(sid)["status"] == "done" and sid not in m.tasks)
        sent = [e["data"]["content"] for e in m.db.events(sid) if e["type"] == "user_message"][-1]
        assert "deploy.sh, line 2" in sent and "aws-access-token" in sent and KEY not in sent

        app = client.post("/keys", json={"name": "shop", "kind": "app", "scopes": ["sessions", "sessions:all"]}).json()
        auth = {"Authorization": f"Bearer {app['key']}"}
        url = f"/api/v1/sessions/{sid}/secret-findings/{finding['fingerprint']}/dismiss"
        # app tokens never dismiss or draft (403, or 404 where the session isn't theirs to see)
        assert client.post(url, json={"reason": "fixture"}, headers=auth).status_code in (403, 404)
        assert client.post(f"/api/v1/sessions/{sid}/secret-findings/fix", headers=auth).status_code in (403, 404)
        web = f"/sessions/{sid}/secret-findings/{finding['fingerprint']}/dismiss"
        assert client.post(web, json={"reason": ""}).status_code == 400
        done = client.post(web, json={"reason": "fixture key for a test"})
        assert done.status_code == 200 and done.json()["dismissed"]
        assert client.post(f"/sessions/{sid}/review/merge").status_code == 200
        assert KEY not in client.get(f"/sessions/{sid}/transcript").text


def test_clean_branch_scan_is_fast(tmp_path):
    src = make_repo(tmp_path / "src")
    cfg = project_cfg(tmp_path, str(src))

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        s = await finished(m, m.create("bump", project="proj")["id"])
        m.secret_scanner.problem()  # the one-time version check is not part of a scan
        scan = (await m.changes(s["id"]))["secret_scan"]
        assert scan["status"] == "ok" and scan["findings"] == [] and not scan["cached"]
        print(f"clean-branch scan: {scan['elapsed_ms']} ms")
        assert scan["elapsed_ms"] < 5000
        assert (await m.changes(s["id"]))["secret_scan"]["cached"]
        await m.stop()
    asyncio.run(body())


def test_remote_session_changes_say_the_scan_is_not_available(tmp_path, monkeypatch):
    """An older runner's Changes explains why the gate is unavailable."""
    m = Manager(project_cfg(tmp_path, str(make_repo(tmp_path / "src"))), chat=edit_steps())
    s = {"id": "s1", "target": "macbook", "workspace_removed": 0, "base_commit": "abc"}
    monkeypatch.setattr(m, "get", lambda ref: s)

    async def remote(session, op, args, timeout=None):
        return {"repos": []}
    monkeypatch.setattr(m, "remote", remote)
    scan = asyncio.run(m.changes("s1"))["secret_scan"]
    assert scan["status"] == "unsupported" and scan["findings"] == []
    assert "macbook" in scan["message"] and "not available" in scan["message"]
