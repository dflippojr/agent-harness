"""Per-turn workspace checkpoints (#261): snapshot, rewind and fork.

A checkpoint is a commit in a private bare repository that lives beside the account's workspaces, never inside
the workspace or the sandbox mount, so an agent cannot read, rewrite or delete it. The snapshot is built host-side
with a throwaway `GIT_INDEX_FILE`: the agent's branch, index and staging area are never touched. The same
mechanism serves git projects and scratch workspaces. Commits sit on the hidden ref
`refs/harness/checkpoints/<session>/<turn>` and are never pushed (review pushes only the session branch).

What a rewind restores: every file `git add -A` would see (tracked, untracked, deleted). Git-ignored files
(virtualenvs, build output) are neither snapshotted nor removed. A repository nested in the workspace (a clone)
is snapshotted as its plain files, never its own .git: restoring a checkpoint that has the clone brings its files
back without their history, and rewinding to before the clone removes its directory, .git included. For git
projects the session branch is also reset to the HEAD recorded with the checkpoint; a HEAD that was detached then
is detached again at that commit, and no branch moves. Anything outside the workspace (installed packages,
processes, images already written to artifacts) is not rewound.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import projects
from .projects import GitError

log = logging.getLogger("harness.checkpoints")

CAP = 50                      # visible checkpoints kept per session
MUTATING_TOOLS = ("run_shell", "write_file", "edit_file", "git_clone", "apply_delegated_edit", "generate_image")
# The run fields a turn writes (update_state, update_notes) that the next turn reads (`new_run` carries them, and
# compaction re-injects them): a checkpoint keeps them with its context, and rewind and fork restore them.
TURN_RUN_KEYS = ("state", "notes")
# A run field set when a rewind restored the files but could neither record itself nor put the files back: the
# workspace no longer matches the context, so a send is refused until a rewind succeeds (which clears it).
UNSETTLED = "rewind_unsettled"
REF_PREFIX = "refs/harness/checkpoints"
UNDO_PREFIX = "refs/harness/rewind-undo"     # the workspace as it was before a rewind, while that rewind runs
STAGE_PREFIX = "refs/harness/staged"         # a new checkpoint until its database record commits
REPLACED_PREFIX = "refs/harness/replaced"    # a rewound-past checkpoint a new one of its turn replaces, until then
GITLINK = 0o160000           # index mode of a nested repository
_INDEX_LOCKS: dict[str, threading.Lock] = {}     # one snapshot at a time per store: they share its index file


@dataclass
class Plan:
    """The file operations of one restore: `tree` to reach from `current`."""
    tree: str
    current: str
    dirs: list[str]          # nested repositories to remove whole
    extra: list[str]         # files to remove
    changed: list[str]       # files to write


def index_entries(raw: bytes):
    """(mode, file bytes, path) of each entry of a git index, read from the file itself rather than another git
    process. Only the version 2/3 layout git writes by default is parsed; another version yields nothing."""
    if len(raw) < 12 or raw[:4] != b"DIRC" or int.from_bytes(raw[4:8], "big") not in (2, 3):
        return
    version, count, pos = int.from_bytes(raw[4:8], "big"), int.from_bytes(raw[8:12], "big"), 12
    for _ in range(count):       # fixed 62-byte header (+2 extended flags), the path, NUL padding to 8 bytes
        if pos + 62 > len(raw):
            return
        mode, size = int.from_bytes(raw[pos + 24:pos + 28], "big"), int.from_bytes(raw[pos + 36:pos + 40], "big")
        flags = int.from_bytes(raw[pos + 60:pos + 62], "big")
        start = pos + 62 + (2 if version == 3 and flags & 0x4000 else 0)
        end = raw.find(b"\0", start)
        if end < 0:
            return
        yield mode, size, raw[start:end].decode("utf-8", "surrogateescape")
        pos += ((end - pos) // 8 + 1) * 8


def index_stats(raw: bytes) -> tuple[int, int]:
    """(entries, total file bytes) of a git index. Bytes are summed for the version 2/3 layout git writes by
    default; another version reports 0 bytes."""
    if len(raw) < 12 or raw[:4] != b"DIRC":
        return 0, 0
    return int.from_bytes(raw[8:12], "big"), sum(size for _, size, _ in index_entries(raw))


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
        self.files = self.bytes = 0   # what the last snapshot holds, for its trace span

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
        return self._build(workspace, index)[0]

    def _build(self, workspace: Path, index: Path) -> tuple[str, int, int, list[str]]:
        """Stage the workspace into `index` and write its tree: (tree, files, bytes, nested repository paths).
        A repository nested in the workspace (a clone) is staged as its plain files, recursively, where git alone
        records a gitlink or refuses one with no commit yet. The nested repository's own .git is never stored."""
        added = self._git(workspace, index, "add", "-A", "--", ".", check=False)
        raw = index.read_bytes() if added.code == 0 and index.is_file() else b""
        if added.code == 0 and not any(mode == GITLINK for mode, _, _ in index_entries(raw)):
            tree = self._git(workspace, index, "write-tree").out.strip()
            return (tree, *index_stats(raw), [])
        index.unlink(missing_ok=True)
        listed = self._git(workspace, index, "ls-files", "-o", "-z", "--exclude-standard").out
        top = [n[:-1] for n in self._names(listed) if n.endswith("/")]      # git lists a nested repository as "dir/"
        if not top:
            raise GitError(f"could not stage the workspace: {added.text[-300:]}")
        spec = ".\0" + "".join(f":(exclude,literal){rel}\0" for rel in top)
        self._git(workspace, index, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul", input_=spec)
        nested_bytes, nested = 0, list(top)
        for i, rel in enumerate(top):
            tree, _, size, inner = self._build(workspace / rel, index.with_name(f"{index.name}.{i}"))
            self._git(workspace, index, "read-tree", f"--prefix={rel}/", tree)
            nested_bytes += size        # entries read from a tree carry no size; their own index had it
            nested += [f"{rel}/{n}" for n in inner]
        tree = self._git(workspace, index, "write-tree").out.strip()
        files, size = index_stats(index.read_bytes())
        return tree, files, size + nested_bytes, nested

    # snapshot ---------------------------------------------------------------------------------------------------
    def snapshot(self, workspace: Path, sid: str, turn: int, head: str, branch: str, publish: bool = True,
                 unless_tree: str = "") -> str:
        """Commit the workspace's current files (ignored ones excluded) to the hidden ref; returns the commit sha.
        With `publish` False no ref is written until `stage` and `Store.publish` name the commit, so a rewound-past
        checkpoint with the same turn number survives a snapshot that is then dropped. Returns "" without committing when the files
        are exactly `unless_tree`: staging them then wrote no object the repository did not already hold."""
        self.init()
        with _INDEX_LOCKS.setdefault(str(self.base), threading.Lock()):
            tree = self._tree_kept(workspace)
            if unless_tree and tree == unless_tree:
                return ""
            message = f"checkpoint {turn}\n\nHarness-Session: {sid}\nHarness-Turn: {turn}\nHead: {head}\nBranch: {branch}\n"
            sha = self._git(None, None, "commit-tree", tree, input_=message).out.strip()
        if publish:
            self._git(None, None, "update-ref", ref_name(sid, turn), sha)
        return sha

    def _tree_kept(self, workspace: Path) -> str:
        """The workspace's tree, staged into the store's own index, which survives between snapshots so `git add`
        re-hashes only the files whose size or time changed. It is thrown away and rebuilt from nothing when it
        cannot be trusted: a failed or interrupted git call, or a tracked file that is now ignored (a fresh index
        never holds one)."""
        index = self.base / "index"
        for stale in self.base.glob("index.*"):      # leftovers of a nested build or an interrupted git
            stale.unlink(missing_ok=True)
        reused = index.is_file()
        try:
            if reused and self._git(workspace, index, "ls-files", "-c", "-i", "--exclude-standard", "-z").out:
                index.unlink()
            tree, self.files, self.bytes, _ = self._build(workspace, index)
            return tree
        except BaseException:
            index.unlink(missing_ok=True)
            raise
        finally:
            for stale in self.base.glob("index.*"):
                stale.unlink(missing_ok=True)

    def stage(self, sid: str, turn: int, sha: str, context: bytes) -> None:
        """Hold a snapshot taken with `publish` False, with its `pack_context`ed model context, under temporary
        names: a rewound-past checkpoint of the same turn stays untouched until `publish` renames them over it;
        `unstage` drops them instead."""
        self._git(None, None, "update-ref", f"{STAGE_PREFIX}/{sid}/{turn}", sha)
        self.contexts.mkdir(parents=True, exist_ok=True)
        (self.contexts / f"staged-{turn}.json.gz").write_bytes(context)

    def publish(self, sid: str, turn: int, sha: str, done: set) -> None:
        """Name a staged snapshot as checkpoint `turn`. A rewound-past checkpoint of that number is set aside, not
        overwritten, so until `drop_replaced` removes it `unpublish` can put it back. Each step taken is added to
        `done`, which `unpublish` reads."""
        ref, aside = ref_name(sid, turn), f"{REPLACED_PREFIX}/{sid}/{turn}"
        context, staged = self.contexts / f"{turn}.json.gz", self.contexts / f"staged-{turn}.json.gz"
        old = self._git(None, None, "rev-parse", "--verify", "-q", ref, check=False).out.strip()
        if old:
            self._git(None, None, "update-ref", aside, old)
            done.add("ref_aside")
        if context.exists():
            os.replace(context, self.contexts / f"replaced-{turn}.json.gz")
            done.add("context_aside")
        self._git(None, None, "update-ref", ref, sha)
        done.add("ref")
        os.replace(staged, context)
        done.add("context")

    def unpublish(self, sid: str, turn: int, done: set) -> None:
        """Undo the steps of `publish` in `done` (putting back what it set aside) and drop the staged names."""
        ref, aside = ref_name(sid, turn), f"{REPLACED_PREFIX}/{sid}/{turn}"
        context = self.contexts / f"{turn}.json.gz"
        if "context" in done:
            context.unlink(missing_ok=True)
        if "context_aside" in done:
            os.replace(self.contexts / f"replaced-{turn}.json.gz", context)
        if "ref" in done:
            if "ref_aside" in done:
                old = self._git(None, None, "rev-parse", "--verify", "-q", aside).out.strip()
                self._git(None, None, "update-ref", ref, old)
            else:
                self._git(None, None, "update-ref", "-d", ref)
        if "ref_aside" in done:
            self._git(None, None, "update-ref", "-d", aside, check=False)
        self.unstage(sid, turn)

    def drop_replaced(self, sid: str, turn: int) -> None:
        """Once the new checkpoint's record has committed: delete what `publish` set aside and the staged ref."""
        self._git(None, None, "update-ref", "-d", f"{REPLACED_PREFIX}/{sid}/{turn}", check=False)
        (self.contexts / f"replaced-{turn}.json.gz").unlink(missing_ok=True)
        self._git(None, None, "update-ref", "-d", f"{STAGE_PREFIX}/{sid}/{turn}", check=False)

    def unstage(self, sid: str, turn: int) -> None:
        self._git(None, None, "update-ref", "-d", f"{STAGE_PREFIX}/{sid}/{turn}", check=False)
        (self.contexts / f"staged-{turn}.json.gz").unlink(missing_ok=True)

    def size_of(self, sha: str) -> int:
        """Bytes the objects of this snapshot would take on their own; 0 when git cannot tell."""
        result = self._git(None, None, "rev-list", "--objects", "--disk-usage", sha, check=False)
        return int(result.out.strip()) if result.code == 0 and result.out.strip().isdigit() else 0

    def tree_of_commit(self, sha: str) -> str:
        return self._git(None, None, "rev-parse", f"{sha}^{{tree}}", check=False).out.strip()

    def delete(self, sid: str, turns: list[int]) -> None:
        for turn in turns:
            self._git(None, None, "update-ref", "-d", ref_name(sid, turn), check=False)
            (self.contexts / f"{turn}.json.gz").unlink(missing_ok=True)

    def reclaim(self) -> None:
        """Drop objects no hidden ref reaches any more, and the kept index (about 90 bytes a file, rebuilt by the
        next snapshot). Best effort: a failure only costs disk."""
        (self.base / "index").unlink(missing_ok=True)
        if (self.repo / "HEAD").exists():
            self._git(None, None, "reflog", "expire", "--expire=now", "--all", check=False)
            self._git(None, None, "gc", "--prune=now", "--quiet", check=False)

    # model context ----------------------------------------------------------------------------------------------
    @staticmethod
    def pack_context(context: list, run: dict | None = None) -> bytes:
        """The model context and the `TURN_RUN_KEYS` of the session's run, as one file."""
        carried = {k: run[k] for k in TURN_RUN_KEYS if k in (run or {})}
        return gzip.compress(json.dumps({"context": context, "run": carried}).encode("utf-8"))

    def save_context(self, turn: int, context: list, run: dict | None = None) -> None:
        self.contexts.mkdir(parents=True, exist_ok=True)
        (self.contexts / f"{turn}.json.gz").write_bytes(self.pack_context(context, run))

    def load_turn(self, turn: int) -> tuple[list, dict]:
        """The model context and the run fields (`TURN_RUN_KEYS`) saved with a checkpoint. A checkpoint saved
        before the run fields were kept gives none, so a rewind clears them rather than keep later ones."""
        path = self.contexts / f"{turn}.json.gz"
        if not path.is_file():
            raise GitError(f"checkpoint {turn} has no saved context", 410)
        saved = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        if isinstance(saved, list):
            return saved, {}
        return saved["context"], {k: v for k, v in (saved.get("run") or {}).items() if k in TURN_RUN_KEYS}

    def load_context(self, turn: int) -> list:
        return self.load_turn(turn)[0]

    # restore ----------------------------------------------------------------------------------------------------
    def plan(self, workspace: Path, sha: str, tmp: Path) -> Plan:
        """What restoring `sha` (a commit or tree) must change in the workspace, without changing anything."""
        current, _, _, nested = self._build(workspace, tmp / "now")
        tree = self._git(None, None, "rev-parse", f"{sha}^{{tree}}").out.strip()
        extra = self._names(self._git(None, None, "diff-tree", "-r", "-z", "--no-renames", "--name-only",
                                      "--diff-filter=A", tree, current).out)
        changed = self._names(self._git(None, None, "diff-tree", "-r", "-z", "--no-renames", "--name-only",
                                        "--diff-filter=MDT", tree, current).out)
        return Plan(tree, current, self._absent_dirs(tree, nested), extra, changed)

    @staticmethod
    def busy(workspace: Path, plan: Plan) -> list[str]:
        """The paths `plan` must remove or replace that another process holds open. Windows refuses to rename a
        file (or a folder holding one) opened without delete sharing, as it refuses to remove or replace it, so
        each is renamed aside and straight back: nothing changes, and nothing is half done when one is locked. The
        aside name is a fresh one that does not exist (POSIX rename would replace it), so no other file is touched."""
        locked: list[str] = []
        for rel in [*plan.dirs, *plan.extra, *plan.changed]:
            path = workspace / rel
            if not os.path.lexists(path):
                continue
            aside = path.with_name(f".{path.name}.{uuid.uuid4().hex}.harness-probe")
            while os.path.lexists(aside):
                aside = path.with_name(f".{path.name}.{uuid.uuid4().hex}.harness-probe")
            try:
                os.rename(path, aside)
            except OSError as e:
                locked.append(f"{rel}: {e.strerror or e}")
                continue
            try:
                os.rename(aside, path)
            except OSError as e:
                raise GitError(f"could not put {rel} back after probing it (it is at {aside.name}): "
                               f"{e.strerror or e}", 500) from e
        return locked

    def restore(self, workspace: Path, sha: str, plan: Plan | None = None) -> list[str]:
        """Make the workspace's files match the checkpoint (`plan`, when given, is this restore's own `plan`).
        Returns the paths that could not be fixed (locked files on Windows, for instance); an empty list means a
        verified exact restore."""
        failures: list[str] = []
        with tempfile.TemporaryDirectory(prefix="harness-ckpt-") as tmp:
            plan = plan or self.plan(workspace, sha, Path(tmp))
            for rel in plan.dirs:                           # a clone made after the checkpoint goes, .git and all
                failures += self._remove_dir(workspace, rel)
            for rel in plan.extra:
                failures += self._remove(workspace, rel)
            if plan.changed:
                want_index = Path(tmp) / "want"
                self._git(workspace, want_index, "read-tree", plan.tree)
                result = self._git(workspace, want_index, "checkout-index", "-f", "-z", "--stdin",
                                   input_="\0".join(plan.changed) + "\0", check=False)
                if result.code != 0:
                    failures.append(result.text[-300:])
            after = self._tree_of(workspace, Path(tmp) / "after")
            if after != plan.tree and not failures:
                failures.append("the workspace still differs from the checkpoint after restoring")
        return failures

    def hold(self, sid: str, tree: str) -> None:
        """Keep a tree (the workspace just before a rewind) from `reclaim` until `release`."""
        self._git(None, None, "update-ref", f"{UNDO_PREFIX}/{sid}", tree)

    def release(self, sid: str) -> None:
        self._git(None, None, "update-ref", "-d", f"{UNDO_PREFIX}/{sid}", check=False)

    @staticmethod
    def _names(raw: str) -> list[str]:
        return [n for n in raw.split("\0") if n]

    def _absent_dirs(self, tree: str, paths: list[str]) -> list[str]:
        """Those of `paths` that are not a directory in `tree`."""
        if not paths:
            return []
        listed = self._names(self._git(None, None, "ls-tree", "-z", tree, "--", *paths).out)
        present = {entry.split("\t", 1)[1] for entry in listed if entry.split(" ")[1:2] == ["tree"]}
        return [p for p in paths if p not in present]

    @staticmethod
    def _remove(workspace: Path, rel: str) -> list[str]:
        path = workspace / rel
        try:
            path.unlink(missing_ok=True)
            Store._prune_empty(workspace, path.parent)
        except OSError as e:
            return [f"{rel}: {e}"]
        return []

    @staticmethod
    def _remove_dir(workspace: Path, rel: str) -> list[str]:
        from .maintenance import remove_tree
        path = workspace / rel
        try:
            remove_tree(path)
            Store._prune_empty(workspace, path.parent)
        except OSError as e:
            return [f"{rel}: {e}"]
        return []

    @staticmethod
    def _prune_empty(workspace: Path, parent: Path) -> None:
        while parent != workspace and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    # fork -------------------------------------------------------------------------------------------------------
    def import_from(self, other: "Store", sha: str, ref: str) -> None:
        """Copy one checkpoint (and its objects) from another session's store, so the fork outlives pruning."""
        self.init()
        self._git(None, None, "fetch", "--quiet", "--no-tags", str(other.repo), f"{sha}:{ref}")


def reset_branch(workspace: Path, head: str, branch: str) -> None:
    """Point the session branch and the index at the commit recorded with the checkpoint, leaving the files to
    `Store.restore` (so every file change of a rewind is planned and probed first). No branch means HEAD was
    detached then: detach it at that commit again and leave every branch where it is."""
    if not head or not (workspace / ".git").exists():
        return
    if projects.git(workspace, "cat-file", "-e", f"{head}^{{commit}}", check=False).code != 0:
        raise GitError(f"commit {head[:12]} recorded with this checkpoint is no longer in the workspace", 410)
    if branch:
        projects.git(workspace, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    else:
        projects.git(workspace, "update-ref", "--no-deref", "HEAD", head)
    projects.git(workspace, "reset", "-q", head)         # moves the branch (or detached HEAD) and the index only


def git_state(workspace: Path, branch: str) -> dict | None:
    """What `reset_branch` changes, for `put_git_state` to undo: HEAD, the index, and the branch's tip."""
    if not (workspace / ".git").exists():
        return None
    index_dir, metadata, _ = projects._resolve_workspace_git(workspace)
    index = index_dir / "index"
    tip = projects.git(workspace, "rev-parse", "-q", "--verify", f"refs/heads/{branch}^{{commit}}",
                       check=False).out.strip() if branch else ""
    return {"head": (metadata / "HEAD").read_bytes(), "index": index.read_bytes() if index.is_file() else None,
            "branch": branch, "tip": tip}


def put_git_state(workspace: Path, state: dict | None) -> None:
    if state is None:
        return
    index_dir, metadata, _ = projects._resolve_workspace_git(workspace)
    if state["branch"]:
        if state["tip"]:
            projects.git(workspace, "update-ref", f"refs/heads/{state['branch']}", state["tip"])
        else:
            projects.git(workspace, "update-ref", "-d", f"refs/heads/{state['branch']}", check=False)
    (metadata / "HEAD").write_bytes(state["head"])
    if state["index"] is None:
        (index_dir / "index").unlink(missing_ok=True)
    else:
        (index_dir / "index").write_bytes(state["index"])


def head_and_branch(workspace: Path) -> tuple[str, str]:
    """HEAD's sha and branch name: ("", "") for a scratch workspace or an unborn branch, (sha, "") for a detached
    HEAD. One isolated git call: each sets up a throwaway GIT_DIR, which costs several processes."""
    if not (workspace / ".git").exists():
        return "", ""
    quick = _read_head(workspace)
    if quick is not None:
        return quick
    result = projects.git(workspace, "rev-parse", "HEAD", "--abbrev-ref", "HEAD", check=False)
    lines = result.out.split()
    if result.code != 0 or len(lines) != 2:
        return "", ""
    return lines[0], "" if lines[1] == "HEAD" else lines[1]     # git names a detached HEAD "HEAD", never a branch


def _read_head(workspace: Path) -> tuple[str, str] | None:
    """`head_and_branch` read from the repository's files, with no git process (about 0.4 s saved on this tower).
    None when the layout is anything but a plain branch with a loose or packed ref, or a detached full sha; the
    caller then asks git."""
    try:
        _, metadata, _ = projects._resolve_workspace_git(workspace)
        head = (metadata / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: refs/heads/"):
            return (head, "") if len(head) in (40, 64) and all(c in "0123456789abcdef" for c in head) else None
        ref = head[5:]
        loose = metadata / ref
        sha = loose.read_text(encoding="utf-8").strip() if loose.is_file() else ""
        if not sha:
            packed = metadata / "packed-refs"
            for line in packed.read_text(encoding="utf-8").splitlines() if packed.is_file() else []:
                if line.endswith(" " + ref) and not line.startswith(("#", "^")):
                    sha = line.split(" ", 1)[0]
        if len(sha) not in (40, 64) or not all(c in "0123456789abcdef" for c in sha):
            return None                                     # unborn, or a symbolic or unusual ref
        return sha, ref[len("refs/heads/"):]
    except (OSError, UnicodeDecodeError, GitError):
        return None


def remove_store(base: Path) -> None:
    shutil.rmtree(base, ignore_errors=True)
