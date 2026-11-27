"""Acceptance metric for the automated PR review (#1011, step 1: measurement only).

For each automated review comment on the last N merged pull requests, this records whether a commit pushed after the
reviewed commit, before merge, touched the lines the review flagged, and reports the acceptance rate per backend, per
severity and per finding type. GitHub is its only source: `gh api` for pull requests, comments and commit lists, and
a shallow `git fetch` of each `refs/pull/N/head` into a temporary bare repository that is deleted afterwards. It writes
nothing to GitHub and changes nothing: not the review prompt, the backends or the gate.

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
* "Later commits" are exactly the PR's own commits after the reviewed commit (owner decision 1a on #1011): those
  `pulls/N/commits` lists (so none reachable from the base branch) that descend from the reviewed commit. Merge
  commits are left out; each other commit is judged on its own diff, in any order, so an edit a later commit reverts
  still counts, a fix merged in from a topic branch counts, and a merge from the base never does.
* A blocking finding cites `path:line` (or `path:a-b`) in the file at the reviewed commit. A commit accepts it when its
  diff deletes or changes one of those lines, or inserts a line between or right next to them (`--window` widens the
  range). The filename is followed through individual commits with `git diff -M`, including merges. The lines are
  then placed in the commit's parent by comparing the reviewed file's contents with that mapped file's contents.
* An advisory suggestion names only a file, never a line (the prompt reserves `path:line` for findings), so it is
  measured at file level: accepted when a later commit changed that file. The report marks those rows.
* A `**Spec**` finding has no location and is counted as unlocated, outside the rate.
* The outcome is unknown, counted but outside the rate, only when it cannot be measured: the reviewed commit is not an
  ancestor of the merged head (a force push), the PR's head ref is gone or moved, GitHub lists 250 commits (its cap),
  the cited file has no text patch (binary), its filename cannot be traced through an intermediate commit, or a later
  commit changed the file after a merge had rewritten the cited lines, so they cannot be placed in it.

The report holds counts and per-finding rows only, never review text, which is untrusted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REVIEW_LOGIN = "github-actions[bot]"
MARKER_RE = re.compile(r"<!-- agent-review: sha=([0-9a-f]{40}) mode=(full|incremental)(?: base=[A-Za-z0-9._/\-]+)? -->")
BACKEND_RE = re.compile(r"^Automated review backend: \*\*([a-z][a-z0-9_-]*)(?: \(([^)\r\n]*)\))?\*\*\.?\s*$", re.M)
FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
ORDINAL_RE = re.compile(r"\d{1,3}[.)]")
# The citation shape of the annotation step in .github/workflows/review.yml, matched in pieces so nothing backtracks.
CITATION_RE = re.compile(r"([^\s:`*]+):(\d+)(?:-(\d+))?")
LABEL_RE = re.compile(r"^\*\*([^*]{1,60}?):?\*\*")
ADVISORY_RE = re.compile(r"\*\*(Style|Structure):?\*\*")
CODE_PATH_RE = re.compile(r"`([^`\s]+)`")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
LEAD = "\u2014\u2013-:, \t"  # dashes, colons and commas between a citation and its label
NO_FINDINGS_RE = re.compile(r"\bno\b.*\bfindings?\b", re.I)

PR_COMMIT_CAP = 250  # GitHub's pulls/N/commits lists no more
MAX_PULL_PAGES = 100

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
        name = _heading(line)
        if name is not None:
            current = None if name in sections else sections.setdefault(name, [])
            continue
        if current is not None:
            current.append(line)
    return sections


def _heading(line: str) -> str | None:
    """The lower-cased title of a `## ` heading line, or None."""
    text = line.strip()
    if not text.startswith("##") or text.startswith("###") or text[2:3] not in (" ", "\t"):
        return None
    return text[2:].strip().lower()


def _item(line: str) -> str | None:
    """The text of a top-level list item (`- `, `* `, `+ ` or `1. `), or None for any other line."""
    if len(line) - len(line.lstrip()) > 1:
        return None  # a nested bullet is part of the finding above it
    text = line.lstrip()
    if text[:1] in ("-", "*", "+"):
        marker = 1
    else:
        ordinal = ORDINAL_RE.match(text)
        if not ordinal:
            return None
        marker = ordinal.end()
    if text[marker:marker + 1] not in (" ", "\t"):
        return None
    return text[marker:].strip()


def _label(text: str) -> str | None:
    match = LABEL_RE.match(text.lstrip(LEAD))
    if not match:
        return None
    # A combined label such as "Unmet criterion / Proof contradicted" counts under its first type.
    return re.split(r"[/;,]", match.group(1).lower())[0].strip()


def parse_findings(body: str) -> list[Finding]:
    """The blocking items under `## Findings` and the advisory ones under `## Style and structure`."""
    sections = _sections(body)
    findings: list[Finding] = []
    for line in sections.get("findings", []):
        text = _item(line)
        if text is None:
            continue
        cite = CITATION_RE.match(text.lstrip("`*_"))
        if cite:
            start = int(cite.group(2))
            end = int(cite.group(3)) if cite.group(3) else start
            if start < 1 or end < start:
                end = start
            path = cite.group(1).replace("\\", "/").removeprefix("./")
            rest = text.lstrip("`*_")[cite.end():].lstrip("`*_").lstrip(":) \t-")
            findings.append(Finding("blocking", _label(rest) or "correctness", path, start, end))
            continue
        label = _label(text)
        if label == "spec":
            rest = LABEL_RE.sub("", text.lstrip(LEAD), count=1)
            findings.append(Finding("blocking", _label(rest) or "spec", None))
        elif label is not None or not NO_FINDINGS_RE.search(text):
            findings.append(Finding("blocking", label or "unclassified", None))
    advisory_key = next((name for name in sections if name.startswith("style and structure")), None)
    for line in sections.get(advisory_key, []) if advisory_key else []:
        item = _item(line)
        text = item if item is not None else line.strip()
        match = ADVISORY_RE.match(text)
        code = CODE_PATH_RE.match(text[match.end():].lstrip(LEAD)) if match else None
        if match and code:
            path = code.group(1).replace("\\", "/").removeprefix("./")
            findings.append(Finding("advisory", match.group(1).lower(), path))
    return findings


def parse_review(body: str) -> Review | None:
    """A review comment's reviewed commit, backend and findings, or None when it carries no review marker."""
    marker = MARKER_RE.search(body)
    if not marker:
        return None
    # The job appends the real footer after the review text, just above its marker, so take the last footer-shaped
    # line before the marker: one the reviewer quoted (in a fence or not) always comes earlier.
    footers = list(BACKEND_RE.finditer(body, 0, marker.start()))
    backend = footers[-1] if footers else None
    name = backend.group(1) if backend else "unknown"
    model = (backend.group(2) or "") if backend else ""
    return Review(marker.group(1), name, model, parse_findings(body))


