"""Resolve a staging dispatch to one immutable commit, or fail closed before candidate code runs.

Staging is a trusted-code smoke slot (issue #129): only branches of dflippojr/agent-harness and pull requests
whose head repository is that same repository may be deployed. This runs on a GitHub-hosted runner, before the
staging runner checks out or executes anything, so a rejected ref never reaches the tower.

    python scripts/resolve_staging_ref.py --branch feat/84-chat-home
    python scripts/resolve_staging_ref.py --pr-number 131
    python scripts/resolve_staging_ref.py --reset

Writes `sha`, `ref_label`, and `reset_only` to $GITHUB_OUTPUT when that variable is set.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Callable

REPOSITORY = "dflippojr/agent-harness"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class RefRejected(Exception):
    """The dispatch is not an allowed staging target."""


def gh_api(path: str) -> dict:
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RefRejected(f"gh api {path} failed: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout)


def resolve(
    branch: str = "",
    pr_number: str = "",
    reset: bool = False,
    api: Callable[[str], dict] = gh_api,
    repository: str = REPOSITORY,
) -> dict:
    """The resolved dispatch: {"sha", "ref_label", "reset_only"}. Raises RefRejected for anything else."""
    branch, pr_number = branch.strip(), pr_number.strip()
    if reset:
        if branch or pr_number:
            raise RefRejected("reset must be dispatched on its own, without branch or pr_number")
        return {"sha": "", "ref_label": "reset", "reset_only": True}
    if bool(branch) == bool(pr_number):
        raise RefRejected("set exactly one of branch or pr_number (or dispatch reset on its own)")

    if branch:
        if branch.startswith("-") or ".." in branch or any(c.isspace() for c in branch):
            raise RefRejected(f"branch is not a usable ref name: {branch!r}")
        ref = api(f"repos/{repository}/git/ref/heads/{branch}")
        sha = ((ref.get("object") or {}).get("sha") or "").strip()
        if not SHA_PATTERN.match(sha):
            raise RefRejected(f"branch {branch} did not resolve to a commit SHA")
        if ((ref.get("object") or {}).get("type") or "commit") != "commit":
            raise RefRejected(f"branch {branch} does not point at a commit")
        return {"sha": sha, "ref_label": f"branch {branch}", "reset_only": False}

    if not pr_number.isdigit():
        raise RefRejected(f"pr_number must be a number: {pr_number!r}")
    pull = api(f"repos/{repository}/pulls/{pr_number}")
    head = pull.get("head") or {}
    head_repo = (head.get("repo") or {}).get("full_name") or ""
    if head_repo.casefold() != repository.casefold():
        # Fork pull requests are out of scope: staging runs candidate code with the owner's own tower account.
        raise RefRejected(
            f"pull request #{pr_number} head repository is {head_repo or 'unknown'}; only {repository} is allowed")
    sha = (head.get("sha") or "").strip()
    if not SHA_PATTERN.match(sha):
        raise RefRejected(f"pull request #{pr_number} did not resolve to a commit SHA")
    return {"sha": sha, "ref_label": f"pull request #{pr_number}", "reset_only": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", default="")
    parser.add_argument("--pr-number", default="")
    parser.add_argument("--reset", default="")
    parser.add_argument("--repository", default=REPOSITORY)
    args = parser.parse_args(argv)

    try:
        resolved = resolve(args.branch, args.pr_number, args.reset.strip().lower() in ("1", "true", "yes"),
                           repository=args.repository)
    except RefRejected as exc:
        print(f"staging dispatch rejected: {exc}", file=sys.stderr)
        return 1

    if resolved["reset_only"]:
        print("staging reset requested; no candidate commit will be deployed")
    else:
        print(f"staging target {resolved['ref_label']} resolved to {resolved['sha']}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"sha={resolved['sha']}\n")
            handle.write(f"ref_label={resolved['ref_label']}\n")
            handle.write(f"reset_only={'true' if resolved['reset_only'] else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
