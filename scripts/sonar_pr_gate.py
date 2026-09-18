"""Compare unresolved Sonar issues and hotspots between a PR-base snapshot and the PR head.

Community Build has no pull-request analysis. Pinning New Code to a baseline
scan is a date window: with SCM blame, only lines committed after that
timestamp count as new, so a baseline taken at PR-check time leaves
new_violations at 0. This script waits for Compute Engine, snapshots
unresolved findings keyed by rule + path + message (not line or issue key),
and fails only on keys the PR head adds.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable

PAGE_SIZE = 500
WAIT_TIMEOUT_SEC = 900
POLL_SEC = 2
HTTP_TIMEOUT_SEC = 60
ACTIVITY_PATH = "/api/ce/activity"
ISSUES_PATH = "/api/issues/search"
HOTSPOTS_PATH = "/api/hotspots/search"


class GateError(Exception):
    """Fatal Sonar API or snapshot problem; the GitHub check should fail."""

    def __init__(self, message: str, http_code: int | None = None):
        super().__init__(message)
        self.http_code = http_code


def _auth_header(token: str) -> str:
    blob = base64.b64encode(f"{token}:".encode("ascii")).decode("ascii")
    return f"Basic {blob}"


def sonar_get(host: str, token: str, path: str, params: dict[str, str | int]) -> Any:
    query = urllib.parse.urlencode(params)
    url = f"{host.rstrip('/')}{path}?{query}"
    req = urllib.request.Request(url, headers={"Authorization": _auth_header(token)})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as response:  # noqa: S310 - host is the configured Sonar URL
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise GateError(f"GET {path} failed: HTTP {exc.code} {body[:500]}", http_code=exc.code) from exc
    except urllib.error.URLError as exc:
        raise GateError(f"GET {path} failed: {exc}") from exc


def file_path(component: str, project: str) -> str:
    prefix = f"{project}:"
    if component.startswith(prefix):
        return component[len(prefix):]
    if ":" in component:
        return component.split(":", 1)[1]
    return component


def fingerprint(kind: str, rule: str, component: str, message: str, project: str) -> tuple[str, str, str, str]:
    return (kind, rule, file_path(component, project), message)


def issue_fingerprint(issue: dict[str, Any], project: str) -> tuple[str, str, str, str] | None:
    if issue.get("type") == "SECURITY_HOTSPOT":
        return None
    return fingerprint(
        "issue",
        str(issue.get("rule") or ""),
        str(issue.get("component") or ""),
        str(issue.get("message") or ""),
        project,
    )


def hotspot_fingerprint(hotspot: dict[str, Any], project: str) -> tuple[str, str, str, str]:
    rule = str(hotspot.get("ruleKey") or hotspot.get("rule") or hotspot.get("securityCategory") or "")
    return fingerprint(
        "hotspot",
        rule,
        str(hotspot.get("component") or ""),
        str(hotspot.get("message") or ""),
        project,
    )


def paginate(fetch_page: Callable[[int], dict[str, Any]], list_key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = fetch_page(page)
        batch = list(payload.get(list_key) or [])
        items.extend(batch)
        paging = payload.get("paging") or {}
        total = int(paging.get("total") or len(items))
        if not batch or len(items) >= total:
            break
        page += 1
        if page > 1000:
            raise GateError(f"{list_key} pagination exceeded 1000 pages")
    return items


def fetch_issues(host: str, token: str, project: str) -> list[dict[str, Any]]:
    def page(index: int) -> dict[str, Any]:
        return sonar_get(host, token, ISSUES_PATH, {
            "componentKeys": project,
            "resolved": "false",
            "ps": PAGE_SIZE,
            "p": index,
        })
    return paginate(page, "issues")


def fetch_hotspots(host: str, token: str, project: str) -> list[dict[str, Any]]:
    def page(index: int) -> dict[str, Any]:
        return sonar_get(host, token, HOTSPOTS_PATH, {
            "projectKey": project,
            "status": "TO_REVIEW",
            "ps": PAGE_SIZE,
            "p": index,
        })
    return paginate(page, "hotspots")


def snapshot_findings(issues: list[dict[str, Any]], hotspots: list[dict[str, Any]],
                      project: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for issue in issues:
        fp = issue_fingerprint(issue, project)
        if fp is None:
            continue
        kind, rule, path, message = fp
        rows.append({"kind": kind, "rule": rule, "path": path, "message": message})
    for hotspot in hotspots:
        kind, rule, path, message = hotspot_fingerprint(hotspot, project)
        rows.append({"kind": kind, "rule": rule, "path": path, "message": message})
    return rows


def counts(rows: list[dict[str, str]]) -> Counter[tuple[str, str, str, str]]:
    return Counter((row["kind"], row["rule"], row["path"], row["message"]) for row in rows)


def new_findings(baseline: list[dict[str, str]], head: list[dict[str, str]]) -> list[dict[str, str]]:
    extra = counts(head) - counts(baseline)
    out = [{"kind": kind, "rule": rule, "path": path, "message": message}
           for (kind, rule, path, message), n in extra.items() for _ in range(n)]
    out.sort(key=lambda row: (row["kind"], row["path"], row["rule"], row["message"]))
    return out


def latest_task_id(host: str, token: str, project: str) -> str:
    try:
        data = sonar_get(host, token, ACTIVITY_PATH, {"component": project, "ps": 1})
    except GateError as exc:
        if exc.http_code == 404:
            return ""
        raise
    tasks = data.get("tasks") or []
    if not tasks:
        return ""
    return str(tasks[0].get("id") or "")


def wait_for_new_report(host: str, token: str, project: str, after_id: str,
                        timeout: float = WAIT_TIMEOUT_SEC, sleeper=time.sleep,
                        clock=time.monotonic) -> dict[str, Any]:
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            data = sonar_get(host, token, ACTIVITY_PATH, {"component": project, "ps": 10})
        except GateError as exc:
            if exc.http_code == 404:
                sleeper(POLL_SEC)
                continue
            raise
        for task in data.get("tasks") or []:
            kind = str(task.get("type") or "REPORT")
            if kind != "REPORT":
                continue
            task_id = str(task.get("id") or "")
            if after_id and task_id == after_id:
                break
            status = str(task.get("status") or "")
            if status == "SUCCESS":
                return task
            if status in ("FAILED", "CANCELED"):
                raise GateError(f"Sonar analysis {task_id} {status}")
            break
        sleeper(POLL_SEC)
    raise GateError(f"timed out after {timeout:.0f}s waiting for Sonar analysis of {project}")


def collect(host: str, token: str, project: str, after_id: str,
            timeout: float = WAIT_TIMEOUT_SEC) -> list[dict[str, str]]:
    wait_for_new_report(host, token, project, after_id, timeout=timeout)
    return snapshot_findings(fetch_issues(host, token, project), fetch_hotspots(host, token, project), project)


def format_new(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "No new Sonar issues or hotspots vs the PR base snapshot."
    lines = [f"New Sonar findings introduced by this PR ({len(rows)}):"]
    for row in rows:
        lines.append(f"  [{row['kind']}] {row['rule']} {row['path']}: {row['message']}")
    return "\n".join(lines)


def token_from_env() -> str:
    token = (os.environ.get("SONAR_TOKEN") or "").strip()
    if not token:
        raise GateError("SONAR_TOKEN is not set")
    return token


def cmd_latest_id(args: argparse.Namespace) -> int:
    print(latest_task_id(args.host, token_from_env(), args.project))
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    rows = collect(args.host, token_from_env(), args.project, args.after_id or "", timeout=args.timeout)
    Path(args.out).write_text(json.dumps(rows), encoding="utf-8")
    print(f"snapshot {len(rows)} unresolved findings -> {args.out}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    head = json.loads(Path(args.head).read_text(encoding="utf-8"))
    extra = new_findings(baseline, head)
    print(format_new(extra))
    return 1 if extra else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    latest = sub.add_parser("latest-id", help="print the newest Compute Engine task id, or empty")
    latest.add_argument("--host", required=True)
    latest.add_argument("--project", required=True)
    latest.set_defaults(func=cmd_latest_id)
    collect_cmd = sub.add_parser("collect", help="wait for a new analysis and write a findings snapshot")
    collect_cmd.add_argument("--host", required=True)
    collect_cmd.add_argument("--project", required=True)
    collect_cmd.add_argument("--after-id", default="")
    collect_cmd.add_argument("--out", required=True)
    collect_cmd.add_argument("--timeout", type=float, default=WAIT_TIMEOUT_SEC)
    collect_cmd.set_defaults(func=cmd_collect)
    diff = sub.add_parser("diff", help="fail if the head snapshot has findings absent from baseline")
    diff.add_argument("--baseline", required=True)
    diff.add_argument("--head", required=True)
    diff.set_defaults(func=cmd_diff)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except GateError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
