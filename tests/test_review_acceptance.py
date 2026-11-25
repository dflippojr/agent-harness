"""The automated review's acceptance metric (#1011, step 1): parsing review comments, judging later diffs, reporting."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from scripts import review_acceptance as ra  # noqa: E402

REPO = "o/r"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_M = "d" * 40
SHA_X = "e" * 40

# PR #1086's first automated review, as GitHub serves it (CRLF in the job's own lines).
REVIEW_1086 = (
    "Reviewed the full diff\r\n\r\n## Acceptance criteria\n\n"
    "**Not met** — “newest `finished_at` of a `complete` canary row, in Unix seconds”: "
    "`harness/metrics.py` reuses a helper in `harness/canary.py`.\n\n"
    "**Met** — “With no complete row, the gauge is absent (not 0)”: `tests/test_canary_silence.py`.\n\n"
    "## Findings\n\n"
    "- harness/metrics.py:278 — **Unmet criterion**: “newest `finished_at` of a `complete` canary row.” "
    "`last_complete_at()` additionally requires `pass_rate IS NOT NULL`.\n\n"
    "## Style and structure (advisory)\n\n"
    "No style or structure suggestions worth a human’s time.\r\n\r\n---\r\n"
    "Automated review backend: **codex (gpt-6.1-sol, xhigh)**.\r\n\r\n"
    f"<!-- agent-review: sha={SHA_A} mode=full base=main -->\r\n"
    f"<!-- agent-review: sha={SHA_A} criteria=3/4 findings=1 -->"
)


def review_body(findings: str, advisory: str = "No suggestions.", backend: str = "claude (opus, high)",
                sha: str = SHA_A) -> str:
    return (f"Reviewed the full diff\r\n\r\n## Acceptance criteria\n\n**Met** — x\n\n## Findings\n\n{findings}\n\n"
            f"## Style and structure (advisory)\n\n{advisory}\r\n\r\n---\r\n"
            f"Automated review backend: **{backend}**.\r\n\r\n<!-- agent-review: sha={sha} mode=full -->")


# ------------------------------------------------------------------------------------------------ parsing


def test_parses_the_real_review_comment_from_pr_1086():
    review = ra.parse_review(REVIEW_1086)
    assert review is not None
    assert (review.sha, review.backend, review.model) == (SHA_A, "codex", "gpt-6.1-sol, xhigh")
    assert review.findings == [ra.Finding("blocking", "unmet criterion", "harness/metrics.py", 278, 278)]
    assert review.findings[0].granularity == "line"


def test_a_comment_without_the_job_marker_is_not_a_review():
    assert ra.parse_review(REVIEW_1086.split("<!--")[0]) is None
    quoted = REVIEW_1086.replace(f"sha={SHA_A} mode=full", f"sha={SHA_A} mode=other")
    assert ra.parse_review(quoted.replace("criteria=3/4", "x")) is None


def test_a_review_without_a_backend_footer_has_an_unknown_backend():
    body = REVIEW_1086.replace("Automated review backend: **codex (gpt-6.1-sol, xhigh)**.", "")
    review = ra.parse_review(body)
    assert review is not None and (review.backend, review.model) == ("unknown", "")
    bare = ra.parse_review(REVIEW_1086.replace("codex (gpt-6.1-sol, xhigh)", "cursor"))
    assert bare is not None and (bare.backend, bare.model) == ("cursor", "")


def test_no_significant_findings_is_no_finding():
    assert ra.parse_findings(review_body("No significant findings.")) == []
    assert ra.parse_findings(review_body("- No significant findings.")) == []


def test_finding_types_locations_and_nested_bullets():
    findings = ra.parse_findings(review_body(
        "- `harness/web_tools.py:464` **Unmet criterion**: quoted.\n"
        "- `harness/web_tools.py:440`: a redirect can crash the run. The failure path:\n"
        "  - A server replies 302 with a bad port.\n"
        "  - It gets past the handler.\n"
        "1. ./harness/a.py:10-12 — **Untested fix**: no test.\n"
        "- harness\\b.py:7-3 **Weakened test**: backwards range.\n"
        "- **Spec**, **Unmet criterion**: \"The PR text describes the functional change only.\"\n"
        "- **Spec** — the description claims a test that does not exist.\n"
        "- **Proof contradicted**: the Proof row cites no run.\n"
        "- a.py:5 \u2014 **Unmet criterion / Proof contradicted**: both.\n"
        "- a.py:6 \u2014 **Unmet criterion; proof contradicted**: both.\n"
        "- Something with no label and no location.\n"
        "```\n- harness/fenced.py:1 — quoted from the diff\n```"
    ))
    assert findings == [
        ra.Finding("blocking", "unmet criterion", "harness/web_tools.py", 464, 464),
        ra.Finding("blocking", "correctness", "harness/web_tools.py", 440, 440),
        ra.Finding("blocking", "untested fix", "harness/a.py", 10, 12),
        ra.Finding("blocking", "weakened test", "harness/b.py", 7, 7),
        ra.Finding("blocking", "unmet criterion", None),
        ra.Finding("blocking", "spec", None),
        ra.Finding("blocking", "proof contradicted", None),
        ra.Finding("blocking", "unmet criterion", "a.py", 5, 5),
        ra.Finding("blocking", "unmet criterion", "a.py", 6, 6),
        ra.Finding("blocking", "unclassified", None),
    ]
    assert findings[4].granularity == "none"


def test_advisory_suggestions_are_file_level():
    findings = ra.parse_findings(review_body(
        "No significant findings.",
        advisory=("**Structure** — `tests/test_api.py`, near the new test: exercise a stored approval.\n"
                  "- **Style**: `./harness/x.py`, near `load()`: rename it.\n"
                  "**Style** with no file named.\n"
                  "Plain prose naming `harness/y.py`."),
    ))
    assert findings == [ra.Finding("advisory", "structure", "tests/test_api.py"),
                        ra.Finding("advisory", "style", "harness/x.py")]
    assert findings[0].granularity == "file"


def test_only_the_first_findings_heading_counts():
    body = review_body("- a.py:1 — real") + "\n## Findings\n- b.py:2 — quoted later\n"
    assert [f.path for f in ra.parse_findings(body)] == ["a.py"]
    assert ra.parse_findings("no headings at all") == []


# ------------------------------------------------------------------------------------------------ patches


PATCH = (
    "@@ -8,7 +8,8 @@ def f():\n"
    " line8\n"
    " line9\n"
    "-line10\n"
    "+line10 changed\n"
    "+inserted after 10\n"
    " line11\n"
    " line12\n"
    " line13\n"
    " line14\n"
    "@@ -30,2 +30,0 @@\n"
    "-line30\n"
    "-line31\n"
    "\\ No newline at end of file\n"
)


def test_check_patch_cited_lines():
    assert ra.check_patch(PATCH, 10, 10) == ra.LineCheck(True)  # changed
    assert ra.check_patch(PATCH, 11, 11) == ra.LineCheck(True)  # a line inserted directly above it
    assert ra.check_patch(PATCH, 9, 9) == ra.LineCheck(False, 9, 9)  # a change below it does not count
    assert ra.check_patch(PATCH, 9, 10) == ra.LineCheck(True)  # a range counts when any of its lines changed


def test_check_patch_follows_untouched_lines_into_the_new_file():
    assert ra.check_patch(PATCH, 3, 4) == ra.LineCheck(False, 3, 4)  # before any hunk
    assert ra.check_patch(PATCH, 12, 13) == ra.LineCheck(False, 13, 14)  # inside the hunk as context
    assert ra.check_patch(PATCH, 20, 22) == ra.LineCheck(False, 21, 23)  # between hunks
    assert ra.check_patch(PATCH, 30, 30) == ra.LineCheck(True)  # deleted at the end
    assert ra.check_patch(PATCH, 40, 41) == ra.LineCheck(False, 39, 40)  # after both hunks


def test_check_patch_window_and_pure_insertions():
    assert ra.check_patch(PATCH, 13, 13, window=1) == ra.LineCheck(False, 14, 14)
    assert ra.check_patch(PATCH, 13, 13, window=2) == ra.LineCheck(True)  # reaches the insertion above line 11
    insert = "@@ -10,0 +11,2 @@\n+a\n+b\n"  # two lines inserted after old line 10
    assert ra.check_patch(insert, 10, 10) == ra.LineCheck(True)
    assert ra.check_patch(insert, 11, 11) == ra.LineCheck(True)
    assert ra.check_patch(insert, 9, 9) == ra.LineCheck(False, 9, 9)
    assert ra.check_patch(insert, 12, 12) == ra.LineCheck(False, 14, 14)
    assert ra.check_patch("garbage before a header\n" + insert, 1, 1) == ra.LineCheck(False, 1, 1)


def entry(filename: str, status: str = "modified", patch: str | None = PATCH, **extra) -> dict:
    data = {"filename": filename, "status": status, "changes": 3, **extra}
    if patch is not None:
        data["patch"] = patch
    return data


def test_step_location_through_one_file_list():
    step = ra.step_location
    assert step([entry("a.py")], False, "a.py", 10, 10, 0).outcome == "touched"
    assert step([entry("a.py")], False, "a.py", 20, 20, 0) == ra.FileStep("untouched", "a.py", 21, 21)
    assert step([entry("b.py")], False, "a.py", 10, 10, 0) == ra.FileStep("untouched", "a.py", 10, 10)
    assert step([entry("b.py")], True, "a.py", 10, 10, 0).outcome == "unknown"
    assert step([entry("a.py", "removed", None)], False, "a.py", 1, 1, 0).outcome == "touched"
    missing = step([entry("a.py", patch=None)], False, "a.py", 1, 1, 0)
    assert (missing.outcome, missing.reason) == ("unknown", "GitHub omitted the file's patch")
    moved = entry("c.py", "renamed", None, previous_filename="a.py", changes=0)
    assert step([moved], False, "a.py", 5, 6, 0) == ra.FileStep("untouched", "c.py", 5, 6)
    assert step([moved], False, "a.py", None, None, 0) == ra.FileStep("untouched", "c.py")
    edited = entry("c.py", "renamed", previous_filename="a.py")
    assert step([edited], False, "a.py", 10, 10, 0) == ra.FileStep("touched", "c.py")
    assert step([edited], False, "a.py", None, None, 0) == ra.FileStep("touched", "c.py")
    copied = entry("c.py", "copied", previous_filename="a.py")
    assert step([copied], False, "a.py", 10, 10, 0) == ra.FileStep("untouched", "a.py", 10, 10)
    assert step([entry("a.py")], False, "a.py", None, None, 0) == ra.FileStep("touched", "a.py")


# ---------------------------------------------------------------------------------------------- outcomes


class FakeGitHub:
    """Serves canned REST answers by path; a missing path is a 404, a listed exception is raised."""

    def __init__(self, answers: dict[str, object]):
        self.answers = answers
        self.calls: list[str] = []

    def __call__(self, path: str) -> object:
        self.calls.append(path)
        if path not in self.answers:
            raise ra.GitHubError(f"HTTP 404 {path}", missing=True)
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer


def compare_path(base: str, head: str) -> str:
    return f"repos/{REPO}/compare/{base}...{head}"


def commit(sha: str, *parents: str) -> dict:
    return {"sha": sha, "parents": [{"sha": p} for p in parents]}


LINE = ra.Finding("blocking", "correctness", "a.py", 10, 10)


def test_outcomes_for_a_linear_range():
    fake = FakeGitHub({
        compare_path(SHA_A, SHA_B): {"status": "ahead", "ahead_by": 1, "total_commits": 1,
                                     "commits": [commit(SHA_B, SHA_A)], "files": [entry("a.py")]},
        compare_path(SHA_A, SHA_A): {"status": "identical", "ahead_by": 0, "commits": [], "files": []},
        compare_path(SHA_C, SHA_B): {"status": "diverged", "ahead_by": 1, "commits": [], "files": []},
        compare_path(SHA_X, SHA_B): {"status": "ahead", "ahead_by": 300, "total_commits": 300,
                                     "commits": [commit(SHA_B, SHA_A)], "files": []},
    })
    m = ra.Measurer(REPO, fake)
    assert m.outcome(LINE, SHA_A, SHA_B) == (ra.ACCEPTED, "")
    assert m.outcome(ra.Finding("blocking", "correctness", "a.py", 20, 20), SHA_A, SHA_B) == (ra.NOT_ACCEPTED, "")
    assert m.outcome(LINE, SHA_A, SHA_A) == (ra.NOT_ACCEPTED, "no commit after the review")
    assert m.outcome(LINE, SHA_C, SHA_B)[0] == ra.UNKNOWN
    assert m.outcome(LINE, SHA_M, SHA_B)[0] == ra.UNKNOWN  # the reviewed commit is gone (404)
    assert m.outcome(LINE, SHA_X, SHA_B) == (ra.UNKNOWN, "more commits after the review than one compare lists")
    assert m.outcome(ra.Finding("blocking", "spec", None), SHA_A, SHA_B)[0] == ra.UNLOCATED
    m.outcome(LINE, SHA_A, SHA_B)
    assert fake.calls.count(compare_path(SHA_A, SHA_B)) == 1  # cached
    assert fake.calls.count(compare_path(SHA_M, SHA_B)) == 1  # a 404 is cached too


def test_other_github_failures_propagate():
    fake = FakeGitHub({compare_path(SHA_A, SHA_B): ra.GitHubError("HTTP 502", missing=False)})
    with pytest.raises(ra.GitHubError):
        ra.Measurer(REPO, fake).outcome(LINE, SHA_A, SHA_B)


def merged_range(files_m: list[dict], files_c: list[dict]) -> dict[str, object]:
    """reviewed A -> merge M (first parent A, second parent X from main) -> fix commit C."""
    return {
        compare_path(SHA_A, SHA_C): {"status": "ahead", "ahead_by": 3, "total_commits": 3,
                                     "commits": [commit(SHA_X, "f" * 40), commit(SHA_M, SHA_A, SHA_X),
                                                 commit(SHA_C, SHA_M)],
                                     "files": [entry("a.py")]},
        compare_path(SHA_A, SHA_M): {"status": "ahead", "files": files_m},
        compare_path(SHA_M, SHA_C): {"status": "ahead", "files": files_c},
    }


def test_a_merge_from_the_base_is_walked_past_and_never_counts():
    shift_two = "@@ -0,0 +1,2 @@\n+x\n+y\n"
    # Main's merge inserts two lines at the top; the later fix edits old line 10, now line 12.
    fix_at_12 = "@@ -12 +12 @@\n-old\n+new\n"
    m = ra.Measurer(REPO, FakeGitHub(merged_range([entry("a.py", patch=shift_two)], [entry("a.py", patch=fix_at_12)])))
    assert m.outcome(LINE, SHA_A, SHA_C) == (ra.ACCEPTED, "")
    # The fix edits line 10 of the merged file, which is old line 8: not the cited line.
    fix_at_10 = "@@ -10 +10 @@\n-old\n+new\n"
    m = ra.Measurer(REPO, FakeGitHub(merged_range([entry("a.py", patch=shift_two)], [entry("a.py", patch=fix_at_10)])))
    assert m.outcome(LINE, SHA_A, SHA_C) == (ra.NOT_ACCEPTED, "")
    # The single compare would have said accepted, because main's change to line 10 is in it.
    main_edit = "@@ -10 +10 @@\n-old\n+main\n"
    m = ra.Measurer(REPO, FakeGitHub(merged_range([entry("a.py", patch=main_edit)], [])))
    assert m.outcome(LINE, SHA_A, SHA_C) == (ra.UNKNOWN, "a merge from the base branch changed the cited lines")
    m = ra.Measurer(REPO, FakeGitHub(merged_range([entry("a.py", patch=None)], [])))
    assert m.outcome(LINE, SHA_A, SHA_C) == (ra.UNKNOWN, "GitHub omitted the file's patch")
    # An advisory suggestion follows a rename through the merge and counts a later edit of the new name.
    moved = entry("b.py", "renamed", None, previous_filename="a.py", changes=0)
    m = ra.Measurer(REPO, FakeGitHub(merged_range([moved], [entry("b.py")])))
    assert m.outcome(ra.Finding("advisory", "style", "a.py"), SHA_A, SHA_C) == (ra.ACCEPTED, "")


def test_a_walk_that_cannot_reach_the_reviewed_commit_is_unknown():
    answers = merged_range([], [])
    del answers[compare_path(SHA_M, SHA_C)]
    m = ra.Measurer(REPO, FakeGitHub(answers))
    assert m.outcome(LINE, SHA_A, SHA_C) == (ra.UNKNOWN, "GitHub cannot compare one of the commits after the review")
    answers = merged_range([], [])
    answers[compare_path(SHA_B, SHA_C)] = answers[compare_path(SHA_A, SHA_C)]
    m = ra.Measurer(REPO, FakeGitHub(answers))
    assert m.outcome(LINE, SHA_B, SHA_C)[1] == "the reviewed commit is not on the merged head's first-parent chain"


# --------------------------------------------------------------------------------------- the whole run


def pull(number: int, merged_at: str | None, head: str = SHA_B) -> dict:
    return {"number": number, "merged_at": merged_at, "head": {"sha": head}}


def comment(body: str, created_at: str = "2026-11-20T00:00:00Z", login: str = ra.REVIEW_LOGIN) -> dict:
    return {"user": {"login": login}, "body": body, "created_at": created_at}


def fake_repo() -> FakeGitHub:
    review = review_body(
        "- a.py:10 — **Unmet criterion**: x\n- a.py:20 — a bug\n- **Spec** — y",
        advisory="**Style** — `b.py`, near the top: rename.",
    )
    second = review_body("- a.py:3 — later round", backend="codex (gpt, xhigh)", sha=SHA_B)
    return FakeGitHub({
        f"repos/{REPO}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=1": [
            pull(7, "2026-11-21T00:00:00Z"), pull(8, None), pull(9, "2026-11-22T00:00:00Z", SHA_A),
        ],
        f"repos/{REPO}/issues/7/comments?per_page=100&page=1": [
            comment(second, "2026-11-20T12:00:00Z"),
            comment(review),
            comment(review, login="someone"),
            comment("Thanks!"),
            comment(review, "2026-11-23T00:00:00Z"),  # after the merge
        ],
        f"repos/{REPO}/issues/9/comments?per_page=100&page=1": [],
        compare_path(SHA_A, SHA_B): {"status": "ahead", "ahead_by": 1, "total_commits": 1,
                                     "commits": [commit(SHA_B, SHA_A)], "files": [entry("a.py"), entry("b.py")]},
        compare_path(SHA_B, SHA_B): {"status": "identical", "ahead_by": 0, "commits": [], "files": []},
    })


def test_measure_reads_merged_prs_and_counts_each_round():
    report = ra.measure(REPO, 5, fake_repo(), now=datetime(2026, 11, 25, tzinfo=timezone.utc))
    assert report.generated_at == "2026-11-25T00:00:00Z"
    assert report.pulls == [{"number": 9, "merged_at": "2026-11-22T00:00:00Z", "reviews": 0},
                            {"number": 7, "merged_at": "2026-11-21T00:00:00Z", "reviews": 2}]
    assert report.skipped_after_merge == 1
    rows = [(r.round, r.backend, r.severity, r.type, r.path, r.start, r.outcome) for r in report.results]
    assert rows == [
        (1, "claude", "blocking", "unmet criterion", "a.py", 10, ra.ACCEPTED),
        (1, "claude", "blocking", "correctness", "a.py", 20, ra.NOT_ACCEPTED),
        (1, "claude", "blocking", "spec", None, None, ra.UNLOCATED),
        (1, "claude", "advisory", "style", "b.py", None, ra.ACCEPTED),
        (2, "codex", "blocking", "correctness", "a.py", 3, ra.NOT_ACCEPTED),
    ]
    assert ra.measure(REPO, 1, fake_repo()).pulls[0]["number"] == 9


def test_report_tables_per_backend_severity_and_type():
    report = ra.measure(REPO, 5, fake_repo(), now=datetime(2026, 11, 25, tzinfo=timezone.utc))
    text = ra.render_markdown(report)
    assert "last 5 by merge time (2 found, merged 2026-11-21 to 2026-11-22), 1 with automated reviews, 2 review rounds" in text
    assert "| all findings | 5 | 2 | 2 | 0 | 1 | 50.0% |" in text
    assert "| claude | 4 | 2 | 1 | 0 | 1 | 66.7% |" in text
    assert "| codex | 1 | 0 | 1 | 0 | 0 | 0.0% |" in text
    assert "| advisory (file level) | 1 | 1 | 0 | 0 | 0 | 100.0% |" in text
    assert "| blocking (line level) | 4 | 1 | 2 | 0 | 1 | 33.3% |" in text
    assert "| blocking: spec | 1 | 0 | 0 | 0 | 1 | n/a |" in text
    assert "| #7 | 1 | claude | blocking | unmet criterion | `a.py:10` | accepted |  |" in text
    assert "| #7 | 1 | claude | advisory | style | `b.py` | accepted |  |" in text
    assert "| #7 | 1 | claude | blocking | spec | (none) | unlocated | the finding cites no file |" in text
    assert "Pull requests: #9, #7" in text
    assert "<!--" not in text


def test_report_cells_are_escaped_and_empty_runs_render():
    empty = ra.Measurement(REPO, 3, 0, "2026-11-25T00:00:00Z")
    text = ra.render_markdown(empty)
    assert "| (none) | 0 | 0 | 0 | 0 | 0 | n/a |" in text and "merged none" in text
    row = ra.Result(1, 1, "x|y", "", SHA_A, "blocking", "a<b", "line", "p|q.py", 2, 4, ra.UNKNOWN, "r|s")
    text = ra.render_markdown(ra.Measurement(REPO, 1, 0, "t", [{"number": 1, "merged_at": "2026", "reviews": 1}], [row]))
    assert "| #1 | 1 | x\\|y | blocking | a&lt;b | `p\\|q.py:2-4` | unknown | r\\|s |" in text


def test_main_writes_the_report_and_json(tmp_path, capsys):
    out, data = tmp_path / "report.md", tmp_path / "report.json"
    assert ra.main(["--repo", REPO, "--limit", "5", "--out", str(out), "--json", str(data)], fetch=fake_repo()) == 0
    assert "## By finding type" in out.read_text(encoding="utf-8")
    payload = json.loads(data.read_text(encoding="utf-8"))
    assert payload["limit"] == 5 and len(payload["results"]) == 5
    assert ra.main(["--repo", REPO, "--limit", "1"], fetch=fake_repo()) == 0
    assert "# Automated review acceptance" in capsys.readouterr().out


def test_main_reports_github_failures_and_bad_arguments(capsys):
    def broken(path: str) -> object:
        raise ra.GitHubError("HTTP 401 bad credentials")

    assert ra.main(["--repo", REPO], fetch=broken) == 1
    assert "HTTP 401" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        ra.main(["--limit", "0"], fetch=broken)


def test_review_comments_page_through_long_threads():
    first = [comment("filler")] * 99 + [comment(review_body("x"))]
    fake = FakeGitHub({
        f"repos/{REPO}/issues/5/comments?per_page=100&page=1": first,
        f"repos/{REPO}/issues/5/comments?per_page=100&page=2": [comment(review_body("y"))],
    })
    assert len(ra.review_comments(fake, REPO, 5, ra.REVIEW_LOGIN)) == 2
    assert ra.review_comments(FakeGitHub({f"repos/{REPO}/issues/6/comments?per_page=100&page=1": {}}),
                              REPO, 6, ra.REVIEW_LOGIN) == []


def test_merged_pulls_page_until_enough():
    page = [pull(n, f"2026-11-{n % 28 + 1:02d}T00:00:00Z") for n in range(100)]
    fake = FakeGitHub({
        f"repos/{REPO}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=1": page,
        f"repos/{REPO}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=2": [],
    })
    assert len(ra.merged_pulls(fake, REPO, 60)) == 60
    assert len(ra.merged_pulls(fake, REPO, 30)) == 30


def test_gh_fetch_calls_the_cli(monkeypatch):
    seen: list[list[str]] = []

    def run(args, **kwargs):
        seen.append(args)
        if args[-1] == "ok":
            return subprocess.CompletedProcess(args, 0, '{"a": 1}', "")
        if args[-1] == "gone":
            return subprocess.CompletedProcess(args, 1, "", "gh: Not Found (HTTP 404)")
        return subprocess.CompletedProcess(args, 1, "", "gh: Server Error (HTTP 502)")

    monkeypatch.setattr(ra.subprocess, "run", run)
    assert ra.gh_fetch("ok") == {"a": 1}
    assert seen[0][:2] == ["gh", "api"]
    with pytest.raises(ra.GitHubError) as gone:
        ra.gh_fetch("gone")
    assert gone.value.missing
    with pytest.raises(ra.GitHubError) as broken:
        ra.gh_fetch("broken")
    assert not broken.value.missing
