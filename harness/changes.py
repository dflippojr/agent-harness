"""What a session changed in its workspace, for the diff viewer.

Git repositories in the workspace (the root or up to two levels down) are diffed against the commit the branch
started from (its upstream), so both commits the agent made and uncommitted edits show, including untracked files. Files outside any repository are listed without a diff: there's nothing to compare against.
"""

from __future__ import annotations

from pathlib import Path

from .projects import git
from .review_comments import parse_diff

MAX_DIFF_CHARS = 400_000
MAX_SCAN_COMMITS = 1_000
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # the parent of a root commit
SKIP = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv"}


def _git(repo: Path, *args: str) -> str:
    # Isolated host Git: workspace config/hooks/filters must not run. See harness.projects.
    return git(repo, *args, timeout=60, check=False).out


def find_repos(root: Path, depth: int = 2) -> list[Path]:
    if (root / ".git").exists():
        return [root]
    found: list[Path] = []
    if depth == 0:
        return found
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir() and p.name not in SKIP)
    except OSError:
        return found
    for child in children:
        found += find_repos(child, depth - 1)
    return found


def _status_files(repo: Path) -> list[dict]:
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    files = []
    for line in status.splitlines():
        code, path = line[:2], line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append({"path": path, "status": code.strip() or "?"})
    return files


def _base_commit(repo: Path, workspace: Path, base_commit: str | None) -> str:
    # Compare against where the branch started (its upstream), so commits the agent made show up too.
    base = base_commit if base_commit and repo == workspace else ""
    for ref in () if base else ("@{upstream}", "origin/HEAD"):
        base = _git(repo, "merge-base", "HEAD", ref).strip()
        if base:
            break
    return base or _git(repo, "rev-parse", "--verify", "-q", "HEAD").strip()


def _repo_diff(repo: Path, base: str, untracked: list[str]) -> str:
    diff = _git(repo, "diff", base, "--no-color", "--no-ext-diff", "--no-textconv") if base else ""
    for path in untracked:
        # Git for Windows maps /dev/null for --no-index too.
        diff += _git(repo, "diff", "--no-index", "--no-color", "--no-ext-diff", "--no-textconv",
                     "--", "/dev/null", path)
    return diff


def commit_diffs(repo: Path, base: str) -> list[dict]:
    """Each commit in base..HEAD, oldest first, with its own diff against its first parent: what a push sends."""
    out = []
    for sha in _git(repo, "rev-list", "--reverse", f"{base}..HEAD").split() if base else ():
        parent = _git(repo, "rev-parse", "--verify", "-q", f"{sha}^").strip() or EMPTY_TREE
        out.append({"sha": sha, "diff": _git(repo, "diff", parent, sha, "--no-color", "--no-ext-diff",
                                             "--no-textconv")})
    return out


def published(repo: Path, commit: str, tips: list[str]) -> bool:
    """Whether `commit` is at or before one of `tips` (remote-tracking refs, pushed heads): already on the remote,
    so rewriting it would need a force-push. A pushed head that no longer resolves counts as covering it."""
    for tip in tips:
        if not _git(repo, "rev-parse", "--verify", "-q", f"{tip}^{{commit}}").strip():
            if tip.startswith("refs/"):
                continue  # no such remote branch
            return True
        if git(repo, "merge-base", "--is-ancestor", commit, tip, timeout=60, check=False).code == 0:
            return True
    return False


def repo_diffs(workspace: Path, base_commit: str | None = None) -> list[dict]:
    """Each repository's full (untruncated) diff from its base to the working tree, untracked files included,
    and each commit since the base with its own diff (`commits`)."""
    workspace = workspace.resolve()
    out = []
    for repo in find_repos(workspace):
        files = _status_files(repo)
        # Show untracked files in the diff too, without staging anything for real.
        untracked = [f["path"] for f in files if f["status"] == "??" and "__pycache__/" not in f["path"]]
        base = _base_commit(repo, workspace, base_commit)
        out.append({"repo": repo, "path": repo.relative_to(workspace).as_posix() or ".", "files": files,
                    "base": base, "diff": _repo_diff(repo, base, untracked),
                    "head": _git(repo, "rev-parse", "--verify", "-q", "HEAD").strip(),
                    "commits": commit_diffs(repo, base),
                    "branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip(),
                    "subjects": _git(repo, "log", "--oneline", "--no-color", f"{base}..HEAD").splitlines()
                    if base else []})
    return out


def workspace_changes(workspace: Path, base_commit: str | None = None, scan=None) -> dict:
    """`base_commit` is where a git project's session branch started; it applies to the repo at the root.

    `scan(diffs) -> (public result, [diff per repo])` is the secret scan (issue #263): it sees the full diffs and
    returns them with flagged values masked, before truncation and parsing."""
    diffs = repo_diffs(workspace, base_commit)
    return changes_from_diffs(diffs, scan)


def changes_from_diffs(diffs: list[dict], scan=None) -> dict:
    """Render the same snapshot locally or from a runner, masking before parsing."""
    secret = None
    if scan is not None:
        secret, masked = scan(diffs)
        for d, text in zip(diffs, masked):
            d["diff"] = text
    repos = []
    budget = MAX_DIFF_CHARS
    for d in diffs:
        base, diff = d["base"], d["diff"]
        truncated = len(diff) > budget
        diff = diff[:max(0, budget)]
        budget -= len(diff)
        repos.append({"path": d["path"], "branch": d["branch"], "files": d["files"], "diff": diff,
                      "truncated": truncated, "base": base[:12], "head": d["head"][:12],
                      "parsed": parse_diff(diff),
                      "commits": d["subjects"]})
    return {"repos": repos} if secret is None else {"repos": repos, "secret_scan": secret}
