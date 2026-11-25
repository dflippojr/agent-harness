"""Acceptance metric for the automated PR review (#1011, step 1: measurement only).

For each automated review comment on the last N merged pull requests, this records whether a commit pushed after the
reviewed commit, before merge, touched the lines the review flagged, and reports the acceptance rate per backend, per
severity and per finding type. It reads everything from GitHub through `gh api` and changes nothing: not the review
prompt, the backends or the gate.

    python scripts/review_acceptance.py --repo dflippojr/agent-harness --limit 50 --out docs/review-acceptance.md
    python scripts/review_acceptance.py --limit 50 --json .tmp/review-acceptance.json

What counts, in the words of the review format in `.github/workflows/review.yml`:

* A review comment is a comment by the review bot (`github-actions[bot]`) carrying the job's
  `<!-- agent-review: sha=<reviewed commit> mode=... -->` marker, posted before the merge. Each round counts on its
  own, against the commits pushed after its own reviewed commit, so a finding repeated in two rounds counts twice.
* The backend is the one the comment's footer names (`Automated review backend: **codex (...)**`); the runner falls
  back from one backend to the next, so one comment has one backend.
* Severity is the section: `blocking` for an item under `## Findings`, `advisory` for a suggestion under
  `## Style and structure`. The type is the item's bold label (Unmet criterion, Untested fix, ...); a located
  blocking item with no label is `correctness`; an advisory one is `style` or `structure`.
* A blocking finding cites `path:line` (or `path:a-b`). It is accepted when a later commit deletes or changes one of
  those lines, or inserts a line right next to them (`--window` widens the range). The line numbers refer to the file
  at the reviewed commit, which is the old side of the diff to the merged head, so they need no remapping.
* An advisory suggestion names only a file, never a line (the prompt reserves `path:line` for findings), so it is
  measured at file level: accepted when a later commit changed that file. The report marks those rows.
* A `**Spec**` finding has no location and is counted as unlocated, outside the rate.
* The outcome is unknown, outside the rate, when it cannot be measured: the reviewed commit is gone or no longer an
  ancestor of the merged head (a force push), GitHub omits the file's patch, a compare is truncated, or a merge from
  the base branch touched the flagged lines. When the range holds a merge commit, the commits are walked one by one
  along the first-parent chain, so changes the merge brings in from the base branch never count as acceptance.

The report holds counts and per-finding rows only, never review text, which is untrusted.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REVIEW_LOGIN = "github-actions[bot]"
MARKER_RE = re.compile(r"<!-- agent-review: sha=([0-9a-f]{40}) mode=(full|incremental)(?: base=[A-Za-z0-9._/\-]+)? -->")
BACKEND_RE = re.compile(r"^Automated review backend: \*\*([a-z][a-z0-9_-]*)(?: \(([^)\r\n]*)\))?\*\*\.?\s*$", re.M)
HEADING_RE = re.compile(r"^\s*##\s+(.*?)\s*$")
FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+(.*)$")
# The same citation shape as the annotation step of .github/workflows/review.yml.
CITATION_RE = re.compile(r"^[`*_]*([^\s:`*]+):(\d+)(?:-(\d+))?[`*_]*[:\s)-]*(.*)$")
LABEL_RE = re.compile(r"^\*\*([^*]{1,60}?):?\*\*")
ADVISORY_RE = re.compile(r"^\s*(?:[-*+]\s+|\d{1,3}[.)]\s+)?\*\*(Style|Structure):?\*\*\s*[—–:-]*\s*`([^`\s]+)`")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
LEAD = "\u2014\u2013-:, \t"  # dashes, colons and commas between a citation and its label
NO_FINDINGS_RE = re.compile(r"\bno\b.*\bfindings?\b", re.I)

COMPARE_FILE_CAP = 300

ACCEPTED = "accepted"
NOT_ACCEPTED = "not_accepted"
UNKNOWN = "unknown"
UNLOCATED = "unlocated"
OUTCOMES = (ACCEPTED, NOT_ACCEPTED, UNKNOWN, UNLOCATED)


class GitHubError(Exception):
    """A `gh api` call failed; `missing` is true for a 404 or 422 (a commit or range GitHub cannot serve)."""

    def __init__(self, message: str, missing: bool = False):
        super().__init__(message)
        self.missing = missing


Fetch = Callable[[str], object]


def gh_fetch(path: str) -> object:
    """GET one GitHub REST path through the authenticated `gh` CLI and return the decoded JSON."""
    proc = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise GitHubError(f"gh api {path}: {detail[-300:]}", missing=bool(re.search(r"HTTP (404|422)", detail)))
    return json.loads(proc.stdout)


# ------------------------------------------------------------------------------------------------ review parsing


@dataclass
class Finding:
    severity: str  # blocking | advisory
    type: str
    path: str | None
    start: int | None = None
    end: int | None = None

    @property
    def granularity(self) -> str:
        if self.path is None:
            return "none"
        return "line" if self.start is not None else "file"


@dataclass
class Review:
    sha: str
    backend: str
    model: str
    findings: list[Finding]


def _sections(body: str) -> dict[str, list[str]]:
    """Lines under each `## ` heading, outside fenced code; the first heading of a name wins."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    fence: str | None = None
    for line in body.splitlines():
        match = FENCE_RE.match(line)
        if match:
            mark = match.group(1)
            if fence is None:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        heading = HEADING_RE.match(line)
        if heading:
            name = heading.group(1).lower()
            current = None if name in sections else sections.setdefault(name, [])
            continue
        if current is not None:
            current.append(line)
    return sections


