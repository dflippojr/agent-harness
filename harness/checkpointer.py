"""Checkpoint service (#261): take after a mutating turn, rewind, and fork. Policy lives here; git lives in
`checkpoints.py`. Every method blocks on git, so callers run it in a thread; the bus publishes to asyncio queues,
so methods return their event payloads and the caller emits them on the event loop."""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from . import projects
from .checkpoints import (CAP, Store, eligible, git_state, head_and_branch, put_git_state, ref_name,
                          reset_branch)
from .projects import GitError

log = logging.getLogger("harness.checkpointer")


class Checkpointer:
    def __init__(self, cfg, db, bus):
        self.cfg, self.db, self.bus = cfg, db, bus

    def store(self, s: dict) -> Store:
        from . import storage
        from .principal import session_user_id
        return Store(storage.checkpoints_dir(self.cfg, session_user_id(s)) / s["id"])

    # take -------------------------------------------------------------------------------------------------------
    def take(self, sid: str, only_if_changed: bool = False, stats: dict | None = None) -> dict | None:
        """Checkpoint the session's workspace now. Returns the "checkpoint" event payload (`turn`, or `status`
        "skipped" and a `reason`), or None when there was nothing to record. Never raises: a checkpoint is a
        convenience and must not fail the turn it describes. `stats`, when given, is filled with the trace span's
        attributes: `turn`, `files`, `bytes`, and a `skipped` code (never the reason text, which can hold paths)."""
        stats = {} if stats is None else stats
        s = self.db.get_session(sid)
        if s is None or not eligible(s):
            stats["skipped"] = "ineligible"
            return None
        store, workspace = self.store(s), Path(s["workspace"])
        turn = int(s.get("turn_seq") or 0) + 1
        stats["turn"] = turn
        try:
            head, branch = head_and_branch(workspace)
            sha = store.snapshot(workspace, sid, turn, head, branch, publish=False)
            stats.update(files=store.files, bytes=store.bytes)
            if only_if_changed and self._same_tree(store, sid, sha):
                stats["skipped"] = "unchanged"
                return None
            context = store.pack_context(s["context"])
        except (GitError, OSError, ValueError, subprocess.SubprocessError) as e:
            log.warning("checkpoint %s/%s failed: %s", sid, turn, e)
            stats["skipped"] = "snapshot_failed"
            return self._skipped(sid, f"the snapshot failed ({str(e)[-160:]})")
        # Quota is decided before anything existing is touched: a skipped snapshot leaves every checkpoint,
        # including the rewound-past ones a later rewind can still redo (one may share this turn's number).
        if not self._room_for(s, store, sha, len(context)):
            store.reclaim()                                 # drops the unnamed snapshot's objects
            stats["skipped"] = "over_quota"
            return self._skipped(sid, "the account is over its disk quota")
        stale = [c["turn"] for c in self.db.checkpoints(sid, hidden=True)]   # rewound past: replaced now
        capped = self.db.write(self._record, sid, turn, sha, head, branch, stale)
        store.delete(sid, [t for t in stale + capped if t != turn])
        store.keep(sid, turn, sha, context)                 # replaces a rewound-past ref and context of this turn
        if stale or capped:
            store.reclaim()
        if not self._within_quota(s, store, sid, turn):     # the account grew meanwhile; its quota check refuses writes
            log.warning("checkpoint %s/%s kept although the account is still over its quota", sid, turn)
        return {"turn": turn, "head": head[:12]}

    @staticmethod
    def _skipped(sid: str, reason: str) -> dict:
        log.info("checkpoint for %s skipped: %s", sid, reason)
        return {"status": "skipped", "reason": reason}

    def _same_tree(self, store: Store, sid: str, sha: str) -> bool:
        known = self.db.checkpoints(sid, hidden=None)
        if not known:
            return False
        trees = [store.tree_of_commit(c) for c in (sha, known[-1]["sha"])]
        return trees[0] == trees[1] != ""

    def _record(self, sid: str, turn: int, sha: str, head: str, branch: str, stale: list[int]) -> list[int]:
        """One transaction (pass to `db.write`): the new checkpoint replaces the rewound-past ones and the oldest
        beyond the cap. Returns the capped turns; the caller deletes their refs once this has committed."""
        self.db.delete_checkpoints(sid, stale)
        self.db.add_checkpoint(sid, turn, sha, head, branch)
        self.db.update_session(sid, turn_seq=turn)
        visible = self.db.checkpoints(sid, hidden=False)
        capped = [c["turn"] for c in visible[:max(0, len(visible) - CAP)]]
        self.db.delete_checkpoints(sid, capped)
        return capped

    def _quota(self, s: dict) -> tuple[str, int] | None:
        """(account, limit) for a member's session; None for the owner, who is only measured."""
        from .principal import OWNER_USER_ID, session_user_id
        uid = session_user_id(s)
        account = self.db.account_by_id(uid) if uid != OWNER_USER_ID else None
        return None if account is None else (uid, int(account["disk_quota_bytes"]))

    def _room_for(self, s: dict, store: Store, sha: str, context_bytes: int) -> bool:
        """Whether the new snapshot may be kept: the account is under quota now, or would be with this session's
        other checkpoints pruned and only the new one stored. Deletes nothing."""
        from .fileops import dir_size
        from .storage import account_usage_bytes
        quota = self._quota(s)
        if quota is None:
            return True
        uid, limit = quota
        used = account_usage_bytes(self.cfg, uid)
        return used < limit or used - dir_size(store.base) + store.size_of(sha) + context_bytes < limit

    def _within_quota(self, s: dict, store: Store, sid: str, keep: int) -> bool:
        """Members are capped; the owner is only measured. Prune this session's oldest checkpoints to make room,
        and report False when even that would not fit."""
        from .fileops import dir_size
        from .storage import account_usage_bytes
        quota = self._quota(s)
        if quota is None:
            return True
        uid, limit = quota
        used = account_usage_bytes(self.cfg, uid)
        if used < limit:
            return True
        if used - dir_size(store.base) >= limit:    # even an empty store would not fit
            return False
        for c in self.db.checkpoints(sid, hidden=None):
            if c["turn"] == keep:
                continue
            store.delete(sid, [c["turn"]])
            self.db.delete_checkpoints(sid, [c["turn"]])
            store.reclaim()
            if account_usage_bytes(self.cfg, uid) < limit:
                return True
        return account_usage_bytes(self.cfg, uid) < limit

    # rewind -----------------------------------------------------------------------------------------------------
    def checkpoint(self, sid: str, turn: int) -> dict:
        for c in self.db.checkpoints(sid, hidden=None):
            if c["turn"] == turn:
                return c
        raise GitError(f"session {sid} has no checkpoint {turn}", 404)

    def restore(self, sid: str, turn: int) -> list:
        """Restore the workspace to a checkpoint and return the model context saved with it; `commit_rewind`
        then records the rewind. The caller has checked that the session is idle and local.

        All or nothing: every file the restore must remove or replace is probed first, and a step that still
        fails puts the branch, index and files back as they were before raising (409, naming the files)."""
        s = self.db.get_session(sid)
        ckpt = self.checkpoint(sid, turn)
        store, workspace = self.store(s), Path(s["workspace"])
        context = store.load_context(turn)
        with tempfile.TemporaryDirectory(prefix="harness-ckpt-") as tmp:
            plan = store.plan(workspace, ckpt["sha"], Path(tmp))
            locked = store.busy(workspace, plan)
            if locked:
                raise GitError("nothing was rewound: files another program has open (close them and retry): "
                               + "; ".join(locked)[:600], 409)
            before = git_state(workspace, ckpt["branch"])
            store.hold(sid, plan.current)
            try:
                reset_branch(workspace, ckpt["head"], ckpt["branch"])
                failures = store.restore(workspace, ckpt["sha"], plan)
                if failures:
                    raise GitError("; ".join(failures)[:600], 409)
            except (GitError, OSError, subprocess.SubprocessError) as e:
                status = e.status if isinstance(e, GitError) and e.status != 500 else 409
                raise self._undo(sid, store, workspace, plan.current, before, e, status) from e
            finally:
                store.release(sid)
        return context

    @staticmethod
    def _undo(sid: str, store: Store, workspace: Path, tree: str, before: dict | None, error: Exception,
              status: int) -> GitError:
        """Put the workspace back as it was before a failed rewind; returns the error to raise."""
        try:
            put_git_state(workspace, before)
            failures = store.restore(workspace, tree)
        except (GitError, OSError, subprocess.SubprocessError) as e:
            failures = [str(e)]
        if failures:
            log.error("rewind of %s failed and could not be undone: %s; %s", sid, error, failures)
            return GitError(f"the rewind failed ({str(error)[:300]}) and the workspace could not be put back "
                            f"({'; '.join(failures)[:300]})", 500)
        return GitError(f"nothing was rewound: {str(error)[:600]}", status)

    def commit_rewind(self, sid: str, turn: int, context: list) -> None:
        """One transaction (pass to `db.write`/`awrite`): truncate the context, hide later checkpoints, and mark the
        event log. The log itself keeps the full history."""
        ckpt = self.checkpoint(sid, turn)
        self.db.update_session(sid, context=context, inbox=[], turn_seq=turn, review="", review_detail="",
                               answer="", stop_reason="")
        self.db.hide_checkpoints_after(sid, turn)
        self.bus.emit(sid, "rewound", {"turn": turn, "head": ckpt["head"][:12]})

    # fork -------------------------------------------------------------------------------------------------------
    def prepare_fork(self, parent: dict, turn: int, new_sid: str, workspace: Path) -> dict:
        """Build the fork's workspace at a checkpoint and import the checkpoint into the fork's own store, so it
        outlives pruning of the parent. Returns the git fields for the new session row."""
        ckpt = self.checkpoint(parent["id"], turn)
        source = self.store(parent)
        git_fields: dict = {}
        workspace.mkdir(parents=True, exist_ok=True)
        if ckpt["head"] and (Path(parent["workspace"]) / ".git").exists():
            git_fields = self._fork_clone(parent, ckpt, new_sid, workspace)
        fork = Store(source.base.parent / new_sid)
        fork.import_from(source, ckpt["sha"], ref_name(new_sid, turn))
        failures = fork.restore(workspace, ckpt["sha"])
        if failures:
            raise GitError("could not rebuild the workspace at that checkpoint: " + "; ".join(failures)[:600], 500)
        return git_fields

    @staticmethod
    def _fork_clone(parent: dict, ckpt: dict, new_sid: str, workspace: Path) -> dict:
        parent_ws = Path(parent["workspace"])
        origin = projects.git(parent_ws, "config", "--get", "remote.origin.url", check=False).out.strip()
        projects._run(["init", "-q", "-b", "main", str(workspace)], env=projects._isolate_env())
        projects.git(workspace, "config", "core.autocrlf", "false")
        if origin:
            projects.git(workspace, "remote", "add", "origin", origin)
        projects.git(workspace, "fetch", "-q", "--no-tags", str(parent_ws), ckpt["head"], timeout=900)
        branch = projects.branch_name(new_sid)
        projects.git(workspace, "checkout", "-q", "-b", branch, ckpt["head"])
        cfg = workspace / ".git" / "config"
        for key, value in (("user.name", projects.AGENT_NAME), ("user.email", projects.AGENT_EMAIL)):
            projects.git(None, "config", "--file", str(cfg), key, value)
        return {"branch": branch, "base_branch": parent.get("base_branch", ""),
                "base_commit": parent.get("base_commit", "")}

    def summary(self, parent: dict, turn: int, limit: int = 6000) -> str:
        """A digest of the parent's transcript up to the checkpoint, for hosted sessions whose CLI state cannot
        be forked. Built from the event log; no model call."""
        ckpt = self.checkpoint(parent["id"], turn)
        parts: list[str] = []
        for e in self.db.events(parent["id"]):
            if e["seq"] > ckpt["event_seq"]:
                break
            d = e["data"]
            if e["type"] == "user_message" and d.get("content"):
                parts.append(f"User: {str(d['content']).strip()}")
            elif e["type"] == "assistant" and str(d.get("content") or "").strip():
                parts.append(f"Agent: {str(d['content']).strip()}")
        text = "\n".join(parts)
        return text if len(text) <= limit else "…" + text[-limit:]