# ----------------------------------------------------------------------------------------------- patch analysis


@dataclass
class LineCheck:
    touched: bool
    start: int = 0  # where the range sits in the new file when untouched
    end: int = 0


def check_patch(patch: str, start: int, end: int, window: int = 0, adjacent: bool = True) -> LineCheck:
    """Did this unified-diff patch change old lines [start, end] (widened by window)?

    A deleted or changed line in the range touches it, and so does a line inserted between two of its lines. With
    `adjacent`, a line inserted directly above or below the range touches it too: that is how a commit is judged.
    Mapping lines through someone else's diff passes adjacent=False, since a neighbour's insertion leaves the lines
    where they were. When untouched, the result gives the range's line numbers in the new file.
    """
    edge = 1 if adjacent else 0
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
            if lo + 1 - edge <= old <= hi + edge:
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


def step_location(files: list[dict], path: str, start: int | None, end: int | None, adjacent: bool = True) -> FileStep:
    """Follow a cited file (and line range, when given) through one diff's file list."""
    for entry in files:
        status = entry.get("status")
        renamed_from_path = status == "renamed" and entry.get("previous_filename") == path
        if entry.get("filename") != path and not renamed_from_path:
            continue  # another file, or a copy of the cited file that leaves the original in place
        new_path = entry.get("filename", path)
        if status == "removed":
            return FileStep("touched", path)
        # No patch and no changed lines: a pure rename or a permission change. The text, and every line, stays put.
        metadata_only = not entry.get("patch") and not entry.get("changes")
        if start is None or end is None:
            return FileStep("untouched" if metadata_only else "touched", new_path)
        patch = entry.get("patch")
        if not patch:
            if metadata_only:
                return FileStep("untouched", new_path, start, end)
            return FileStep("unknown", reason="the diff has no text patch for the cited file")
        check = check_patch(patch, start, end, 0, adjacent)
        if check.touched:
            return FileStep("touched", new_path)
        return FileStep("untouched", new_path, check.start, check.end)
    return FileStep("untouched", path, start or 0, end or 0)