def _label(text: str) -> str | None:
    match = LABEL_RE.match(text.lstrip(LEAD))
    if not match:
        return None
    # A combined label such as "Unmet criterion / Proof contradicted" counts under its first type.
    return re.split(r"\s*[/;,]\s*", match.group(1).strip().lower())[0]


def parse_findings(body: str) -> list[Finding]:
    """The blocking items under `## Findings` and the advisory ones under `## Style and structure`."""
    sections = _sections(body)
    findings: list[Finding] = []
    for line in sections.get("findings", []):
        item = ITEM_RE.match(line)
        if not item or len(line) - len(line.lstrip()) > 1:
            continue  # a nested bullet is part of the finding above it
        text = item.group(1).strip()
        cite = CITATION_RE.match(text)
        if cite:
            start = int(cite.group(2))
            end = int(cite.group(3)) if cite.group(3) else start
            if start < 1 or end < start:
                end = start
            path = cite.group(1).replace("\\", "/").removeprefix("./")
            findings.append(Finding("blocking", _label(cite.group(4)) or "correctness", path, start, end))
            continue
        label = _label(text)
        if label == "spec":
            rest = LABEL_RE.sub("", text.lstrip(LEAD), count=1)
            findings.append(Finding("blocking", _label(rest) or "spec", None))
        elif label is not None or not NO_FINDINGS_RE.search(text):
            findings.append(Finding("blocking", label or "unclassified", None))
    advisory_key = next((name for name in sections if name.startswith("style and structure")), None)
    for line in sections.get(advisory_key, []) if advisory_key else []:
        match = ADVISORY_RE.match(line)
        if match:
            path = match.group(2).replace("\\", "/").removeprefix("./")
            findings.append(Finding("advisory", match.group(1).lower(), path))
    return findings


def parse_review(body: str) -> Review | None:
    """A review comment's reviewed commit, backend and findings, or None when it carries no review marker."""
    marker = MARKER_RE.search(body)
    if not marker:
        return None
    backend = BACKEND_RE.search(body)
    name = backend.group(1) if backend else "unknown"
    model = (backend.group(2) or "") if backend else ""
    return Review(marker.group(1), name, model, parse_findings(body))


# ----------------------------------------------------------------------------------------------- patch analysis


@dataclass
class LineCheck:
    touched: bool
    start: int = 0  # where the range sits in the new file when untouched
    end: int = 0


def check_patch(patch: str, start: int, end: int, window: int = 0) -> LineCheck:
    """Did this unified-diff patch change old lines [start, end] (widened by window), or insert right next to them?

    A deleted or changed line in the range touches it, and so does a line inserted directly above or below it.
    When untouched, the result gives the range's line numbers in the new file, for walking commit by commit.
    """
    lo, hi = max(1, start - window), end + window
    delta = 0
    old = new = 0  # no hunk yet: lines before the first header are not diff lines
    for raw in patch.splitlines():
        header = HUNK_RE.match(raw)
        if header:
            old_start, old_count = int(header.group(1)), int(header.group(2) or "1")
            new_start, new_count = int(header.group(3)), int(header.group(4) or "1")
            old = old_start + 1 if old_count == 0 else old_start
            new = new_start + 1 if new_count == 0 else new_start
            if old <= lo:
                delta = new - old
            continue
        tag = raw[:1]
        if old == 0:
            continue
        if tag == "-":
            if lo <= old <= hi:
                return LineCheck(True)
            old += 1
        elif tag == "+":
            if lo <= old <= hi + 1:
                return LineCheck(True)
            new += 1
        elif tag == " ":
            old += 1
            new += 1
        else:
            continue  # "\ No newline at end of file"
        if old <= lo:
            delta = new - old
    return LineCheck(False, start + delta, end + delta)


