"""Compare hosted runs by tool profile (#638): tokens, cost, success and localisation failures, per task type.

    python -m bakeoff.tool_profile_compare --db D:/Agents/harness/harness.sqlite3 --tasks tasks.json [--workspaces]
    python -m bakeoff.tool_profile_compare --runs bakeoff/tool_profile_fixture.json [--json]

`--tasks` names the runs to compare, one entry per session:

    {"runs": [{"session_id": "a1b2c3d4e5", "task": "fix-flaky-test", "task_type": "edit",
               "expected_files": ["tests/test_cache.py"], "success": true}]}

`task_type` is `shell` (the work is mostly commands) or `edit` (it is mostly changing files): arXiv 2609.20804 found
bash-only gains depended on the benchmark, so the two are never pooled. `success` is the owner's grade; without it a
run that ended `done` counts as a success. A run with `expected_files` is a localisation failure when it changed none
of them, the failure mode that grew under bash-only in the paper (Table 10). Changed files come from the entry's own
`files_changed`, else the files the harness saw the run write plus, with `--workspaces`, the session workspace's diff
(or, in a workspace with no git repository, the files written since the session was created).
`--db` is the main store, which holds the usage log; each session is read from the store that holds it (Web's,
`apps/app-web/harness.sqlite3` beside it, or an App's), or from the main store before the Web store migration. Every
database is opened read-only. `--runs` reads ready-made rows (the same fields plus `tool_profile`,
`prompt_tokens`, `completion_tokens` and `cost_usd`) instead, as the fixture does.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROFILES = ("default", "bash-only")
TASK_TYPES = ("shell", "edit")


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").removeprefix("./").removeprefix("/workspace/")


def workspace_files(workspace: Path, base_commit: str | None = None, since: float = 0.0) -> set[str]:
    """Files the session's workspace changed: against the commit its branch started from, as the diff viewer shows,
    or, in a workspace with no git repository, every file written since the session was created."""
    from harness.changes import SKIP, find_repos, repo_diffs
    from harness.review_comments import parse_diff
    if not find_repos(workspace.resolve()):
        return {p.relative_to(workspace).as_posix() for p in workspace.rglob("*")
                if p.is_file() and not SKIP & set(p.relative_to(workspace).parts) and p.stat().st_mtime >= since}
    out: set[str] = set()
    for repo in repo_diffs(workspace, base_commit or None):
        prefix = "" if repo["path"] == "." else repo["path"] + "/"
        out |= {prefix + f["name"] for f in parse_diff(repo["diff"])}
    return out


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def session_stores(db_path: Path) -> list[Path]:
    """Where sessions can live (#330): Web's store first, then each App's, then the main store itself."""
    apps = Path(db_path).resolve().parent / "apps"
    web = apps / "app-web" / "harness.sqlite3"
    others = sorted(p for p in apps.glob("*/harness.sqlite3") if p != web) if apps.is_dir() else []
    return [p for p in (web, *others) if p.is_file()] + [Path(db_path)]


def _find_session(stores: list[sqlite3.Connection], sid: str) -> dict | None:
    """The session's row from the first store that holds it. An App store left dormant since the upgrade has not run
    the tool profile migration yet: its sessions read back `default`, as the migration would make them."""
    for conn in stores:
        try:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        except sqlite3.OperationalError:  # a store with no sessions table
            continue
        if row is not None:
            return {"tool_profile": "default", "base_commit": "", **dict(row)}
    return None


def rows_from_db(db_path: Path, manifest: dict, workspaces: bool = False) -> list[dict]:
    """One row per manifest entry: the session's profile and status from its store, its summed usage from the main
    store's usage log."""
    conn = _open(db_path)
    stores = [_open(p) for p in session_stores(db_path)]
    rows = []
    try:
        for entry in manifest.get("runs", []):
            sid = entry["session_id"]
            s = _find_session(stores, sid)
            if s is None:
                raise SystemExit(f"session {sid} is in no store beside {db_path}")
            usage = conn.execute("SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c, "
                                 "COALESCE(SUM(cost_usd),0) cost FROM usage WHERE session_id = ?", (sid,)).fetchone()
            run = json.loads(s["run"] or "{}")
            changed = entry.get("files_changed")
            if changed is None:
                changed = set(run.get("files_touched") or [])
                if workspaces and s["workspace"] and Path(s["workspace"]).is_dir():
                    changed |= workspace_files(Path(s["workspace"]), s["base_commit"], s["created_at"])
            rows.append({**entry, "tool_profile": s["tool_profile"], "backend": s["backend"], "model": s["model"],
                         "status": s["status"], "prompt_tokens": usage["p"], "completion_tokens": usage["c"],
                         "cost_usd": usage["cost"], "files_changed": sorted(changed)})
    finally:
        for c in (conn, *stores):
            c.close()
    return rows


def succeeded(row: dict) -> bool:
    return bool(row["success"]) if row.get("success") is not None else row.get("status") == "done"


def localisation_failure(row: dict) -> bool | None:
    """True when the run changed none of the files the task needed; None when the task names no files."""
    expected = {_norm(p) for p in row.get("expected_files") or []}
    if not expected:
        return None
    return not expected & {_norm(p) for p in row.get("files_changed") or []}


def summarize(rows: list[dict]) -> list[dict]:
    """One summary per (task type, profile) that has runs, then one per profile over every task type."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        task_type = row.get("task_type") or "unknown"
        groups.setdefault((task_type, row.get("tool_profile") or "default"), []).append(row)
        groups.setdefault(("all", row.get("tool_profile") or "default"), []).append(row)
    order = {t: i for i, t in enumerate((*TASK_TYPES, "unknown", "all", *PROFILES))}
    out = []
    for (task_type, profile), group in sorted(groups.items(), key=lambda kv: (
            order.get(kv[0][0], 2.5), order.get(kv[0][1], len(order)), kv[0])):
        wins = sum(succeeded(r) for r in group)
        localisation_results = [f for f in (localisation_failure(r) for r in group) if f is not None]
        cost = sum(float(r.get("cost_usd") or 0) for r in group)
        out.append({
            "task_type": task_type, "tool_profile": profile, "runs": len(group), "successes": wins,
            "success_rate": wins / len(group),
            "prompt_tokens": sum(int(r.get("prompt_tokens") or 0) for r in group),
            "completion_tokens": sum(int(r.get("completion_tokens") or 0) for r in group),
            "cost_usd": round(cost, 6), "cost_per_run": round(cost / len(group), 6),
            "cost_per_success": round(cost / wins, 6) if wins else None,
            "localisation_checked": len(localisation_results), "localisation_failures": sum(localisation_results),
        })
    return out


def render(summary: list[dict]) -> str:
    head = ("task type", "profile", "runs", "success", "prompt tok", "completion tok", "cost $", "$/run",
            "$/success", "loc. fail")
    lines: list[tuple[str, ...]] = [head]
    for g in summary:
        lines.append((g["task_type"], g["tool_profile"], str(g["runs"]),
                      f"{g['successes']}/{g['runs']} ({g['success_rate']:.0%})", f"{g['prompt_tokens']:,}",
                      f"{g['completion_tokens']:,}", f"{g['cost_usd']:.4f}", f"{g['cost_per_run']:.4f}",
                      "-" if g["cost_per_success"] is None else f"{g['cost_per_success']:.4f}",
                      f"{g['localisation_failures']}/{g['localisation_checked']}" if g["localisation_checked"] else "-"))
    widths = [max(len(line[i]) for line in lines) for i in range(len(head))]
    text = ["  ".join(cell.ljust(w) if i < 2 else cell.rjust(w) for i, (cell, w) in enumerate(zip(line, widths)))
            for line in lines]
    return "\n".join([text[0], "  ".join("-" * w for w in widths), *text[1:]])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare hosted runs by tool profile (#638).")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", type=Path, help="the harness SQLite database (opened read-only)")
    source.add_argument("--runs", type=Path, help="ready-made rows, such as bakeoff/tool_profile_fixture.json")
    parser.add_argument("--tasks", type=Path, help="with --db: the runs to compare and their task types")
    parser.add_argument("--workspaces", action="store_true", help="with --db: add each workspace's diff")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    if args.db and not args.tasks:
        parser.error("--db needs --tasks")
    if args.db:
        rows = rows_from_db(args.db, json.loads(args.tasks.read_text(encoding="utf-8")), args.workspaces)
    else:
        rows = json.loads(args.runs.read_text(encoding="utf-8"))["runs"]
    summary = summarize(rows)
    sys.stdout.write((json.dumps(summary, indent=2) if args.json else render(summary)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