def parse_git_diff(text: str) -> list[dict]:
    """`git diff -M` output as GitHub-style file entries: filename, previous_filename, status, patch, changes."""
    files: list[dict] = []
    entry: dict | None = None
    in_patch = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            names = line[len("diff --git "):]
            old, _, new = names.partition(" b/")
            entry = {"filename": new, "previous_filename": old.removeprefix("a/"), "status": "modified",
                     "changes": 0}
            files.append(entry)
            in_patch = False
            continue
        if entry is None:
            continue
        if in_patch:
            if line[:1] in ("+", "-"):
                entry["changes"] += 1
            entry["patch"] += line + "\n"
            continue
        if line.startswith("@@ "):
            in_patch = True
            entry["patch"] = line + "\n"
        elif line.startswith("rename from "):
            entry["previous_filename"] = line[len("rename from "):]
            entry["status"] = "renamed"
        elif line.startswith("rename to "):
            entry["filename"] = line[len("rename to "):]
        elif line.startswith("new file mode"):
            entry["status"] = "added"
        elif line.startswith("deleted file mode"):
            entry["status"] = "removed"
        elif line.startswith("--- a/"):
            entry["previous_filename"] = line[len("--- a/"):]
        elif line.startswith("+++ b/"):
            entry["filename"] = line[len("+++ b/"):]
        elif line.startswith("Binary files "):
            entry["changes"] = 1
    for item in files:
        if item["status"] == "removed":
            item["filename"] = item["previous_filename"]
    return files


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


class GitError(Exception):
    """A git command failed for a reason other than a commit or ref the remote does not have."""


class PullHistory:
    """One merged PR's own commits, fetched from the remote into a temporary bare repository."""

    def __init__(self, workdir: Path, head: str, listed: list[dict], problem: str = ""):
        self.git_dir = workdir
        self.head = head
        self.problem = problem  # why nothing can be measured, when it cannot
        self.commits = {str(c.get("sha")): [str(p.get("sha")) for p in c.get("parents") or []] for c in listed}
        self._diffs: dict[tuple[str, str], list[dict]] = {}
        self._ancestries: dict[str, list[list[str]]] = {}
        self._paths: dict[tuple[str, str], dict[str, str | None]] = {}

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return run_git(["--git-dir", str(self.git_dir), *args], check=check)

    def has(self, sha: str) -> bool:
        return self.git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.git("merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0

    def diff(self, old: str, new: str) -> list[dict]:
        """Compare trees, or explicit `commit:path` blobs after following a filename."""
        key = (old, new)
        if key not in self._diffs:
            out = self.git("-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff", "-M", "-U0",
                           old, new).stdout
            self._diffs[key] = parse_git_diff(out)
        return self._diffs[key]

    def path_at(self, reviewed: str, target: str, path: str) -> str | None:
        """Follow renames along ancestry edges, never an accumulated tree diff that can lose similarity.

        Include merges for placement, although only the PR's non-merge commits can accept a finding. Follow every
        parent reachable from the review: a topic branch may retain a rename that the first parent's diff loses.
        Surviving mappings must agree; distinct destinations are ambiguous.
        A deletion without a detected rename loses the mapping, even if a later commit reuses the old filename.
        """
        key = (reviewed, path)
        if key not in self._paths:
            if reviewed not in self._ancestries:
                out = self.git("rev-list", "--reverse", "--topo-order", "--ancestry-path", "--parents",
                               f"{reviewed}..{self.head}").stdout
                self._ancestries[reviewed] = [line.split() for line in out.splitlines()]
            source_type = self.git("cat-file", "-t", f"{reviewed}:{path}", check=False).stdout.strip()
            paths: dict[str, str | None] = {reviewed: path if source_type == "blob" else None}
            for sha, *parents in self._ancestries[reviewed]:
                destinations: set[str] = set()
                for parent in parents:
                    previous = paths.get(parent)
                    if previous is None:
                        continue
                    current: str | None = previous
                    for entry in self.diff(parent, sha):
                        if entry.get("status") == "renamed" and entry.get("previous_filename") == previous:
                            current = entry["filename"]
                            break
                        if entry.get("status") == "removed" and entry.get("filename") == previous:
                            current = None
                            break
                    if current is not None:
                        destinations.add(current)
                paths[sha] = destinations.pop() if len(destinations) == 1 else None
            self._paths[key] = paths
        return self._paths[key].get(target)


def run_git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=env, check=False)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args[2:4])}: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return proc