@dataclass
class FileStep:
    """What one diff did to a cited location."""

    outcome: str  # touched | untouched | unknown
    path: str = ""
    start: int = 0
    end: int = 0
    reason: str = ""


def step_location(files: list[dict], truncated: bool, path: str, start: int | None, end: int | None, window: int) -> FileStep:
    """Follow a cited file (and line range, when given) through one compare's file list."""
    for entry in files:
        status = entry.get("status")
        renamed_from_path = status == "renamed" and entry.get("previous_filename") == path
        if entry.get("filename") != path and not renamed_from_path:
            continue  # another file, or a copy of the cited file that leaves the original in place
        new_path = entry.get("filename", path)
        if status == "removed":
            return FileStep("touched", path)
        if start is None or end is None:
            if status == "renamed" and not entry.get("patch") and not entry.get("changes"):
                return FileStep("untouched", new_path)
            return FileStep("touched", new_path)
        patch = entry.get("patch")
        if not patch:
            if status == "renamed" and not entry.get("changes"):
                return FileStep("untouched", new_path, start, end)
            return FileStep("unknown", reason="GitHub omitted the file's patch")
        check = check_patch(patch, start, end, window)
        if check.touched:
            return FileStep("touched", new_path)
        return FileStep("untouched", new_path, check.start, check.end)
    if truncated:
        return FileStep("unknown", reason="compare truncated at GitHub's file cap")
    return FileStep("untouched", path, start or 0, end or 0)


# ---------------------------------------------------------------------------------------------- measurement


@dataclass
class Result:
    pr: int
    round: int
    backend: str
    model: str
    reviewed_sha: str
    severity: str
    type: str
    granularity: str
    path: str | None
    start: int | None
    end: int | None
    outcome: str
    reason: str = ""


class Measurer:
    """Resolves outcomes against GitHub, caching every compare."""

    def __init__(self, repo: str, fetch: Fetch = gh_fetch, window: int = 0):
        self.repo = repo
        self.fetch = fetch
        self.window = window
        self._compares: dict[tuple[str, str], dict | GitHubError] = {}

    def compare(self, base: str, head: str) -> dict:
        key = (base, head)
        if key not in self._compares:
            try:
                data = self.fetch(f"repos/{self.repo}/compare/{base}...{head}")
                self._compares[key] = data if isinstance(data, dict) else {}
            except GitHubError as exc:
                if not exc.missing:
                    raise
                self._compares[key] = exc
        cached = self._compares[key]
        if isinstance(cached, GitHubError):
            raise cached
        return cached

    def outcome(self, finding: Finding, reviewed: str, head: str) -> tuple[str, str]:
        if finding.path is None:
            return UNLOCATED, "the finding cites no file"
        try:
            data = self.compare(reviewed, head)
        except GitHubError as exc:
            if not exc.missing:
                raise
            return UNKNOWN, "GitHub cannot compare the reviewed commit with the merged head"
        status = data.get("status")
        if status not in ("ahead", "identical"):
            return UNKNOWN, "the reviewed commit is not an ancestor of the merged head"
        commits = data.get("commits") or []
        if status == "identical" or not data.get("ahead_by"):
            return NOT_ACCEPTED, "no commit after the review"
        if int(data.get("total_commits") or len(commits)) > len(commits):
            return UNKNOWN, "more commits after the review than one compare lists"
        if not any(len(c.get("parents") or []) > 1 for c in commits):
            step = step_location(self._files(data), len(data.get("files") or []) >= COMPARE_FILE_CAP,
                                 finding.path, finding.start, finding.end, self.window)
            return self._verdict(step)
        return self._walk(finding, reviewed, head, commits)

    @staticmethod
    def _files(data: dict) -> list[dict]:
        return [f for f in data.get("files") or [] if isinstance(f, dict)]

    @staticmethod
    def _verdict(step: FileStep) -> tuple[str, str]:
        if step.outcome == "touched":
            return ACCEPTED, ""
        if step.outcome == "unknown":
            return UNKNOWN, step.reason
        return NOT_ACCEPTED, ""

    def _walk(self, finding: Finding, reviewed: str, head: str, commits: list[dict]) -> tuple[str, str]:
        """Commit by commit along the first-parent chain, so a merge from the base never counts as acceptance."""
        parents = {c.get("sha"): (c.get("parents") or []) for c in commits}
        chain: list[str] = []
        sha = head
        while sha != reviewed:
            if sha not in parents or not parents[sha]:
                return UNKNOWN, "the reviewed commit is not on the merged head's first-parent chain"
            chain.append(sha)
            sha = str(parents[sha][0].get("sha") or "")
        chain.reverse()
        path, start, end = finding.path or "", finding.start, finding.end
        previous = reviewed
        for sha in chain:
            is_merge = len(parents[sha]) > 1
            try:
                data = self.compare(previous, sha)
            except GitHubError as exc:
                if not exc.missing:
                    raise
                return UNKNOWN, "GitHub cannot compare one of the commits after the review"
            step = step_location(self._files(data), len(data.get("files") or []) >= COMPARE_FILE_CAP,
                                 path, start, end, self.window)
            if step.outcome == "unknown":
                return UNKNOWN, step.reason
            if step.outcome == "touched":
                if is_merge:
                    return UNKNOWN, "a merge from the base branch changed the cited lines"
                return ACCEPTED, ""
            path = step.path
            if start is not None:
                start, end = step.start, step.end
            previous = sha
        return NOT_ACCEPTED, ""


