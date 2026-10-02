"""Per-turn workspace checkpoints (#261): snapshot, rewind and fork.

A checkpoint is a commit in a private bare repository that lives beside the account's workspaces, never inside
the workspace or the sandbox mount, so an agent cannot read, rewrite or delete it. The snapshot is built host-side
with a throwaway `GIT_INDEX_FILE`: the agent's branch, index and staging area are never touched. The same
mechanism serves git projects and scratch workspaces. Commits sit on the hidden ref
`refs/harness/checkpoints/<session>/<turn>` and are never pushed (review pushes only the session branch).

What a rewind restores: every file `git add -A` would see (tracked, untracked, deleted). Git-ignored files
(virtualenvs, build output) are neither snapshotted nor removed. For git projects the session branch is also
reset to the HEAD recorded with the checkpoint. Anything outside the workspace (installed packages, processes,
images already written to artifacts) is not rewound.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from . import projects
from .projects import GitError

log = logging.getLogger("harness.checkpoints")

CAP = 50                      # visible checkpoints kept per session
MUTATING_TOOLS = ("run_shell", "write_file", "edit_file", "git_clone", "delegate_apply")
REF_PREFIX = "refs/harness/checkpoints"


def ref_name(sid: str, turn: int) -> str:
    return f"{REF_PREFIX}/{sid}/{turn}"


def eligible(s: dict) -> bool:
    """Tower sessions with a workspace on disk. Mac Runner sessions are out of scope (the protocol has no snapshot ops)."""
    return (s.get("target") == "tower" and s.get("kind", "agent") == "agent" and not s.get("workspace_removed")
            and Path(s["workspace"]).is_dir())


class Store:
    """The checkpoint repository of one session: `<base>/repo.git` plus gzipped model contexts."""

    def __init__(self, base: Path):
        self.base = Path(base)
        self.repo = self.base / "repo.git"
        self.contexts = self.base / "context"

    # git plumbing -----------------------------------------------------------------------------------------------
    def _git(self, workspace: Path | None, index: Path | None, *args: str, input_: str | None = None,
             check: bool = True) -> projects.GitResult:
        env = projects._isolate_env()
        env.update({"GIT_DIR": str(self.repo), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_AUTHOR_NAME": projects.AGENT_NAME, "GIT_AUTHOR_EMAIL": projects.AGENT_EMAIL,
                    "GIT_COMMITTER_NAME": projects.AGENT_NAME, "GIT_COMMITTER_EMAIL": projects.AGENT_EMAIL})
        if workspace is not None:
            env["GIT_WORK_TREE"] = str(workspace)
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        flags = ["-c", "core.autocrlf=false", "-c", "core.fsmonitor=", "-c", "core.longpaths=true",
                 "-c", "core.quotepath=off", "-c", "core.safecrlf=false", "-c", "gc.auto=0"]
        return projects._run(flags + list(args), check=check, input_=input_, env=env,
                             cwd=str(workspace) if workspace is not None else None)

    def init(self) -> None:
        if (self.repo / "HEAD").exists():
            return
        self.base.mkdir(parents=True, exist_ok=True)
        self.contexts.mkdir(parents=True, exist_ok=True)
        projects._run(["init", "--bare", "-q", str(self.repo)], env=projects._isolate_env())

    def _tree_of(self, workspace: Path, index: Path) -> str:
        self._git(workspace, index, "add", "-A", "--", ".")
        return self._git(workspace, index, "write-tree").out.strip()

    # snapshot ---------------------------------------------------------------------------------------------------
    def snapshot(self, workspace: Path, sid: str, turn: int, head: str, branch: str) -> str:
        """Commit the workspace's current files (ignored ones excluded) to the hidden ref; returns the commit sha."""
        self.init()
        with tempfile.TemporaryDirectory(prefix="harness-ckpt-") as tmp:
            index = Path(tmp) / "index"
            tree = self._tree_of(workspace, index)
            message = f"checkpoint {turn}\n\nHarness-Session: {sid}\nHarness-Turn: {turn}\nHead: {head}\nBranch: {branch}\n"
            sha = self._git(None, None, "commit-tree", tree, input_=message).out.strip()
        self._git(None, None, "update-ref", ref_name(sid, turn), sha)
        return sha

    def delete(self, sid: str, turns: list[int]) -> None:
        for turn in turns:
            self._git(None, None, "update-ref", "-d", ref_name(sid, turn), check=False)
            (self.contexts / f"{turn}.json.gz").unlink(missing_ok=True)

    def reclaim(self) -> None:
        """Drop objects no hidden ref reaches any more. Best effort: a failure only costs disk."""
        if (self.repo / "HEAD").exists():
            self._git(None, None, "reflog", "expire", "--expire=now", "--all", check=False)
            self._git(None, None, "gc", "--prune=now", "--quiet", check=False)

    # model context ----------------------------------------------------------------------------------------------
    def save_context(self, turn: int, context: list) -> None:
        self.contexts.mkdir(parents=True, exist_ok=True)
        (self.contexts / f"{turn}.json.gz").write_bytes(gzip.compress(json.dumps(context).encode("utf-8")))

    def load_context(self, turn: int) -> list:
        path = self.contexts / f"{turn}.json.gz"
        if not path.is_file():
            raise GitError(f"checkpoint {turn} has no saved context", 410)
        return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))

    # restore ----------------------------------------------------------------------------------------------------
    def restore(self, workspace: Path, sha: str) -> list[str]:
        """Make the workspace's files match the checkpoint. Returns the paths that could not be fixed (locked
        files on Windows, for instance); an empty list means a verified exact restore."""
        failures: list[str] = []
        with tempfile.TemporaryDirectory(prefix="harness-ckpt-") as tmp:
            now_index, want_index = Path(tmp) / "now", Path(tmp) / "want"
            current = self._tree_of(workspace, now_index)
            tree = self._git(None, None, "rev-parse", f"{sha}^{{tree}}").out.strip()
            extra = self._names(self._git(None, None, "diff-tree", "-r", "-z", "--no-renames", "--name-only",
                                          "--diff-filter=A", tree, current).out)
            changed = self._names(self._git(None, None, "diff-tree", "-r", "-z", "--no-renames", "--name-only",
                                            "--diff-filter=MDT", tree, current).out)
            for rel in extra:
                failures += self._remove(workspace, rel)
            if changed:
                self._git(workspace, want_index, "read-tree", tree)
                result = self._git(workspace, want_index, "checkout-index", "-f", "-z", "--stdin",
                                   input_="\0".join(changed) + "\0", check=False)
                if result.code != 0:
                    failures.append(result.text[-300:])
            after = self._tree_of(workspace, Path(tmp) / "after")
            if after != tree and not failures:
                failures.append("the workspace still differs from the checkpoint after restoring")
        return failures

    @staticmethod
    def _names(raw: str) -> list[str]:
        return [n for n in raw.split("\0") if n]

    @staticmethod
    def _remove(workspace: Path, rel: str) -> list[str]:
        path = workspace / rel
        try:
            path.unlink(missing_ok=True)
            parent = path.parent
            while parent != workspace and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        except OSError as e:
            return [f"{rel}: {e}"]
        return []

    # fork -------------------------------------------------------------------------------------------------------
    def import_from(self, other: "Store", sha: str, ref: str) -> None:
        """Copy one checkpoint (and its objects) from another session's store, so the fork outlives pruning."""
        self.init()
        self._git(None, None, "fetch", "--quiet", "--no-tags", str(other.repo), f"{sha}:{ref}")


def reset_branch(workspace: Path, head: str, branch: str) -> None:
    """Point the session branch (and its working tree) at the commit recorded with the checkpoint."""
    if not head or not (workspace / ".git").exists():
        return
    if projects.git(workspace, "cat-file", "-e", f"{head}^{{commit}}", check=False).code != 0:
        raise GitError(f"commit {head[:12]} recorded with this checkpoint is no longer in the workspace", 410)
    if branch:
        projects.git(workspace, "checkout", "-q", "-f", "-B", branch, head)
    else:
        projects.git(workspace, "reset", "-q", "--hard", head)


def head_and_branch(workspace: Path) -> tuple[str, str]:
    if not (workspace / ".git").exists():
        return "", ""
    head = projects.git(workspace, "rev-parse", "HEAD", check=False)
    branch = projects.git(workspace, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    return (head.out.strip() if head.code == 0 else "", branch.out.strip() if branch.code == 0 else "")


def remove_store(base: Path) -> None:
    shutil.rmtree(base, ignore_errors=True)
