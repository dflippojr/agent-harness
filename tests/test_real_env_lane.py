"""The real-environment lane's own logic (#731): statuses, de-duplicated issues, the summary, and the gates.

Every suite's result is faked here; nothing reaches Docker, the GPU, a hosted CLI or GitHub. The real runs are the
lane itself (scripts/real_env_lane.py, docs/real-env-lane.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
from scripts import real_env_lane as lane  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"


class FakeGitHub:
    def __init__(self, issues: list[dict] | None = None, fail: str = ""):
        self.statuses: list[tuple[str, str, str, str]] = []
        self.issues = list(issues or [])
        self.created: list[tuple[str, str, list[str]]] = []
        self.comments: list[tuple[int, str]] = []
        self.fail = fail

    def post_status(self, sha, context, state, description):
        if self.fail == "status":
            raise lane.GitHubError("HTTP 403: Resource not accessible by integration")
        self.statuses.append((sha, context, state, description))

    def open_issues(self, label):
        assert label == "bug"
        if self.fail == "issues":
            raise lane.GitHubError("HTTP 502")
        return list(self.issues)

    def create_issue(self, title, body, labels):
        number = 900 + len(self.created)
        self.created.append((title, body, labels))
        self.issues.append({"number": number, "title": title, "body": body})
        return number

    def comment(self, number, body):
        self.comments.append((number, body))


def runners(results: dict[str, lane.SuiteResult]) -> dict:
    return {suite: (lambda r=r: r) for suite, r in results.items()}


# --- each suite, each outcome: status, issue, summary --------------------------------------------------------------

@pytest.mark.parametrize("suite", lane.SUITES)
@pytest.mark.parametrize("state,expected_status,files_issue", [
    (lane.PASSED, "success", False),
    (lane.SKIPPED, "success", False),
    (lane.FAILED, "failure", True),
    (lane.ERROR, "error", True),
])
def test_each_suite_result_posts_its_status_files_its_issue_and_lands_in_the_summary(
        tmp_path, suite, state, expected_status, files_issue):
    result = lane.SuiteResult(suite, state, f"{suite} said {state}", log="line 1\nline 2: boom")
    gh = FakeGitHub()
    doc = lane.run_lane(SHA, [suite], runners({suite: result}), gh, tmp_path, "run-1")

    assert gh.statuses == [(SHA, f"real-env/{suite}", expected_status, f"{state}: {suite} said {state}")]
    if state == lane.SKIPPED:
        assert gh.statuses[0][3].startswith("skipped:")  # a skip never reads as a plain pass
    assert bool(gh.created) == files_issue
    if files_issue:
        title, body, labels = gh.created[0]
        assert labels == ["bug"] and title == f"real-env/{suite} {state} on {SHA[:8]}"
        assert lane.issue_marker(suite) in body and SHA in body and f"`{suite}`" in body and "line 2: boom" in body
        assert doc["suites"][suite]["issue"] == {"action": "created", "number": 900}
    entry = doc["suites"][suite]
    assert entry["state"] == state and entry["status"] == expected_status
    assert entry["posted"] == f"posted {expected_status}" and doc["sha"] == SHA and doc["run_id"] == "run-1"
    assert doc["ok"] is (state in (lane.PASSED, lane.SKIPPED))
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert latest == doc == json.loads((tmp_path / "runs" / "run-1" / "summary.json").read_text())
    assert latest["finished_at"].endswith("+00:00")


def test_a_second_failure_comments_on_the_open_issue_instead_of_opening_another(tmp_path):
    gh = FakeGitHub(issues=[{"number": 5, "title": "unrelated", "body": "no marker"}])
    failed = lane.SuiteResult("sandbox", lane.FAILED, "verify did not pass", log="FAILED test_app")
    first = lane.run_lane(SHA, ["sandbox"], runners({"sandbox": failed}), gh, tmp_path, "run-1")
    second = lane.run_lane("f" * 40, ["sandbox"], runners({"sandbox": failed}), gh, tmp_path, "run-2")

    assert len(gh.created) == 1
    assert first["suites"]["sandbox"]["issue"]["action"] == "created"
    assert second["suites"]["sandbox"]["issue"] == {"action": "commented", "number": 900}
    assert gh.comments[0][0] == 900 and "f" * 40 in gh.comments[0][1]
    # Another suite's failure is its own issue.
    lane.run_lane(SHA, ["gpu"], runners({"gpu": lane.SuiteResult("gpu", lane.FAILED, "x")}), gh, tmp_path, "run-3")
    assert len(gh.created) == 2 and lane.issue_marker("gpu") in gh.created[1][1]


def test_github_errors_are_recorded_and_the_summary_is_still_written(tmp_path):
    failed = lane.SuiteResult("gpu", lane.FAILED, "the session failed")
    doc = lane.run_lane(SHA, ["gpu"], runners({"gpu": failed}), FakeGitHub(fail="status"), tmp_path, "r")
    assert doc["suites"]["gpu"]["status"] == "failure"
    assert doc["suites"]["gpu"]["posted"].startswith("error: HTTP 403")
    doc = lane.run_lane(SHA, ["gpu"], runners({"gpu": failed}), FakeGitHub(fail="issues"), tmp_path, "r2")
    assert doc["suites"]["gpu"]["issue"] == {"action": "error", "error": "HTTP 502"}
    assert (tmp_path / "latest.json").is_file()


def test_report_records_the_status_post_outcome():
    ok = lane.SuiteResult("sandbox", lane.PASSED, "fine")
    assert lane.report([ok], SHA, "r", FakeGitHub()) == {"sandbox": {"posted": "posted success"}}
    assert lane.report([ok], SHA, "r", FakeGitHub(fail="status"))["sandbox"]["posted"].startswith("error: HTTP 403")
    assert lane.report([ok], SHA, "r", None) == {"sandbox": {"posted": "no (--no-report)"}}


def test_a_suite_that_raises_is_an_error_not_a_crash(tmp_path):
    def broken():
        raise RuntimeError("docker: command not found")
    gh = FakeGitHub()
    doc = lane.run_lane(SHA, ["sandbox"], {"sandbox": broken}, gh, tmp_path, "r")
    assert doc["suites"]["sandbox"]["state"] == "error"
    assert "docker: command not found" in doc["suites"]["sandbox"]["summary"]
    assert gh.statuses[0][2] == "error" and len(gh.created) == 1


def test_status_descriptions_fit_githubs_limit():
    state, description = lane.status_for(lane.SuiteResult("gpu", lane.FAILED, "x" * 500))
    assert state == "failure" and len(description) == lane.STATUS_DESCRIPTION_MAX and description.endswith("...")


def test_redaction_masks_credentials_in_issue_excerpts():
    log = ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345\n"
           "token=ghs_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV\n"
           '"api_key": "s3cr3t-value-123"\noauth eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop\n'
           "plain line stays")
    text = lane.excerpt(log)
    for secret in ("abcdefghijklmnopqrstuvwxyz012345", "ghs_ABCDEF", "sk-ant-api03", "s3cr3t-value-123", "eyJhbGci"):
        assert secret not in text
    assert "plain line stays" in text and "Authorization: [redacted]" in text and '"api_key": "[redacted]' in text
    body = lane.failure_text(lane.SuiteResult("hosted-cli", lane.FAILED, "token=ghp_ABCDEFGHIJKLMNOPQRSTUVWX", log),
                             SHA, "r")
    assert "ghp_ABCDEF" not in body and "ghs_ABCDEF" not in body


def test_excerpt_keeps_the_tail():
    log = "\n".join(f"line {i}" for i in range(200))
    kept = lane.excerpt(log).splitlines()
    assert len(kept) == lane.EXCERPT_LINES and kept[-1] == "line 199"


# --- live signals: the GPU gate and yielding to real sessions ------------------------------------------------------

METRICS = """# HELP harness_gpu_guard_state Current GPU guard state.
harness_gpu_guard_state{state="clear"} 1
harness_gpu_guard_state{state="pausing"} 0
harness_gpu_guard_state{state="paused"} 0
harness_gpu_guard_state{state="resuming"} 0
harness_model_parked 0
harness_gpu_manual_hold_age_seconds 0
harness_resource_memory_low 0
harness_sessions{status="queued"} 0
harness_sessions{status="running"} 0
harness_sessions{status="done"} 115
"""


def metrics(**changes: float) -> dict[str, float]:
    m = lane.parse_metrics(METRICS)
    m.update(changes)
    return m


def test_gpu_gate_runs_only_when_the_guard_is_clear_and_the_model_loaded():
    assert lane.gpu_hold_reason(metrics()) == ""
    paused = metrics(**{'harness_gpu_guard_state{state="clear"}': 0, 'harness_gpu_guard_state{state="paused"}': 1})
    assert lane.gpu_hold_reason(paused) == "gpu-held: the GPU guard is paused"
    assert "manual GPU hold" in lane.gpu_hold_reason(metrics(harness_gpu_manual_hold_age_seconds=30))
    assert "parked" in lane.gpu_hold_reason(metrics(harness_model_parked=1))
    assert "low memory" in lane.gpu_hold_reason(metrics(harness_resource_memory_low=1))
    assert lane.gpu_hold_reason(None).startswith("gpu-held: ") and "unknown" in lane.gpu_hold_reason(None)
    assert "no GPU guard" in lane.gpu_hold_reason({"harness_sessions{status=\"running\"}": 0})
    for reason in (lane.gpu_hold_reason(paused), lane.gpu_hold_reason(None)):
        assert reason.startswith("gpu-held")


def test_gpu_suite_skips_as_gpu_held_and_never_reports_a_pass(tmp_path, monkeypatch):
    held = metrics(harness_gpu_manual_hold_age_seconds=5)
    monkeypatch.setattr(lane, "fetch_metrics", lambda url=lane.LIVE_METRICS: held)
    result = lane.gpu_suite(lane.Lane(tmp_path, SHA, tmp_path / "run", tmp_path))
    assert result.state == lane.SKIPPED and result.summary.startswith("gpu-held")
    assert lane.status_for(result) == ("success", "skipped: gpu-held: a manual GPU hold is on")


def test_live_busy_counts_running_and_queued_sessions():
    assert lane.live_busy(metrics()) == 0
    assert lane.live_busy(metrics(**{'harness_sessions{status="running"}': 1,
                                     'harness_sessions{status="queued"}': 2})) == 3
    assert lane.live_busy(None) == 0


def test_the_lane_yields_to_live_sessions_then_gives_up():
    readings = iter([3, 1, 0])
    slept: list[float] = []
    now = [0.0]

    def fetch(_url):
        return metrics(**{'harness_sessions{status="running"}': next(readings)})

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds
    assert lane.wait_for_idle("u", 600, poll=60, fetch=fetch, sleep=sleep, clock=lambda: now[0]) == 0
    assert slept == [60, 60]
    busy = lane.wait_for_idle("u", 90, poll=60, fetch=lambda _u: metrics(**{'harness_sessions{status="running"}': 1}),
                              sleep=sleep, clock=lambda: now[0])
    assert busy == 1


def test_a_busy_live_daemon_skips_each_suite(tmp_path, monkeypatch):
    monkeypatch.setattr(lane, "wait_for_idle", lambda *a, **k: 2)
    run = lane.Lane(tmp_path, SHA, tmp_path / "run", tmp_path, force_hosted=True, live_config=hosted_live(tmp_path))
    for suite in (lane.sandbox_suite, lane.gpu_suite, lane.hosted_suite):
        result = suite(run)
        assert result.state == lane.SKIPPED and result.summary.startswith("busy:")


def test_hosted_suite_runs_at_most_weekly(tmp_path):
    (tmp_path / "hosted-ledger.json").write_text(json.dumps({"last_run_at": 1_000_000.0, "sha": SHA}))
    run = lane.Lane(tmp_path, SHA, tmp_path / "run", tmp_path)
    result = lane.hosted_suite(run, clock=lambda: 1_000_000.0 + 86400)
    assert result.state == lane.SKIPPED and result.summary.startswith("weekly cap")
    assert lane.read_ledger(tmp_path)["sha"] == SHA
    assert lane.read_ledger(tmp_path / "missing") == {}


# --- evidence parsing ----------------------------------------------------------------------------------------------

def test_junit_counts(tmp_path):
    report = tmp_path / "r.xml"
    report.write_text('<testsuites><testsuite tests="9" failures="1" errors="2" skipped="3"/></testsuites>')
    assert lane.junit_counts(report) == (3, 1, 2, 3)
    report.write_text('<testsuite tests="2" failures="0" errors="0" skipped="0"/>')
    assert lane.junit_counts(report) == (2, 0, 0, 0)
    assert lane.junit_counts(tmp_path / "none.xml") == (0, 0, 0, 0)


def test_pytest_run_ok_and_line():
    assert lane.PytestRun(0, passed=3).ok
    assert not lane.PytestRun(5).ok and not lane.PytestRun(0, skipped=4).ok  # nothing ran is not a pass
    assert not lane.PytestRun(1, failed=1).ok and not lane.PytestRun(0, errors=1).ok
    assert lane.PytestRun(0, 1, 2, 3, 4).line("x") == "x: 1 passed, 2 failed, 3 errors, 4 skipped"


def test_parse_sse_flattens_event_data():
    text = ('id: 3\nevent: tool_result\ndata: {"seq": 3, "type": "tool_result", "data": {"name": "verify", "ok": true,'
            ' "output": "verify: all checks passed (1 checks)"}}\n\n: keepalive\ndata: not json\n\n')
    assert lane.parse_sse(text) == [{"seq": 3, "type": "tool_result", "name": "verify", "ok": True,
                                     "output": "verify: all checks passed (1 checks)"}]


def result_event(name: str, output: str, ok: bool = True) -> dict:
    return {"type": "tool_result", "name": name, "ok": ok, "output": output}


GOOD_EVENTS = [result_event("git_clone", "cloned lane-fixture"), result_event("run_shell", "fixture-ok\n1\n"),
               result_event("verify", "verify: all checks passed (1 checks)"),
               result_event("read_file", "Error: path escapes the workspace: ../../../outside/secret.txt", ok=False),
               result_event("read_file", "Error: no such file: /host/outside/secret.txt", ok=False),
               result_event("read_file", "Error: path escapes the workspace: lane-fixture/escape", ok=False),
               result_event("run_shell", "cat: /x: No such file or directory\nshell-done")]


def test_sandbox_checks_pass_on_the_journey():
    run = lane.SessionRun("s1", "done", events=GOOD_EVENTS, transcript="done")
    assert lane.sandbox_checks(run, "real-env-secret-1") == []
    assert run.tool_results("verify") == [GOOD_EVENTS[2]]


@pytest.mark.parametrize("change,problem", [
    ({"status": "failed", "stop_reason": "boom"}, "ended failed"),
    ({"events": GOOD_EVENTS[1:]}, "git_clone"),
    ({"events": [GOOD_EVENTS[0], GOOD_EVENTS[2]]}, "run_shell"),
    ({"events": GOOD_EVENTS[:2] + [result_event("verify", "verify: 1 failed (1 checks)", ok=False)]}, "verify"),
    ({"events": GOOD_EVENTS + [result_event("read_file", "real-env-secret-1")]}, "#370 class"),
    ({"transcript": "tool said real-env-secret-1"}, "#370 class"),
    ({"events": GOOD_EVENTS[:4] + GOOD_EVENTS[6:]}, "expected three refused reads"),
    ({"events": GOOD_EVENTS[:5] + [result_event("read_file", "symlinked contents")] + GOOD_EVENTS[6:]},
     "expected three refused reads"),
    ({"events": GOOD_EVENTS[:6]}, "shell read outside /workspace never ran"),
])
def test_sandbox_checks_catch_each_failure(change, problem):
    run = lane.SessionRun(**{"sid": "s1", "status": "done", "events": GOOD_EVENTS, "transcript": "", **change})
    problems = lane.sandbox_checks(run, "real-env-secret-1")
    assert any(problem in p for p in problems), problems


def test_describe_lists_tool_events_and_denials():
    run = lane.SessionRun("s1", "failed", "stuck", events=GOOD_EVENTS[:1] + [{"type": "token"}], denied=["outside"])
    text = lane.describe(run)
    assert text.startswith("session s1: failed (stuck)") and "git_clone" in text and "token" not in text
    assert "denied approvals: ['outside']" in text


# --- the disposable instance's config ------------------------------------------------------------------------------

def live_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "live"
    cfg.mkdir(exist_ok=True)
    (cfg / "harness.yaml").write_text(yaml.safe_dump({
        "default_model": "qwen", "models": {"qwen": {"base_url": "http://127.0.0.1:8090", "context_tokens": 65536}},
        "backends": {"claude": {"enabled": True, "image": "agent-harness-cli:1"}}}))
    (cfg / "harness.local.yaml").write_text(yaml.safe_dump({"data_dir": "/live/data",
                                                            "models": {"qwen": {"base_url": "http://127.0.0.1:8091",
                                                                                "context_tokens": 4096}}}))
    return cfg


def test_the_disposable_instance_never_shares_the_live_networks_proxy_or_pause_flag(tmp_path):
    root = tmp_path / "instance"
    cfg = lane.lane_config(root, 18123, "sandbox", "run1", "img:sha")
    assert cfg["listen"] == {"host": "127.0.0.1", "port": 18123}
    assert cfg["data_dir"] == (root / "data").as_posix() and cfg["repos_dir"] == (root / "repos").as_posix()
    sandbox = cfg["sandbox"]
    assert sandbox["image"] == "img:sha"
    for key in ("network", "egress_network", "proxy_network"):
        assert sandbox[key].startswith("real-env-run1-")
    assert "harness-egress-sandbox" not in sandbox["proxy"] and sandbox["proxy"] == lane.NO_PROXY
    assert cfg["gpu_guard"]["enabled"] is False and cfg["gpu_guard"]["pause_flag"].startswith(root.as_posix())
    assert cfg["modules"]["gpu_guard"] is False and cfg["canary"] == {"enabled": False}
    assert cfg["default_model"] == "lane-scripted" and "backends" not in cfg


def test_the_gpu_instance_uses_the_live_model_address(tmp_path):
    model = lane.live_model(lane.live_raw(live_config(tmp_path)))
    cfg = lane.lane_config(tmp_path / "i", 1, "gpu", "t", "", {"model": model})
    assert cfg["default_model"] == "qwen"
    assert cfg["models"] == {"qwen": {"base_url": "http://127.0.0.1:8091", "context_tokens": 4096}}
    assert cfg["data_dir"] != "/live/data"


def test_the_hosted_instance_copies_only_the_cleared_live_backends(tmp_path):
    spec = {"backends": {"claude": {"enabled": True, "image": "agent-harness-cli:1"}}}
    cfg = lane.lane_config(tmp_path / "i", 1, "hosted", "t", "", spec)
    assert cfg["backends"] == spec["backends"] and cfg["default_model"] == "lane-scripted"
    assert lane.lane_config(tmp_path / "i", 1, "hosted", "t", "")["backends"] == {}
    with pytest.raises(RuntimeError, match="base_url"):
        lane.lane_config(tmp_path / "i", 1, "gpu", "t", "", {})
    assert cfg["data_dir"] == (tmp_path / "i" / "data").as_posix()


def test_live_model_needs_a_base_url():
    with pytest.raises(RuntimeError, match="no local model"):
        lane.live_model({"default_model": "x"})


def test_the_plan_and_the_daemon_read_the_same_merge_of_the_live_config(tmp_path):
    """The daemon's loader merges harness.local.yaml one level deep: a local `claude:` entry replaces the base one. A
    base token with a local entry that drops it must not count as token auth (review of #919)."""
    cfg = tmp_path / "live"
    cfg.mkdir()
    token = tmp_path / "token"
    token.write_text("t\n")
    (cfg / "harness.yaml").write_text(yaml.safe_dump({"backends": {"claude": {"enabled": True,
                                                                              "oauth_token_file": str(token)}}}))
    (cfg / "harness.local.yaml").write_text(yaml.safe_dump({"backends": {"claude": {"enabled": True}}}))
    raw = lane.live_raw(cfg)
    assert raw["backends"]["claude"] == {"enabled": True}
    assert lane.hosted_plan(raw["backends"])[0] == []


def test_fixture_repo_and_scripted_model(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    lane.write_fixture_repo(tmp_path / "repos", broken=True, outside=outside)
    repo = tmp_path / "repos" / lane.FIXTURE_REPO
    assert (repo / "escape").is_symlink() and "'never'" in (repo / "test_app.py").read_text()
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True).stdout
    assert len(log.splitlines()) == 1
    model = lane.ScriptedSandboxModel(outside / "secret.txt")
    names = [name for name, _ in model.steps]
    assert names[:3] == ["git_clone", "run_shell", "verify"] and names.count("read_file") == 3
    first = asyncio.run(model(None, [{"role": "user", "content": "go"}], tools=[{}]))
    assert first.tool_calls[0]["function"]["name"] == "git_clone"
    history = [{"role": "assistant"}] * len(model.steps)
    assert asyncio.run(model(None, history, tools=[{}])).content == "done"
    assert asyncio.run(model(None, [], tools=None)).content


# --- GitHub through gh ---------------------------------------------------------------------------------------------

def test_gh_cli_commands():
    calls: list[tuple[list[str], str | None]] = []

    def run(args, input=None, **_kwargs):
        calls.append((args, input))
        out = {"--paginate": '{"number": 1, "title": "t", "body": "b"}\n{"number": 2, "title": "u", "body": "c"}\n',
               "create": "https://github.com/dflippojr/agent-harness/issues/77\n"}.get(args[2], "")
        return subprocess.CompletedProcess(args, 0, out, "")
    gh = lane.GhCli("o/r", run=run)
    gh.post_status(SHA, "real-env/gpu", "success", "skipped: gpu-held")
    assert gh.open_issues("bug") == [{"number": 1, "title": "t", "body": "b"}, {"number": 2, "title": "u", "body": "c"}]
    assert calls[1][0][:4] == ["gh", "api", "--paginate", "repos/o/r/issues?state=open&labels=bug&per_page=100"]
    assert gh.create_issue("title", "body", ["bug"]) == 77
    gh.comment(77, "again")
    assert calls[0][0][:3] == ["gh", "api", f"repos/o/r/statuses/{SHA}"] and "context=real-env/gpu" in calls[0][0]
    assert calls[2][0][-2:] == ["--label", "bug"] and calls[2][1] == "body"
    assert calls[3][0][:4] == ["gh", "issue", "comment", "77"] and calls[3][1] == "again"


def test_gh_cli_errors_are_redacted():
    def run(args, **_kwargs):
        return subprocess.CompletedProcess(args, 1, "", "HTTP 401 token=ghs_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
    with pytest.raises(lane.GitHubError) as e:
        lane.GhCli(run=run).post_status(SHA, "c", "success", "d")
    assert "ghs_" not in str(e.value) and "[redacted]" in str(e.value)


def test_the_command_line_parses_the_scheduled_invocations():
    args = lane.parser().parse_args(["run", "--suite", "hosted-cli", "--pair"])
    assert args.suite == ["hosted-cli"] and args.pair and not args.no_report and args.hosted_backend == []
    assert lane.parser().parse_args(["run", "--hosted-backend", "codex"]).hosted_backend == ["codex"]
    args = lane.parser().parse_args(["run"])
    assert args.suite is None and args.workers == 2 and Path(args.base).name == "agent-harness-real-env"


# --- the suite runners, with the real environment faked at its edges -----------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code, self._data, self.text = status_code, data, text or json.dumps(data)

    def json(self):
        return self._data


class FakeClient:
    """The disposable daemon's API: a session waits on one approval, then ends with `final`."""

    def __init__(self, final="done", backends=None, events=None, transcript="done real-env-ok"):
        self.statuses = iter(["running", "waiting_approval", final])
        self.posts: list[tuple[str, dict | None]] = []
        self.final, self.backends = final, backends or []
        self.events = events if events is not None else GOOD_EVENTS
        self.transcript = transcript

    def post(self, path, json=None):
        self.posts.append((path, json))
        if path == "/api/v1/sessions":
            return FakeResponse(201, {"id": f"s{len(self.posts)}"})
        return FakeResponse(200, {})

    def get(self, path, params=None):
        if path.endswith("/approvals"):
            return FakeResponse(200, [{"id": "a1", "reason": "reads a file outside /workspace"}])
        if path.endswith("/events"):
            return FakeResponse(200, text="".join(
                f"data: {json.dumps({'type': e['type'], 'data': e})}\n\n" for e in self.events))
        if path.endswith("/transcript"):
            return FakeResponse(200, text=self.transcript)
        if path == "/api/v1/backends":
            return FakeResponse(200, self.backends)
        return FakeResponse(200, {"status": next(self.statuses, self.final), "stop_reason": ""})

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_drive_session_denies_every_approval_and_collects_the_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(lane.time, "sleep", lambda _s: None)
    inst = lane.Instance("http://127.0.0.1:1", "tok", tmp_path, {})
    client = FakeClient()
    run = lane.drive_session(inst, "go", backend="claude", client=client)
    assert run.status == "done" and run.sid == "s1" and run.denied == ["reads a file outside /workspace"]
    assert ("/api/v1/sessions/s1/approvals/a1", {"decision": "deny", "note": "real-env lane: never approves"}) \
        in client.posts
    assert client.posts[0][1]["backend"] == "claude" and client.posts[0][1]["project"] == lane.PROJECT
    assert run.tool_results("git_clone") and run.transcript == "done real-env-ok"
    assert (tmp_path / "sessions.txt").read_text() == "s1\n"


def test_drive_session_reports_a_refused_start_and_a_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(lane.time, "sleep", lambda _s: None)
    inst = lane.Instance("http://127.0.0.1:1", "tok", tmp_path, {})

    class Refusing(FakeClient):
        def post(self, path, json=None):
            return FakeResponse(503, {"detail": "busy"})
    assert lane.drive_session(inst, "go", client=Refusing()).status == "refused"
    stuck = FakeClient(final="running")
    run = lane.drive_session(inst, "go", client=stuck, timeout=0)
    assert run.status.startswith("timeout") and ("/api/v1/sessions/s1/cancel", None) in stuck.posts


class FakeLane(lane.Lane):
    """A Lane whose Docker, pytest and daemon are stand-ins."""

    def __init__(self, tmp_path, tests=None, client=None, live=None, **kwargs):
        super().__init__(tmp_path, SHA, tmp_path / "run", tmp_path, live_config=live or live_config(tmp_path),
                         **kwargs)
        self.tests = tests or lane.PytestRun(0, passed=4)
        self.client_ = client or FakeClient()
        self.markers: list[tuple[str, dict]] = []

    def image(self, kind, dockerfile, build):
        return f"ghcr.io/x/{kind}:sha-{SHA}"

    def pytest(self, marker, env, timeout=3600, paths=("tests",)):
        self.markers.append((marker, env))
        self.paths = paths
        return self.tests

    def instance(self, mode, spec=None):
        self.instance_spec = spec or {}
        root = self.run_dir / f"instance-{mode}"
        (root / "outside").mkdir(parents=True, exist_ok=True)
        (root / "outside" / "secret.txt").write_text("real-env-secret-1\n")
        inst = lane.Instance("http://127.0.0.1:1", "tok", root, {})
        inst.client = lambda: self.client_
        return contextlib.nullcontext(inst)


@pytest.fixture
def idle(monkeypatch):
    monkeypatch.setattr(lane, "wait_for_idle", lambda *a, **k: 0)
    monkeypatch.setattr(lane.time, "sleep", lambda _s: None)
    monkeypatch.setattr(lane, "fetch_metrics", lambda url=lane.LIVE_METRICS: metrics())


def test_sandbox_suite_passes_on_the_journey_and_points_the_tests_at_the_commits_images(tmp_path, idle):
    run = FakeLane(tmp_path)
    result = lane.sandbox_suite(run)
    assert result.state == lane.PASSED, result.summary
    marker, env = run.markers[0]
    assert marker == "docker" and env["HARNESS_TEST_SANDBOX_IMAGE"].endswith(f"sandbox:sha-{SHA}")
    assert env["HARNESS_TEST_CLI_IMAGE"].endswith(f"cli:sha-{SHA}") and env["HARNESS_LIVE_DOCKER"] == "1"
    assert result.details["denied_approvals"] == ["reads a file outside /workspace"]


def test_sandbox_suite_fails_on_a_failing_test_or_a_leaked_secret(tmp_path, idle):
    result = lane.sandbox_suite(FakeLane(tmp_path, tests=lane.PytestRun(1, passed=3, failed=1, output="FAILED x")))
    assert result.state == lane.FAILED and "1 failed" in result.summary and "FAILED x" in result.log
    leaked = FakeClient(events=GOOD_EVENTS + [result_event("run_shell", "real-env-secret-1")])
    (tmp_path / "b").mkdir()
    result = lane.sandbox_suite(FakeLane(tmp_path / "b", client=leaked))
    assert result.state == lane.FAILED and "#370 class" in result.summary


def test_sandbox_suite_fails_when_no_docker_test_ran(tmp_path, idle):
    result = lane.sandbox_suite(FakeLane(tmp_path, tests=lane.PytestRun(0, skipped=6)))
    assert result.state == lane.FAILED and "at least one must run" in result.summary


def test_without_the_commits_cli_image_the_cli_tests_skip_instead_of_using_the_installed_one(tmp_path, idle):
    class NoCli(FakeLane):
        def image(self, kind, dockerfile, build):
            return "" if kind == "cli" else super().image(kind, dockerfile, build)
    run = NoCli(tmp_path)
    lane.sandbox_suite(run)
    env = run.markers[0][1]
    assert env["HARNESS_TEST_CLI_IMAGE"] == lane.MISSING_IMAGE and env["HARNESS_TEST_SANDBOX_PROXY"] == lane.NO_PROXY


def test_gpu_suite_runs_the_marked_tests_and_a_session_on_the_live_model(tmp_path, idle):
    events = [{"type": "tool_result", "name": "write_file", "ok": True, "output": "wrote lane.txt"}]
    run = FakeLane(tmp_path, client=FakeClient(events=events))
    result = lane.gpu_suite(run)
    assert result.state == lane.PASSED, result.summary
    marker, env = run.markers[0]
    assert marker == "gpu" and env["HARNESS_REAL_MODEL_URL"] == "http://127.0.0.1:8091"
    assert env["HARNESS_REAL_MODEL_NAME"] == "qwen"
    assert run.paths == (lane.GPU_CHECKS,)
    no_write = lane.gpu_suite(FakeLane(tmp_path, client=FakeClient(events=[])))
    assert no_write.state == lane.FAILED and "never wrote" in no_write.summary
    failed = lane.gpu_suite(FakeLane(tmp_path, client=FakeClient(final="failed", events=[])))
    assert failed.state == lane.FAILED and "ended failed" in failed.summary


BACKENDS = [{"name": "local", "available": True, "logged_in": True},
            {"name": "claude", "available": True, "logged_in": True},
            {"name": "codex", "available": True, "logged_in": True},
            {"name": "cursor", "available": True, "logged_in": False},
            {"name": "gemini", "available": False, "logged_in": False}]


def hosted_live(tmp_path: Path, token: bool = True) -> Path:
    cfg = tmp_path / "hosted-live"
    cfg.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "claude-oauth-token"
    secret.write_text("not-a-real-token\n")
    claude = {"enabled": True, **({"oauth_token_file": str(secret)} if token else {})}
    (cfg / "harness.yaml").write_text(yaml.safe_dump({
        "default_model": "qwen", "models": {"qwen": {"base_url": "http://127.0.0.1:8090"}},
        "backends": {"claude": claude, "codex": {"enabled": True}, "cursor": {"enabled": True},
                     "gemini": {"enabled": False}, "opencode": {"enabled": True, "kind": "cli"}}}))
    return cfg


def test_hosted_plan_runs_claude_only_on_the_token_and_the_rest_only_when_opted_in(tmp_path):
    backends = lane.live_raw(hosted_live(tmp_path))["backends"]
    eligible, notes = lane.hosted_plan(backends)
    assert eligible == ["claude"]
    assert any(n.startswith("codex: opt-in only") for n in notes) and any(n.startswith("cursor:") for n in notes)
    assert any(n.startswith("opencode: a print-mode CLI on the local model") for n in notes)
    assert not any(n.startswith("gemini") for n in notes)  # disabled: not configured at all
    assert lane.hosted_plan(backends, ("codex",))[0] == ["claude", "codex"]
    eligible, notes = lane.hosted_plan(lane.live_raw(hosted_live(tmp_path / "x", token=False))["backends"], ("claude",))
    assert eligible == [] and any("shared refresh-token login (#390)" in n for n in notes)
    empty = tmp_path / "empty-token"
    empty.write_text("")
    assert lane.hosted_plan({"claude": {"enabled": True, "oauth_token_file": str(empty)}})[0] == []
    assert lane.hosted_plan({"claude": {"enabled": True, "oauth_token_file": str(tmp_path / "nope")}})[0] == []


def test_hosted_suite_skips_when_nothing_is_safe_to_run(tmp_path, idle):
    (tmp_path / "x").mkdir()
    run = FakeLane(tmp_path, live=hosted_live(tmp_path / "x", token=False), force_hosted=True)
    result = lane.hosted_suite(run)
    assert result.state == lane.SKIPPED and result.summary.startswith("no hosted backend is safe to run")
    assert "#390" in result.summary and not lane.read_ledger(tmp_path)


def test_hosted_suite_runs_one_session_per_logged_in_backend_and_records_the_week(tmp_path, idle, monkeypatch):
    sessions: list[str | None] = []

    def drive(inst, prompt, backend=None, timeout=0, client=None):
        sessions.append(backend)
        return lane.SessionRun(f"s{len(sessions)}", "done", events=[{"type": "assistant", "content": lane.OK_WORD}])
    monkeypatch.setattr(lane, "drive_session", drive)
    run = FakeLane(tmp_path, client=FakeClient(backends=BACKENDS), live=hosted_live(tmp_path),
                   hosted_opt_in=("codex",))
    result = lane.hosted_suite(run, clock=lambda: 5_000_000.0)
    assert result.state == lane.PASSED and sessions == ["claude", "codex"]
    assert sorted(run.instance_spec["backends"]) == ["claude", "codex"]  # cursor's login never reaches the instance
    assert any(n.startswith("cursor: opt-in only") for n in result.details["notes"])
    assert result.details["pair"] == "off"
    assert lane.read_ledger(tmp_path) == {"last_run_at": 5_000_000.0, "sha": SHA}


def timed_run(start: float, end: float) -> lane.SessionRun:
    return lane.SessionRun("s", "done", events=[{"type": "status", "status": "running", "ts": start},
                                                {"type": "assistant", "content": lane.OK_WORD},
                                                {"type": "run_finished", "status": "done", "ts": end}])


def test_overlap_needs_both_windows():
    assert lane.overlapped(timed_run(0, 10), timed_run(5, 15))
    assert not lane.overlapped(timed_run(0, 10), timed_run(10, 20)) and not lane.overlapped(timed_run(0, 10),
                                                                                          lane.SessionRun("s", "done"))


def test_hosted_suite_pair_replaces_claudes_session_and_checks_the_login_after(tmp_path, idle, monkeypatch):
    sessions: list[str | None] = []

    def drive(inst, prompt, backend=None, timeout=0, client=None):
        sessions.append(backend)
        return timed_run(10.0 + len(sessions), 30.0)   # both pair sessions run between 12 and 30: they overlap
    monkeypatch.setattr(lane, "drive_session", drive)
    run = FakeLane(tmp_path, client=FakeClient(backends=BACKENDS), pair=True, force_hosted=True,
                   live=hosted_live(tmp_path), hosted_opt_in=("codex",))
    result = lane.hosted_suite(run)
    assert result.state == lane.PASSED and sorted(sessions) == ["claude", "claude", "codex"]
    assert result.details["pair"] == "claude"
    assert run.instance_spec["backends"]["claude"]["max_sessions"] == 2  # room for both at once

    one_after_the_other = iter([timed_run(0, 10), timed_run(0, 10), timed_run(20, 30)])
    monkeypatch.setattr(lane, "drive_session", lambda *a, **k: next(one_after_the_other))
    result = lane.hosted_suite(FakeLane(tmp_path, client=FakeClient(backends=BACKENDS), pair=True, force_hosted=True,
                                        live=hosted_live(tmp_path), hosted_opt_in=("codex",)))
    assert result.state == lane.FAILED and "did not run at the same time" in result.summary


    def rotating(inst, prompt, backend=None, timeout=0, client=None):  # a session rewrites the shared credential
        (tmp_path / "claude-oauth-token").write_text("")
        return timed_run(0, 10)
    monkeypatch.setattr(lane, "drive_session", rotating)
    result = lane.hosted_suite(FakeLane(tmp_path, client=FakeClient(backends=BACKENDS), pair=True, force_hosted=True,
                                        live=hosted_live(tmp_path)))
    assert result.state == lane.FAILED and "#390 class" in result.summary


def test_hosted_answer_comes_from_the_assistant_not_the_prompt(tmp_path, idle, monkeypatch):
    wrong = lane.SessionRun("s", "done", transcript=f"user: Reply with exactly {lane.OK_WORD}\nassistant: sure",
                            events=[{"type": "user_message", "content": f"Reply with exactly {lane.OK_WORD}"},
                                    {"type": "assistant", "content": "sure"}])
    assert lane.OK_WORD not in wrong.answer
    monkeypatch.setattr(lane, "drive_session", lambda *a, **k: wrong)
    result = lane.hosted_suite(FakeLane(tmp_path, client=FakeClient(backends=BACKENDS), force_hosted=True,
                                        live=hosted_live(tmp_path)))
    assert result.state == lane.FAILED and "claude: done" in result.summary


def test_token_digest(tmp_path):
    token = tmp_path / "t"
    token.write_text("a")
    assert len(lane.token_digest({"oauth_token_file": str(token)})) == 64
    assert lane.token_digest({}) == "" and lane.token_digest({"oauth_token_file": str(tmp_path / "gone")}) == ""


def test_hosted_suite_fails_a_backend_that_does_not_answer_and_skips_with_no_login(tmp_path, idle, monkeypatch):
    monkeypatch.setattr(lane, "drive_session",
                        lambda inst, prompt, backend=None, timeout=0, client=None: lane.SessionRun("s", "failed", "401"))
    result = lane.hosted_suite(FakeLane(tmp_path, client=FakeClient(backends=BACKENDS), force_hosted=True,
                                        live=hosted_live(tmp_path)))
    assert result.state == lane.FAILED and "claude: failed 401" in result.summary
    unavailable = [{**b, "available": b["name"] != "codex"} for b in BACKENDS]
    (tmp_path / "y").mkdir()
    result = lane.hosted_suite(FakeLane(tmp_path, client=FakeClient(backends=unavailable), force_hosted=True,
                                        live=hosted_live(tmp_path / "y", token=False), hosted_opt_in=("codex",)))
    assert result.state == lane.SKIPPED and "no cleared hosted backend is available" in result.summary


# --- the real Lane's edges: git archive, images, pytest, the daemon process ----------------------------------------

def git_repo(path: Path) -> str:
    path.mkdir()
    git = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (path / "a.txt").write_text("from the commit\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "c"], check=True)
    return subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def test_extract_takes_the_commits_files_and_resolve_sha(tmp_path):
    sha = git_repo(tmp_path / "repo")
    assert lane.resolve_sha(tmp_path / "repo", sha[:8]) == sha
    with pytest.raises(SystemExit):
        lane.resolve_sha(tmp_path / "repo", "nope")
    run = lane.Lane(tmp_path / "repo", sha, tmp_path / "base" / "runs" / "R-1", tmp_path / "base")
    run.extract()
    assert (run.tree / "a.txt").read_text() == "from the commit\n" and run.tag == "r-1"
    bad = lane.Lane(tmp_path / "repo", "f" * 40, tmp_path / "base" / "runs" / "R-2", tmp_path / "base")
    with pytest.raises(RuntimeError, match="git archive"):
        bad.extract()


def test_image_prefers_ci_published_then_pull_then_build(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    answers: dict[str, int] = {}

    def run(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, answers.get(args[1], 0), "", "nope")
    monkeypatch.setattr(lane, "_run", run)
    lane_ = lane.Lane(tmp_path, SHA, tmp_path / "run", tmp_path)
    assert lane_.image("sandbox", "Dockerfile", build=True) == f"ghcr.io/dflippojr/agent-harness-sandbox:sha-{SHA}"
    answers.update(image=1)
    assert lane_.image("sandbox", "Dockerfile", build=True).startswith("ghcr.io/") and calls[-1][1] == "pull"
    answers.update(pull=1)
    assert lane_.image("cli", "cli.Dockerfile", build=False) == ""
    assert lane_.image("sandbox", "Dockerfile", build=True) == f"real-env-lane-sandbox:{SHA[:12]}"
    assert lane_.built_images == [f"real-env-lane-sandbox:{SHA[:12]}"]
    lane_.cleanup()
    assert calls[-1] == ["docker", "image", "rm", f"real-env-lane-sandbox:{SHA[:12]}"]
    answers.update(build=1)
    with pytest.raises(RuntimeError, match="building the sandbox image failed"):
        lane_.image("sandbox", "Dockerfile", build=True)
    lane_.images = {"sandbox": "given:1"}
    assert lane_.image("sandbox", "Dockerfile", build=True) == "given:1"


def test_pytest_runs_the_marker_from_the_tree_with_capped_workers(tmp_path, monkeypatch):
    seen = {}

    def run(args, cwd=None, env=None, timeout=None, **_kwargs):
        seen.update(args=args, cwd=cwd, env=env)
        report = next(a for a in args if a.startswith("--junitxml=")).split("=", 1)[1]
        Path(report).write_text('<testsuite tests="3" failures="1" errors="0" skipped="1"/>')
        return subprocess.CompletedProcess(args, 1, "1 failed", "")
    monkeypatch.setattr(lane, "_run", run)
    lane_ = lane.Lane(tmp_path, SHA, tmp_path / "run", tmp_path, workers=9)
    lane_.run_dir.mkdir(parents=True)
    (lane_.tree / "tests").mkdir(parents=True)
    assert lane_.pytest("gpu", {}, paths=(lane.GPU_CHECKS,)).code == 5  # an older commit without the checks
    result = lane_.pytest("docker", lane_.env(EXTRA="1"))
    assert (result.passed, result.failed, result.skipped, result.ok) == (1, 1, 1, False)
    assert seen["cwd"] == lane_.tree and seen["args"][seen["args"].index("-n") + 1] == "4"
    assert " tests -m docker " in " ".join(seen["args"]) and seen["env"]["EXTRA"] == "1"
    assert seen["env"]["TMPDIR"] == str(tmp_path / "tmp") and "HARNESS_CONFIG_DIR" not in seen["env"]

    def slow(args, **_kwargs):
        raise subprocess.TimeoutExpired(args, 5)
    monkeypatch.setattr(lane, "_run", slow)
    assert lane_.pytest("gpu", {}, timeout=5).code == -1


def test_instance_starts_the_daemon_in_its_own_session_and_removes_only_its_leftovers(tmp_path, monkeypatch):
    removed: list[list[str]] = []
    listing = "harness-abc\n/harness-abc-claude\nharness-abc-mcp\nharness-abcdef\nharness-abcdef-codex\n"

    def run(args, **_k):
        removed.append(args)
        return subprocess.CompletedProcess(args, 0, listing if args[:2] == ["docker", "ps"] else "", "")
    monkeypatch.setattr(lane, "_run", run)
    started = {}

    class FakePopen:
        pid = 4242

        def __init__(self, args, cwd=None, env=None, stdout=None, stderr=None, start_new_session=False):
            started.update(args=args, cwd=cwd, session=start_new_session, env=env)
            root = Path(args[args.index("--dir") + 1])
            (root / "instance.json").write_text(json.dumps({"server": "http://127.0.0.1:9", "token": "t"}))
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def wait(self, timeout=None):
            self.alive = False
            return 0
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(lane.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(lane.os, "killpg", lambda pid, sig: killed.append((pid, sig)), raising=False)  # POSIX only
    lane_ = lane.Lane(tmp_path, SHA, tmp_path / "runs" / "R1", tmp_path, broken_fixture=True)
    lane_.sandbox_image = "img"
    with lane_.instance("sandbox", spec={"backends": {"claude": {"enabled": True}}}) as inst:
        assert inst.server == "http://127.0.0.1:9" and inst.token == "t"
        inst.record("abc")
    args = started["args"]
    assert started["session"] and started["cwd"] == lane_.tree and args[2] == "serve"
    assert args[args.index("--image") + 1] == "img" and "--broken-fixture" in args
    # The spec reaches the daemon through its environment, never as a path on its command line.
    assert json.loads(started["env"][lane.SPEC_ENV]) == {"backends": {"claude": {"enabled": True}}}
    assert "--spec" not in args and not any("live-spec" in a for a in args)
    assert killed == [(4242, lane.signal.SIGTERM)]
    gone = sorted(a[3] for a in removed if a[:3] == ["docker", "rm", "-f"])
    # Its own session's sandbox, CLI and relay containers; never another session's, even one whose id starts the same.
    assert gone == ["harness-abc", "harness-abc-claude", "harness-abc-mcp"]
    assert ["docker", "network", "rm", "real-env-r1-sbx"] in removed
    assert [a for a in removed if a[:2] == ["docker", "ps"]] == [
        ["docker", "ps", "-a", "--filter", "name=harness-abc", "--format", "{{.Names}}"]]  # only its own ids


def test_instance_that_never_starts_is_an_error_with_its_log(tmp_path, monkeypatch):
    monkeypatch.setattr(lane, "_run", lambda args, **_k: subprocess.CompletedProcess(args, 0))

    class DeadPopen:
        pid = 1

        def __init__(self, args, stdout=None, **_kwargs):
            stdout.write("Traceback: boom\n")
            stdout.flush()

        def poll(self):
            return 2
    monkeypatch.setattr(lane.subprocess, "Popen", DeadPopen)
    lane_ = lane.Lane(tmp_path, SHA, tmp_path / "runs" / "R1", tmp_path)
    with pytest.raises(RuntimeError, match="did not start"):
        with lane_.instance("gpu"):
            pass


def test_run_end_to_end_without_reporting(tmp_path, monkeypatch, capsys):
    sha = git_repo(tmp_path / "repo")
    monkeypatch.setattr(lane, "sandbox_suite", lambda l: lane.SuiteResult("sandbox", lane.PASSED, "ok"))
    monkeypatch.setattr(lane, "gpu_suite", lambda l: lane.SuiteResult("gpu", lane.SKIPPED, "gpu-held: x"))
    code = lane.main(["run", "--sha", sha, "--repo", str(tmp_path / "repo"), "--base", str(tmp_path / "base"),
                      "--no-report"])
    assert code == 0
    doc = json.loads((tmp_path / "base" / "latest.json").read_text())
    assert doc["sha"] == sha and {k: v["state"] for k, v in doc["suites"].items()} == {"sandbox": "passed",
                                                                                         "gpu": "skipped"}
    assert doc["suites"]["gpu"]["posted"] == "no (--no-report)"
    assert not list((tmp_path / "base" / "runs").glob("*/tree"))  # the extracted tree is removed
    assert json.loads(capsys.readouterr().out)["ok"] is True
    monkeypatch.setattr(lane, "gpu_suite", lambda l: lane.SuiteResult("gpu", lane.FAILED, "boom"))
    assert lane.main(["run", "--sha", sha, "--repo", str(tmp_path / "repo"), "--base", str(tmp_path / "base"),
                      "--no-report", "--suite", "gpu"]) == 1


def test_metrics_fetch_reads_prometheus_text_and_survives_no_answer(monkeypatch):
    import io
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: contextlib.nullcontext(
        io.BytesIO(METRICS.encode())))
    assert lane.fetch_metrics("http://x")['harness_sessions{status="done"}'] == 115

    def down(url, timeout=0):
        raise OSError("refused")
    monkeypatch.setattr(urllib.request, "urlopen", down)
    assert lane.fetch_metrics("http://x") is None


def test_old_run_directories_are_pruned(tmp_path):
    for name in ("20261101T000000Z-a", "20261102T000000Z-b", "20261103T000000Z-c"):
        (tmp_path / "runs" / name).mkdir(parents=True)
    (tmp_path / "latest.json").write_text("{}")
    assert lane.prune_runs(tmp_path, keep=2) == ["20261101T000000Z-a"]
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == ["20261102T000000Z-b", "20261103T000000Z-c"]
    assert (tmp_path / "latest.json").is_file() and lane.prune_runs(tmp_path / "none") == []


@pytest.mark.parametrize("value,pattern", [
    ("origin/main", lane.REV_RE), ("v1.2^", lane.REV_RE), (SHA, lane.SHA_RE),
    (f"ghcr.io/dflippojr/agent-harness-sandbox:sha-{SHA}", lane.IMAGE_RE), ("agent-harness-cli:1", lane.IMAGE_RE),
    ("dflippojr/agent-harness", lane.GITHUB_REPO_RE),
])
def test_well_formed_operator_arguments_pass(value, pattern):
    assert lane.checked(value, pattern, "x") == value


@pytest.mark.parametrize("value,pattern", [
    ("--upload-pack=evil", lane.REV_RE), ("main; rm -rf ~", lane.REV_RE), ("", lane.REV_RE),
    ("abc", lane.SHA_RE), ("-v /:/host", lane.IMAGE_RE), ("img name", lane.IMAGE_RE),
    ("owner/repo --jq x", lane.GITHUB_REPO_RE),
])
def test_an_operator_argument_that_could_be_an_option_stops_the_run(value, pattern):
    with pytest.raises(SystemExit, match="not well-formed"):
        lane.checked(value, pattern, "--flag")


def test_python_and_repo_must_be_what_they_claim(tmp_path):
    assert lane.checked_python(sys.executable) == str(Path(sys.executable))
    for bad in ("python3", str(tmp_path / "missing"), str(tmp_path)):
        with pytest.raises(SystemExit, match="--python"):
            lane.checked_python(bad)
    with pytest.raises(SystemExit, match="not a git checkout"):
        lane.checked_repo(str(tmp_path))


def test_run_refuses_malformed_arguments_before_running_anything(tmp_path):
    sha = git_repo(tmp_path / "repo")
    base = ["run", "--repo", str(tmp_path / "repo"), "--base", str(tmp_path / "base"), "--no-report"]
    for extra, message in ((["--sha=--output=/etc/x"], "--sha"), (["--sha", sha, "--workers", "0"], "--workers"),
                           (["--sha", sha, "--sandbox-image=-v/:/h"], "--sandbox-image"),
                           (["--sha", sha, "--github-repo", "a/b c"], "--github-repo")):
        with pytest.raises(SystemExit, match=message):
            lane.main(base + extra)
    assert not (tmp_path / "base" / "runs").exists()


def test_gpu_suite_yields_to_a_live_session_that_arrives_during_the_checks(tmp_path, idle, monkeypatch):
    readings = iter([metrics(), metrics(**{'harness_sessions{status="running"}': 1})])
    monkeypatch.setattr(lane, "fetch_metrics", lambda url=lane.LIVE_METRICS: next(readings))
    run = FakeLane(tmp_path)
    run.instance = lambda *a, **k: pytest.fail("no session after a live one started")
    result = lane.gpu_suite(run)
    assert result.state == lane.SKIPPED and result.summary.startswith("busy: 1 live session(s) started")
    assert "session not run" in result.summary


def test_gpu_suite_fails_when_no_gpu_check_ran(tmp_path, idle):
    events = [{"type": "tool_result", "name": "write_file", "ok": True, "output": "wrote lane.txt"}]
    result = lane.gpu_suite(FakeLane(tmp_path, tests=lane.PytestRun(5), client=FakeClient(events=events)))
    assert result.state == lane.FAILED and "at least one must run" in result.summary


def test_docker_marked_tests_never_start_maintenance():
    """Maintenance removes session containers its own database doesn't know. On the deploy host the lane runs the
    docker-marked tests beside the live instance, so none of them may start a Manager with maintenance on (#731)."""
    import ast
    offenders = []
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not any(
                    "docker" in ast.unparse(d) for d in fn.decorator_list):
                continue
            for call in ast.walk(fn):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "start"
                        and isinstance(call.func.value, ast.Name) and call.func.value.id in ("m", "manager")
                        and not any(k.arg == "maintenance" and isinstance(k.value, ast.Constant)
                                    and k.value.value is False for k in call.keywords)):
                    offenders.append(f"{path.name}:{call.lineno} {fn.name}")
    assert offenders == []


def test_hosted_suite_never_probes_the_shared_logins(tmp_path, idle, monkeypatch):
    seen = []

    class Recording(FakeClient):
        def get(self, path, params=None):
            if path == "/api/v1/backends":
                seen.append(params)
            return super().get(path, params)
    monkeypatch.setattr(lane, "drive_session", lambda *a, **k: timed_run(0, 1))
    lane.hosted_suite(FakeLane(tmp_path, client=Recording(backends=BACKENDS), force_hosted=True,
                               live=hosted_live(tmp_path)))
    assert seen and all(p == {"auth": "skip"} for p in seen)


def test_a_failed_gpu_check_stays_failed_when_the_lane_yields(tmp_path, idle, monkeypatch):
    readings = iter([metrics(), metrics(harness_gpu_manual_hold_age_seconds=3)])
    monkeypatch.setattr(lane, "fetch_metrics", lambda url=lane.LIVE_METRICS: next(readings))
    result = lane.gpu_suite(FakeLane(tmp_path, tests=lane.PytestRun(1, passed=1, failed=1)))
    assert result.state == lane.FAILED and "session not run" in result.summary


def test_sandbox_suite_yields_before_its_session(tmp_path, idle, monkeypatch):
    busy = metrics(**{'harness_sessions{status="queued"}': 1})
    monkeypatch.setattr(lane, "fetch_metrics", lambda url=lane.LIVE_METRICS: busy)
    run = FakeLane(tmp_path)
    run.instance = lambda *a, **k: pytest.fail("no session after a live one arrived")
    result = lane.sandbox_suite(run)
    assert result.state == lane.SKIPPED and result.summary.startswith("busy: 1 live session(s)")
    result = lane.sandbox_suite(FakeLane(tmp_path, tests=lane.PytestRun(1, passed=2, failed=1)))
    assert result.state == lane.FAILED   # a failure found before yielding still counts


def test_hosted_suite_yields_between_sessions(tmp_path, idle, monkeypatch):
    readings = iter([metrics(), metrics(**{'harness_sessions{status="running"}': 1})] + [metrics()] * 5)
    monkeypatch.setattr(lane, "fetch_metrics", lambda url=lane.LIVE_METRICS: next(readings))
    sessions = []
    monkeypatch.setattr(lane, "drive_session", lambda inst, prompt, backend=None, **k: sessions.append(backend)
                        or timed_run(0, 1))
    result = lane.hosted_suite(FakeLane(tmp_path, client=FakeClient(backends=BACKENDS), force_hosted=True,
                                        live=hosted_live(tmp_path), hosted_opt_in=("codex",)))
    assert sessions == ["claude"] and "codex: not run, a live session started" in result.details["notes"]
    every_one = iter([metrics(**{'harness_sessions{status="running"}': 1})] * 5)
    monkeypatch.setattr(lane, "fetch_metrics", lambda url=lane.LIVE_METRICS: next(every_one))
    result = lane.hosted_suite(FakeLane(tmp_path, client=FakeClient(backends=BACKENDS), force_hosted=True,
                                        live=hosted_live(tmp_path)))
    assert result.state == lane.SKIPPED and result.summary.startswith("busy:")


def test_only_one_lane_run_at_a_time(tmp_path):
    import importlib.util
    with lane.one_run_at_a_time(tmp_path):
        if importlib.util.find_spec("fcntl") is not None:   # POSIX, where the lane runs; Windows has no flock
            with pytest.raises(SystemExit, match="another real-environment lane run"):
                with lane.one_run_at_a_time(tmp_path):
                    pass
    with lane.one_run_at_a_time(tmp_path):  # released after the run
        pass