def review_comments(fetch: Fetch, repo: str, pr: int, login: str) -> list[dict]:
    comments: list[dict] = []
    page = 1
    while True:
        batch = fetch(f"repos/{repo}/issues/{pr}/comments?per_page=100&page={page}")
        if not isinstance(batch, list):
            break
        comments.extend(c for c in batch if isinstance(c, dict))
        if len(batch) < 100:
            break
        page += 1
    return [
        c for c in comments
        if (c.get("user") or {}).get("login") == login and MARKER_RE.search(c.get("body") or "")
    ]


def merged_pulls(fetch: Fetch, repo: str, limit: int) -> list[dict]:
    """The `limit` most recently merged pull requests, newest first."""
    merged: list[dict] = []
    page = 1
    while page <= 30:
        batch = fetch(f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}")
        if not isinstance(batch, list) or not batch:
            break
        merged.extend(p for p in batch if isinstance(p, dict) and p.get("merged_at"))
        if len(batch) < 100:
            break
        page += 1
        if len(merged) >= limit * 2:
            break
    merged.sort(key=lambda p: p["merged_at"], reverse=True)
    return merged[:limit]


@dataclass
class Measurement:
    repo: str
    limit: int
    window: int
    generated_at: str
    pulls: list[dict] = field(default_factory=list)  # number, merged_at, reviews
    results: list[Result] = field(default_factory=list)
    skipped_after_merge: int = 0


def measure(repo: str, limit: int, fetch: Fetch = gh_fetch, window: int = 0, login: str = REVIEW_LOGIN,
            now: datetime | None = None) -> Measurement:
    measurer = Measurer(repo, fetch, window)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = Measurement(repo, limit, window, stamp)
    for pull in merged_pulls(fetch, repo, limit):
        number = int(pull["number"])
        head = (pull.get("head") or {}).get("sha", "")
        merged_at = pull["merged_at"]
        rounds = 0
        comments = sorted(review_comments(fetch, repo, number, login), key=lambda c: c.get("created_at", ""))
        for comment in comments:
            if comment.get("created_at", "") > merged_at:
                report.skipped_after_merge += 1
                continue
            review = parse_review(comment.get("body") or "")
            if review is None:
                continue
            rounds += 1
            for finding in review.findings:
                outcome, reason = measurer.outcome(finding, review.sha, head)
                report.results.append(Result(
                    number, rounds, review.backend, review.model, review.sha, finding.severity, finding.type,
                    finding.granularity, finding.path, finding.start, finding.end, outcome, reason,
                ))
        report.pulls.append({"number": number, "merged_at": merged_at, "reviews": rounds})
    return report


# ---------------------------------------------------------------------------------------------------- reporting


def tally(results: Iterable[Result], key: Callable[[Result], str]) -> list[tuple[str, dict[str, int]]]:
    groups: dict[str, dict[str, int]] = {}
    for result in results:
        counts = groups.setdefault(key(result), {name: 0 for name in OUTCOMES})
        counts[result.outcome] += 1
    return sorted(groups.items())


def rate(counts: dict[str, int]) -> str:
    measured = counts[ACCEPTED] + counts[NOT_ACCEPTED]
    return f"{100 * counts[ACCEPTED] / measured:.1f}%" if measured else "n/a"