def _remove_tree(path: Path) -> None:
    """Delete a temporary repository; git writes its packs read-only, which Windows will not delete as they are."""

    def make_writable_and_retry(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable_and_retry)
    else:  # pragma: no cover - CI runs 3.12 and later
        shutil.rmtree(path, onerror=make_writable_and_retry)


class Measurer:
    """Judges findings against each PR's own commits after the review, fetched from the remote one PR at a time.

    "Later commits" are exactly the PR's own commits after the reviewed commit (owner decision 1a on #1011): those
    GitHub lists for the PR (so none reachable from the base), descending from the reviewed commit, merges left out.
    Each is judged on its own diff. The filename is followed commit by commit into that commit's parent, then the
    cited lines are placed by comparing the two file blobs. `git log -L` cannot do this, because it follows lines
    backwards from a tip, and a line some commit deleted has no position at the tip.
    """

    def __init__(self, repo: str, fetch: Fetch = gh_fetch, window: int = 0, remote: str | None = None):
        self.repo = repo
        self.fetch = fetch
        self.window = window
        self.remote = remote or f"https://github.com/{repo}.git"
        self._root = Path(tempfile.mkdtemp(prefix="review-acceptance-"))
        self._histories: dict[int, PullHistory] = {}

    def __enter__(self) -> Measurer:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._root.exists():
            _remove_tree(self._root)

    def history(self, number: int, head: str) -> PullHistory:
        """The PR's listed commits and a shallow fetch of refs/pull/N/head deep enough to hold them all."""
        if number in self._histories:
            return self._histories[number]
        listed: list[dict] = []
        page = 1
        while page <= PR_COMMIT_CAP // 100 + 1:
            batch = self.fetch(f"repos/{self.repo}/pulls/{number}/commits?per_page=100&page={page}")
            if not isinstance(batch, list):
                break
            listed.extend(c for c in batch if isinstance(c, dict))
            if len(batch) < 100:
                break
            page += 1
        # One bare repository per run, so each fetch sends only what earlier ones did not. measure() judges each PR
        # right after its own fetch, before a later fetch can move the shallow boundary.
        workdir = self._root / "repo"
        history = PullHistory(workdir, head, listed)
        self._histories[number] = history
        if len(listed) >= PR_COMMIT_CAP:
            history.problem = "GitHub lists at most 250 commits of a pull request"
            return history
        if not workdir.exists():
            run_git(["init", "-q", "--bare", str(workdir)])
        ref = f"refs/pull/{number}/head"
        fetched = history.git("fetch", "-q", "--no-tags", f"--depth={len(listed) + 1}", self.remote,
                              f"+{ref}:refs/measured/{number}", check=False)
        if fetched.returncode != 0:
            detail = (fetched.stderr or "").lower()
            if "couldn't find remote ref" in detail or "not our ref" in detail:
                history.problem = "the remote no longer has the pull request's head ref"
                return history
            raise GitError(f"git fetch {ref}: {(fetched.stderr or fetched.stdout).strip()[-300:]}")
        if history.git("rev-parse", f"refs/measured/{number}").stdout.strip() != head:
            history.problem = "the pull request's head ref no longer points at the merged head"
        return history

    def outcome(self, finding: Finding, reviewed: str, number: int, head: str) -> tuple[str, str]:
        if finding.path is None:
            return UNLOCATED, "the finding cites no file"
        if reviewed == head:
            return NOT_ACCEPTED, "no commit after the review"
        history = self.history(number, head)
        if history.problem:
            return UNKNOWN, history.problem
        if not history.has(reviewed) or not history.is_ancestor(reviewed, head):
            return UNKNOWN, "the reviewed commit is not an ancestor of the merged head"
        later = [sha for sha, parents in history.commits.items()
                 if len(parents) == 1 and sha != reviewed and history.is_ancestor(reviewed, sha)]
        if not later:
            merges = any(len(parents) > 1 and history.is_ancestor(reviewed, sha)
                         for sha, parents in history.commits.items())
            return NOT_ACCEPTED, "only merges after the review" if merges else "no commit after the review"
        start = end = None
        if finding.start is not None and finding.end is not None:
            start, end = max(1, finding.start - self.window), finding.end + self.window
        unmappable = ""
        for sha in later:  # any order: one commit that touches the lines is enough
            parent = history.commits[sha][0]
            own = history.diff(parent, sha)
            mapped = history.path_at(reviewed, parent, finding.path)
            if mapped is None:
                unmappable = "the cited file cannot be traced through an intermediate commit"
                continue
            placed = FileStep("untouched", mapped, start or 0, end or 0)
            if parent != reviewed and start is not None:
                placed = step_location(history.diff(f"{reviewed}:{finding.path}", f"{parent}:{mapped}"),
                                       mapped, start, end, adjacent=False)
            if placed.outcome != "untouched":
                # The parent no longer holds the cited lines as reviewed (a base merge rewrote them). That only
                # matters when this commit changes the file; then it cannot be judged.
                names = {finding.path, mapped}  # retain the filename even when the lines have no text patch
                if any(names & {f.get("filename"), f.get("previous_filename")} for f in own):
                    unmappable = (placed.reason
                                  or "the cited lines were rewritten by a merge before a later commit changed the file")
                continue
            judged = step_location(own, placed.path, start and placed.start, end and placed.end)
            if judged.outcome == "touched":
                return ACCEPTED, ""
            if judged.outcome == "unknown":
                unmappable = judged.reason
        return (UNKNOWN, unmappable) if unmappable else (NOT_ACCEPTED, "")


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
    while page <= MAX_PULL_PAGES:
        batch = fetch(f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}")
        if not isinstance(batch, list) or not batch:
            break
        merged.extend(p for p in batch if isinstance(p, dict) and p.get("merged_at"))
        if len(batch) < 100:
            break
        merged.sort(key=lambda p: p["merged_at"], reverse=True)
        oldest_update = min(str(p.get("updated_at") or "") for p in batch if isinstance(p, dict))
        # A merge updates the PR, so every later page holds PRs merged no later than this page's oldest update.
        # Once the N-th latest merge seen is newer than that, no later page can displace it.
        if len(merged) >= limit and merged[limit - 1]["merged_at"] > oldest_update:
            break
        page += 1
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
            now: datetime | None = None, remote: str | None = None) -> Measurement:
    with Measurer(repo, fetch, window, remote) as measurer:
        return _measure(measurer, repo, limit, fetch, window, login, now)


