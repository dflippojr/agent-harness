"""PR Sonar must fail on issues the PR introduces, not on a New Code date window."""

from __future__ import annotations

import base64
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "sonar.yml"
SPEC = importlib.util.spec_from_file_location("sonar_pr_gate", ROOT / "scripts" / "sonar_pr_gate.py")
gate = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(gate)

PROJECT = "agent-harness-pr-119"


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def pr_block() -> str:
    text = workflow_text()
    start = text.index("if ($env:EVENT_NAME -eq 'pull_request')")
    else_at = text.index("} else {", start)
    return text[start:else_at]


def issue(rule: str, path: str, message: str, *, key: str = "ISSUEKEY", line: int = 1,
          itype: str = "CODE_SMELL") -> dict:
    return {"key": key, "rule": rule, "component": f"{PROJECT}:{path}", "line": line,
            "message": message, "type": itype}


def hotspot(rule: str, path: str, message: str, *, key: str = "HSKEY", line: int = 1) -> dict:
    return {"key": key, "ruleKey": rule, "component": f"{PROJECT}:{path}", "line": line,
            "message": message, "status": "TO_REVIEW"}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1


class FakeSonar:
    def __init__(self) -> None:
        self.token = "test-token"
        self.issues: list[dict] = []
        self.hotspots: list[dict] = []
        self.activity_pages: list[dict | int] = []
        self.http_codes: dict[str, int] = {}
        self.calls: list[tuple[str, dict]] = []
        self.host = ""
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> "FakeSonar":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:  # noqa: A003 - BaseHTTPRequestHandler API
                return

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
                fake.calls.append((parsed.path, query))
                expected = "Basic " + base64.b64encode(f"{fake.token}:".encode("ascii")).decode("ascii")
                if self.headers.get("Authorization") != expected:
                    self._send(401, {"errors": [{"msg": "auth"}]})
                    return
                if parsed.path in fake.http_codes:
                    self._send(fake.http_codes[parsed.path], {"errors": [{"msg": "forced"}]})
                    return
                if parsed.path == "/api/ce/activity":
                    if not fake.activity_pages:
                        self._send(200, {"tasks": []})
                        return
                    page = fake.activity_pages.pop(0)
                    if isinstance(page, int):
                        self._send(page, {"errors": [{"msg": "missing"}]})
                        return
                    self._send(200, page)
                    return
                if parsed.path == "/api/issues/search":
                    self._send_page(fake.issues, "issues", query)
                    return
                if parsed.path == "/api/hotspots/search":
                    self._send_page(fake.hotspots, "hotspots", query)
                    return
                self._send(404, {"errors": [{"msg": "nope"}]})

            def _send_page(self, items: list[dict], key: str, query: dict) -> None:
                if query.get("ps") != "500":
                    self._send(400, {"errors": [{"msg": f"ps must be 500, got {query.get('ps')}"}]})
                    return
                page = int(query.get("p") or "1")
                start = (page - 1) * gate.PAGE_SIZE
                batch = items[start:start + gate.PAGE_SIZE]
                self._send(200, {key: batch, "paging": {"pageIndex": page, "pageSize": gate.PAGE_SIZE,
                                                       "total": len(items)}})

            def _send(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server = server
        self.host = f"http://127.0.0.1:{server.server_address[1]}"
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
def sonar():
    fake = FakeSonar().start()
    yield fake
    fake.stop()


def test_pr_sonar_does_not_pin_new_code_to_baseline_analysis_timestamp():
    """A baseline scan taken during the PR check is newer than the PR's commits.

    Sonar New Code of that kind is a date window, so SCM blame would keep
    new_violations at 0 even when the PR adds a real smell.
    """
    text = workflow_text()
    pr = pr_block()
    assert "SPECIFIC_ANALYSIS" not in text
    assert "new_code_periods" not in text
    assert "sonar.qualitygate.wait=true" not in text
    assert '"-Dsonar.qualitygate.wait=false"' in text
    assert "Invoke-Sonar $key $name $baseVer" in pr
    assert "Invoke-Sonar $key $name $prVer" in pr
    assert "scripts\\sonar_pr_gate.py" in pr
    assert pr.count("'collect'") == 2
    assert "'diff'" in pr


def test_pr_sonar_workflow_keeps_isolation_and_self_decides_verdict():
    text = workflow_text()
    pr = pr_block()
    assert "group: sonar-${{ github.event.pull_request.number || github.ref }}" in text
    assert "continue-on-error: ${{ github.event_name != 'pull_request' }}" in text
    assert "sonarsource/sonar-scanner-cli:11" in text
    assert "runs-on: [self-hosted, Windows, X64, agent-harness-tower]" in text
    assert "agent-harness-pr-$($env:PR_NUMBER)" in pr
    assert "git checkout --force --detach $env:BASE_SHA" in pr
    assert "git checkout --force --detach $env:HEAD_SHA" in pr
    assert pr.index("Copy-Item") < pr.index("git checkout --force --detach $env:BASE_SHA")
    assert pr.index("git checkout --force --detach $env:BASE_SHA") < pr.index(
        "git checkout --force --detach $env:HEAD_SHA"
    )
    assert pr.index("'collect'") < pr.index("'diff'")
    main = text[text.index("} else {"):]
    assert '"-Dsonar.projectKey=agent-harness"' in main
    assert "sonar.qualitygate.wait=false" in main
    assert "qualitygate.wait=true" not in main


def test_issue_fingerprint_ignores_key_and_line():
    left = issue("python:S1", "harness/a.py", "nested if", key="AAA", line=10)
    right = issue("python:S1", "harness/a.py", "nested if", key="BBB", line=80)
    assert gate.issue_fingerprint(left, PROJECT) == gate.issue_fingerprint(right, PROJECT)
    assert gate.issue_fingerprint(left, PROJECT) == (
        "issue", "python:S1", "harness/a.py", "nested if"
    )


def test_security_hotspots_in_issues_search_are_not_double_counted():
    row = issue("python:S2245", "harness/a.py", "random", itype="SECURITY_HOTSPOT")
    assert gate.issue_fingerprint(row, PROJECT) is None


def test_new_findings_count_multiplicities_and_ignore_leftovers():
    leftover = {"kind": "issue", "rule": "python:S1", "path": "harness/old.py", "message": "leftover on main"}
    smell = {"kind": "issue", "rule": "python:S2", "path": "harness/new.py", "message": "added by the PR"}
    hotspot_row = {"kind": "hotspot", "rule": "python:S2245", "path": "harness/new.py",
                   "message": "Make sure that using this pseudorandom number generator is safe here."}
    baseline = [leftover, leftover]
    head = [leftover, leftover, smell, hotspot_row]
    extra = gate.new_findings(baseline, head)
    assert extra == [hotspot_row, smell]
    assert gate.new_findings([leftover], [leftover, leftover]) == [leftover]
    # Force-push that only drops a leftover must pass.
    assert gate.new_findings(baseline, [leftover]) == []
    # First-ever / re-run with the same unresolved set must pass.
    assert gate.new_findings(baseline, [leftover, leftover]) == []


def test_diff_cli_fails_only_when_head_has_keys_absent_from_baseline(tmp_path, capsys):
    baseline = tmp_path / "base.json"
    head = tmp_path / "head.json"
    leftover = {"kind": "issue", "rule": "python:S1", "path": "a.py", "message": "old"}
    added = {"kind": "issue", "rule": "python:S2", "path": "b.py", "message": "new smell"}
    baseline.write_text(json.dumps([leftover]), encoding="utf-8")
    head.write_text(json.dumps([leftover]), encoding="utf-8")
    assert gate.main(["diff", "--baseline", str(baseline), "--head", str(head)]) == 0
    assert "No new Sonar issues" in capsys.readouterr().out
    head.write_text(json.dumps([leftover, added]), encoding="utf-8")
    assert gate.main(["diff", "--baseline", str(baseline), "--head", str(head)]) == 1
    out = capsys.readouterr().out
    assert "New Sonar findings introduced by this PR (1):" in out
    assert "[issue] python:S2 b.py: new smell" in out


def test_latest_id_empty_on_first_ever_project(sonar, monkeypatch):
    monkeypatch.setenv("SONAR_TOKEN", sonar.token)
    sonar.activity_pages = [404]
    assert gate.latest_task_id(sonar.host, sonar.token, PROJECT) == ""
    assert gate.main(["latest-id", "--host", sonar.host, "--project", PROJECT]) == 0


def test_wait_for_new_report_skips_previous_task_on_rerun(sonar):
    clock = FakeClock()
    previous = {"id": "task-old", "type": "REPORT", "status": "SUCCESS"}
    older = {"id": "task-older", "type": "REPORT", "status": "SUCCESS"}
    pending = {"id": "task-new", "type": "REPORT", "status": "IN_PROGRESS"}
    done = {"id": "task-new", "type": "REPORT", "status": "SUCCESS"}
    sonar.activity_pages = [
        {"tasks": [previous, older]},
        {"tasks": [pending, previous, older]},
        {"tasks": [done, previous, older]},
    ]
    task = gate.wait_for_new_report(sonar.host, sonar.token, PROJECT, "task-old",
                                    timeout=20, sleeper=clock.sleep, clock=clock.monotonic)
    assert task["id"] == "task-new"
    assert clock.sleeps == 2


def test_wait_times_out_rather_than_returning_an_old_success(sonar):
    clock = FakeClock()
    sonar.activity_pages = [{"tasks": [{"id": "task-old", "type": "REPORT", "status": "SUCCESS"}]}] * 20
    with pytest.raises(gate.GateError, match="timed out"):
        gate.wait_for_new_report(sonar.host, sonar.token, PROJECT, "task-old",
                                 timeout=6, sleeper=clock.sleep, clock=clock.monotonic)


def test_collect_paginates_issues_and_hotspots(sonar, monkeypatch, tmp_path):
    monkeypatch.setenv("SONAR_TOKEN", sonar.token)
    sonar.activity_pages = [{"tasks": [{"id": "task-1", "type": "REPORT", "status": "SUCCESS"}]}]
    sonar.issues = [issue("python:S1", f"harness/f{i}.py", "dup", key=f"I{i}", line=i)
                    for i in range(gate.PAGE_SIZE + 1)]
    sonar.hotspots = [hotspot("python:S2245", f"harness/h{i}.py", "rand", key=f"H{i}", line=i)
                      for i in range(2)]
    out = tmp_path / "snap.json"
    assert gate.main(["collect", "--host", sonar.host, "--project", PROJECT, "--after-id=",
                      "--out", str(out), "--timeout", "5"]) == 0
    rows = json.loads(out.read_text(encoding="utf-8"))
    assert len(rows) == gate.PAGE_SIZE + 3
    issue_calls = [query for path, query in sonar.calls if path == "/api/issues/search"]
    hotspot_calls = [query for path, query in sonar.calls if path == "/api/hotspots/search"]
    assert [query["p"] for query in issue_calls] == ["1", "2"]
    assert [query["ps"] for query in issue_calls] == ["500", "500"]
    assert query_has_unresolved(issue_calls[0])
    assert [query["p"] for query in hotspot_calls] == ["1"]
    assert hotspot_calls[0]["status"] == "TO_REVIEW"


def query_has_unresolved(query: dict) -> bool:
    return query.get("resolved") == "false" and query.get("componentKeys") == PROJECT


def test_collect_fails_closed_when_issues_api_errors(sonar, monkeypatch, tmp_path):
    monkeypatch.setenv("SONAR_TOKEN", sonar.token)
    sonar.activity_pages = [{"tasks": [{"id": "task-1", "type": "REPORT", "status": "SUCCESS"}]}]
    sonar.http_codes["/api/issues/search"] = 500
    assert gate.main(["collect", "--host", sonar.host, "--project", PROJECT, "--out", str(tmp_path / "x.json"),
                      "--timeout", "5"]) == 2


def test_failed_analysis_fails_the_gate(sonar):
    sonar.activity_pages = [{"tasks": [{"id": "task-bad", "type": "REPORT", "status": "FAILED"}]}]
    with pytest.raises(gate.GateError, match="task-bad FAILED"):
        gate.wait_for_new_report(sonar.host, sonar.token, PROJECT, "")