def _table(title: str, rows: list[tuple[str, dict[str, int]]], column: str) -> list[str]:
    lines = [f"## {title}", "", f"| {column} | Findings | Accepted | Not accepted | Unknown | Unlocated | Acceptance |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for name, counts in rows:
        total = sum(counts.values())
        lines.append(f"| {name} | {total} | {counts[ACCEPTED]} | {counts[NOT_ACCEPTED]} | {counts[UNKNOWN]} | "
                     f"{counts[UNLOCATED]} | {rate(counts)} |")
    if not rows:
        lines.append("| (none) | 0 | 0 | 0 | 0 | 0 | n/a |")
    return [*lines, ""]


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("<", "&lt;").replace("\n", " ")


def render_markdown(report: Measurement) -> str:
    results = report.results
    numbers = [p["number"] for p in report.pulls]
    reviewed = [p for p in report.pulls if p["reviews"]]
    span = (f"{report.pulls[-1]['merged_at'][:10]} to {report.pulls[0]['merged_at'][:10]}" if report.pulls else "none")
    severity_label = {"blocking": "blocking (line level)", "advisory": "advisory (file level)"}
    lines = [
        "# Automated review acceptance",
        "",
        "Generated by `python scripts/review_acceptance.py` (#1011, step 1: measurement only). A finding is accepted",
        "when a commit pushed after the reviewed commit, before merge, changed the lines it cites (blocking findings)",
        "or the file it names (advisory suggestions, which never cite a line). Acceptance is accepted divided by",
        "accepted plus not accepted; unknown and unlocated findings are counted but left out of the rate. The script's",
        "docstring defines each outcome.",
        "",
        f"- Repository: `{report.repo}`",
        f"- Generated: {report.generated_at}",
        f"- Merged pull requests: last {report.limit} by merge time ({len(report.pulls)} found, merged {span}),"
        f" {len(reviewed)} with automated reviews, {sum(p['reviews'] for p in report.pulls)} review rounds",
        f"- Line window: {report.window} (plus an insertion directly above or below the cited lines)",
        f"- Review comments posted after the merge, not counted: {report.skipped_after_merge}",
        f"- Pull requests: {', '.join(f'#{n}' for n in numbers) if numbers else 'none'}",
        "",
    ]
    overall = tally(results, lambda r: "all findings")
    lines += _table("Overall", overall, "Scope")
    lines += _table("By backend", tally(results, lambda r: r.backend), "Backend")
    lines += _table("By severity", tally(results, lambda r: severity_label.get(r.severity, r.severity)), "Severity")
    lines += _table("By finding type", tally(results, lambda r: f"{r.severity}: {r.type}"), "Type")
    lines += ["## Findings", "", "| PR | Round | Backend | Severity | Type | Location | Outcome | Reason |",
              "| --- | ---: | --- | --- | --- | --- | --- | --- |"]
    for r in results:
        if r.path is None:
            where = "(none)"
        elif r.start is None:
            where = f"`{_cell(r.path)}`"
        else:
            span_text = f"{r.start}" if r.start == r.end else f"{r.start}-{r.end}"
            where = f"`{_cell(r.path)}:{span_text}`"
        lines.append(f"| #{r.pr} | {r.round} | {_cell(r.backend)} | {r.severity} | {_cell(r.type)} | {where} | "
                     f"{r.outcome.replace('_', ' ')} | {_cell(r.reason)} |")
    if not results:
        lines.append("| (none) | | | | | | | |")
    return "\n".join(lines) + "\n"


def to_json(report: Measurement) -> str:
    payload = asdict(report)
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None, fetch: Fetch = gh_fetch) -> int:
    parser = argparse.ArgumentParser(description="Acceptance metric for the automated PR review (#1011, step 1).")
    parser.add_argument("--repo", default="dflippojr/agent-harness", help="owner/name of the repository")
    parser.add_argument("--limit", type=int, default=50, help="how many of the latest merged PRs to read (N)")
    parser.add_argument("--window", type=int, default=0, help="extra lines around a cited range that also count")
    parser.add_argument("--login", default=REVIEW_LOGIN, help="the account that posts the automated review")
    parser.add_argument("--out", type=Path, help="write the Markdown report here (default: print it)")
    parser.add_argument("--json", type=Path, help="also write the per-finding results as JSON here")
    args = parser.parse_args(argv)
    if args.limit < 1 or args.window < 0:
        parser.error("--limit must be at least 1 and --window at least 0")
    try:
        report = measure(args.repo, args.limit, fetch, args.window, args.login)
    except GitHubError as exc:
        print(f"review_acceptance: {exc}", file=sys.stderr)
        return 1
    markdown = render_markdown(report)
    if args.out:
        args.out.write_text(markdown, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(markdown)
    if args.json:
        args.json.write_text(to_json(report), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