def _measure(measurer: Measurer, repo: str, limit: int, fetch: Fetch, window: int, login: str,
             now: datetime | None) -> Measurement:
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
                outcome, reason = measurer.outcome(finding, review.sha, number, head)
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


def _inside_cwd(path: Path | None, parser: argparse.ArgumentParser) -> Path | None:
    """An output path resolved inside the working directory; anything that leaves it is an argument error."""
    if path is None:
        return None
    base = Path.cwd().resolve()
    resolved = (base / path).resolve()
    if not resolved.is_relative_to(base):
        parser.error(f"output paths must stay inside the working directory: {path}")
    return resolved


def main(argv: list[str] | None = None, fetch: Fetch = gh_fetch) -> int:
    parser = argparse.ArgumentParser(description="Acceptance metric for the automated PR review (#1011, step 1).")
    parser.add_argument("--repo", default="dflippojr/agent-harness", help="owner/name of the repository")
    parser.add_argument("--limit", type=int, default=50, help="how many of the latest merged PRs to read (N)")
    parser.add_argument("--window", type=int, default=0, help="extra lines around a cited range that also count")
    parser.add_argument("--login", default=REVIEW_LOGIN, help="the account that posts the automated review")
    parser.add_argument("--remote", help="git URL to fetch refs/pull/N/head from (default: the GitHub repository)")
    parser.add_argument("--out", type=Path, help="write the Markdown report here (default: print it)")
    parser.add_argument("--json", type=Path, help="also write the per-finding results as JSON here")
    args = parser.parse_args(argv)
    if args.limit < 1 or args.window < 0:
        parser.error("--limit must be at least 1 and --window at least 0")
    try:
        out, data = (_inside_cwd(path, parser) for path in (args.out, args.json))
        report = measure(args.repo, args.limit, fetch, args.window, args.login, remote=args.remote)
    except (GitHubError, GitError) as exc:
        print(f"review_acceptance: {exc}", file=sys.stderr)
        return 1
    markdown = render_markdown(report)
    if out:
        out.write_text(markdown, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(markdown)
    if data:
        data.write_text(to_json(report), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
