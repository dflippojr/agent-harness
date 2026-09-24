"""What a session changed in its workspace, for the diff viewer.

Git repositories in the workspace (the root or up to two levels down) are diffed against the commit the branch
started from (its upstream), so both commits the agent made and uncommitted edits show, including untracked files. Files outside any repository are listed without a diff: there's nothing to compare against.
"""

from __future__ import annotations

from pathlib import Path

from .projects import git
from .review_comments import parse_diff

MAX_DIFF_CHARS = 400_000
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


def workspace_changes(workspace: Path, base_commit: str | None = None) -> dict:
    """`base_commit` is where a git project's session branch started; it applies to the repo at the root."""
    workspace = workspace.resolve()
    repos = []
    budget = MAX_DIFF_CHARS
    for repo in find_repos(workspace):
        rel = repo.relative_to(workspace).as_posix()
        status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
        files = []
        for line in status.splitlines():
            code, path = line[:2], line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            files.append({"path": path, "status": code.strip() or "?"})
        # Show untracked files in the diff too, without staging anything for real.
        untracked = [f["path"] for f in files if f["status"] == "??" and "__pycache__/" not in f["path"]]
        # Compare against where the branch started (its upstream), so commits the agent made show up too.
        base = base_commit if base_commit and repo == workspace else ""
        for ref in () if base else ("@{upstream}", "origin/HEAD"):
            base = _git(repo, "merge-base", "HEAD", ref).strip()
            if base:
                break
        base = base or _git(repo, "rev-parse", "--verify", "-q", "HEAD").strip()
        diff = _git(repo, "diff", base, "--no-color", "--no-ext-diff", "--no-textconv") if base else ""
        for path in untracked:
            # Git for Windows maps /dev/null for --no-index too.
            diff += _git(repo, "diff", "--no-index", "--no-color", "--no-ext-diff", "--no-textconv",
                         "--", "/dev/null", path)
        truncated = len(diff) > budget
        diff = diff[:max(0, budget)]
        budget -= len(diff)
        new_commits = _git(repo, "log", "--oneline", "--no-color", f"{base}..HEAD") if base else ""
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        repos.append({"path": rel or ".", "branch": branch, "files": files, "diff": diff,
                      "truncated": truncated, "base": base[:12],
                      "head": _git(repo, "rev-parse", "--verify", "-q", "HEAD").strip()[:12],
                      "parsed": parse_diff(diff),
                      "commits": new_commits.splitlines()})
    return {"repos": repos}
