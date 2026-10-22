"""Decisions behind the always-on `gate` check (issue #726), run by the CI and deploy workflows.

    python3 scripts/ci_gate.py changes        # ci.yml `changes`: does any changed file need the tests?
    python3 scripts/ci_gate.py gate           # ci.yml `gate`: did every needed job pass or skip on purpose?
    python3 scripts/ci_gate.py deploy --repository dflippojr/agent-harness --run-id <id> --head-sha <sha>
                                              # ci-cd.yml `guard`: may this CI run publish and deploy?

Each subcommand writes its outputs (`tests`, `deploy`) to $GITHUB_OUTPUT when that variable is set, and prints why.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Callable, Iterable, Mapping

ZERO_SHA = "0" * 40

# Jobs whose `if:` reads the `changes` output. They may be skipped only when no changed file needs the tests.
TEST_JOBS = ("test", "test-py313", "openapi", "ui-tests", "hub-contract")
# Jobs that run on every change (#729); skipped is never expected of them.
ALWAYS_JOBS = ("lint",)

Runner = Callable[[list[str]], str]


def _run(command: list[str]) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def _write_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    print(f"{name}={value}")


def skip_paths(text: str) -> set[str]:
    """Exact repository paths, one per line. Blank lines are ignored; there are no globs."""
    return {line.strip() for line in text.splitlines() if line.strip()}


def files_needing_tests(changed: Iterable[str], skipped: set[str]) -> list[str]:
    return [path for path in changed if path not in skipped]


def changed_files(event_name: str, push_before: str, run: Runner | None = None) -> list[str] | None:
    """Files this run changes, or None when they cannot be listed (then the tests run).

    A pull_request run checks out the PR's merge commit, whose first parent is the base tip, so HEAD^1..HEAD is what
    merging the PR changes. A push compares against the event's `before` commit. Rename detection is off: with it on,
    a rename lists only the new path and would hide the old one.
    """
    run = run or _run
    if event_name == "pull_request":
        base = "HEAD^1"
    elif push_before and push_before != ZERO_SHA:
        base = push_before
        try:
            run(["git", "fetch", "--no-tags", "--depth=1", "--filter=blob:none", "origin", base])
        except subprocess.CalledProcessError:
            return None
    else:
        return None
    try:
        output = run(["git", "diff", "--name-only", "--no-renames", base, "HEAD"])
    except subprocess.CalledProcessError:
        return None
    return [line for line in output.splitlines() if line]


def decide_tests(changed: list[str] | None, skipped: set[str]) -> tuple[bool, str]:
    if changed is None:
        return True, "could not list the changed files; running every test job"
    if not changed:
        return True, "no changed files listed; running every test job"
    needing = files_needing_tests(changed, skipped)
    if needing:
        shown = ", ".join(needing[:10]) + (f" and {len(needing) - 10} more" if len(needing) > 10 else "")
        return True, f"tests read changed files: {shown}"
    return False, f"all {len(changed)} changed files are on the skip list; test jobs skip and gate still reports"


def gate_failures(needs: Mapping[str, Mapping[str, object]]) -> list[str]:
    """Reasons the gate fails; empty when every needed job passed or was skipped on purpose."""
    failures = []
    changes = needs.get("changes", {})
    if changes.get("result") != "success":
        failures.append(f"changes: {changes.get('result', 'missing')}")
    outputs = changes.get("outputs")
    tests_wanted = not (isinstance(outputs, Mapping) and outputs.get("tests") == "false")
    for job, details in needs.items():
        if job == "changes":
            continue
        result = details.get("result")
        if result in ("failure", "cancelled"):
            failures.append(f"{job}: {result}")
        elif result == "skipped" and job in TEST_JOBS and tests_wanted:
            failures.append(f"{job}: skipped although changed files need the tests")
        elif result == "skipped" and job in ALWAYS_JOBS:
            failures.append(f"{job}: skipped although it runs on every change")
        elif result not in ("success", "skipped"):
            failures.append(f"{job}: {result}")
    for job in TEST_JOBS + ALWAYS_JOBS:
        if job not in needs:
            failures.append(f"{job}: not in gate's needs")
    return failures


def deploy_decision(repository: str, run_id: str, head_sha: str, run: Runner | None = None) -> tuple[bool, str]:
    """Publish and deploy only a CI run whose `test` job ran and passed, for the commit that is still main's tip.

    Main runs no longer cancel each other, so two can finish out of order. Requiring the tip means an older commit
    finishing after a newer one never publishes or deploys; the newer commit's own run does. deploy-ci.ps1 applies
    the same rule again on the tower, after the queue wait.
    """
    run = run or _run
    jobs = json.loads(run(["gh", "api", f"repos/{repository}/actions/runs/{run_id}/jobs?per_page=100"]))
    conclusions = [job.get("conclusion") for job in jobs.get("jobs", []) if job.get("name") == "test"]
    if conclusions != ["success"]:
        return False, f"CI run {run_id} did not run and pass `test` (conclusions: {conclusions}); docs-only pushes do not deploy"
    tip = run(["gh", "api", f"repos/{repository}/git/ref/heads/main", "--jq", ".object.sha"]).strip()
    if tip != head_sha:
        return False, f"main has moved to {tip}; {head_sha} is superseded and its own run does not publish or deploy"
    return True, f"{head_sha} passed `test` and is main's tip"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("changes", help="reads EVENT_NAME, PUSH_BEFORE and CI_SKIP_UNREAD_PATHS")
    commands.add_parser("gate", help="reads NEEDS (toJSON(needs))")
    deploy = commands.add_parser("deploy")
    deploy.add_argument("--repository", required=True)
    deploy.add_argument("--run-id", required=True)
    deploy.add_argument("--head-sha", required=True)
    args = parser.parse_args(argv)

    if args.command == "changes":
        changed = changed_files(os.environ.get("EVENT_NAME", ""), os.environ.get("PUSH_BEFORE", ""))
        tests, reason = decide_tests(changed, skip_paths(os.environ.get("CI_SKIP_UNREAD_PATHS", "")))
        print(reason)
        _write_output("tests", "true" if tests else "false")
        return 0
    if args.command == "gate":
        needs = json.loads(os.environ.get("NEEDS") or "{}")
        for job, details in needs.items():
            print(f"{job}: {details.get('result')}")
        failures = gate_failures(needs)
        for failure in failures:
            print(f"::error::gate: {failure}")
        return 1 if failures else 0
    allowed, reason = deploy_decision(args.repository, args.run_id, args.head_sha)
    print(reason)
    _write_output("deploy", "true" if allowed else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
