"""The agent loop.

The loop is re-entrant: every step reads the session from SQLite and commits its result before the next step,
so after a daemon restart `run()` picks up wherever the session stopped (a model call, a tool call waiting for
approval, or a tool call that was interrupted mid-run).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from . import compaction, llm
from .bus import EventBus
from .config import Config
from .db import Database
from .policy import ALLOW, ASK, Policy
from .sandbox import Sandbox, SandboxUnavailable
from .scheduler import GpuScheduler
from .tools import ToolError, Workspace, tool_schemas, truncate_middle, validate_args
from .warmup import EXPECTED_WAKE_SECONDS, SLEEPING, WAKING, ModelWarmer

log = logging.getLogger("harness.runner")

SYSTEM_PROMPT = """You are a software agent working in a project workspace on the user's home server.
You act only through the provided tools. File paths are relative to the workspace root (/workspace in the sandbox).
Shell commands run in a Linux container with Python 3.12, pytest, and git, and no network access. Commands that need the network (package installs, downloads) must set network: true; the user approves those, and may also be asked to approve pushes and deletions. If the user denies an action, don't retry it: find another way or explain what you need.

Work methodically: look around before editing, prefer `search` over reading large files in full, and verify changes by running the relevant command or tests. On long tasks older conversation may be condensed, so keep intermediate results and progress in `update_notes`.
When the task is complete, reply with your final answer (or call `finish`). Don't answer until the work is done and verified. The user often reads answers on a phone, so lead with the result."""

ACTIVE = ("queued", "running", "waiting_approval")
INTERRUPTED = ("Error: the daemon restarted while this tool call was running, so its effects are unknown. "
               "Check the workspace state before retrying.")


def new_run(carry: dict | None = None) -> dict:
    """Counters for one run. Notes and the token calibration belong to the session, so they carry over."""
    run = {"turns": 0, "tool_calls": 0, "invalid_tool_calls": 0, "tool_errors": 0, "prompt_tokens": 0,
           "completion_tokens": 0, "idle": 0, "executing": None, "started_at": time.time()}
    for key in ("notes", "chars_per_token"):
        if carry and key in carry:
            run[key] = carry[key]
    return run


def unresolved_calls(context: list[dict]) -> list[dict]:
    i = len(context) - 1
    while i >= 0 and context[i]["role"] == "tool":
        i -= 1
    if i < 0 or context[i]["role"] != "assistant" or not context[i].get("tool_calls"):
        return []
    done = {m.get("tool_call_id") for m in context[i + 1:]}
    return [c for c in context[i]["tool_calls"] if c["id"] not in done]


class Runner:
    def __init__(self, cfg: Config, db: Database, bus: EventBus, scheduler: GpuScheduler, chat=llm.chat,
                 warmer: ModelWarmer | None = None):
        self.cfg = cfg
        self.warmer = warmer or ModelWarmer()
        self.db = db
        self.bus = bus
        self.scheduler = scheduler
        self.chat = chat
        self.approval_events: dict[str, asyncio.Event] = {}
        self.user_cancelled: set[str] = set()
        self._sandboxes: dict[str, Sandbox] = {}

    # helpers
    def sandbox(self, s: dict) -> Sandbox:
        if s["id"] not in self._sandboxes:
            project = self.cfg.projects.get(s["project"])
            sb_cfg = self.cfg.sandbox
            if project and project.sandbox:
                sb_cfg = type(sb_cfg)(**{**sb_cfg.__dict__, **project.sandbox})
            self._sandboxes[s["id"]] = Sandbox(s["id"], Path(s["workspace"]), sb_cfg)
        return self._sandboxes[s["id"]]

    def workspace(self, s: dict) -> Workspace:
        model = self.cfg.models[s["model"]]
        return Workspace(Path(s["workspace"]), self.sandbox(s), self.cfg.repos_dir, model.context_tokens)

    def policy(self, s: dict) -> Policy:
        project = self.cfg.projects.get(s["project"])
        return Policy(project.rules if project else [])

    def set_status(self, sid: str, status: str, **fields) -> None:
        with self.db.tx():
            self.db.update_session(sid, status=status, **fields)
            self.bus.emit(sid, "status", {"status": status, **{k: v for k, v in fields.items()
                                                                 if k in ("stop_reason", "answer")}})

    async def _acquire(self, sid: str) -> None:
        if self.scheduler.holder == sid:
            return
        s = self.db.get_session(sid)
        if s["status"] != "queued":
            self.set_status(sid, "queued")
        await self.scheduler.acquire(sid)
        self.set_status(sid, "running")

    # main entry
    async def run(self, sid: str, recovered: bool = False) -> None:
        try:
            s = self.db.get_session(sid)
            if recovered:
                self.bus.emit(sid, "resumed", {"status": s["status"]})
                if (s["run"].get("executing") or {}).get("name") in ("run_shell", "git_clone"):
                    await self.sandbox(s).restart()  # kill the orphaned command
            if s["status"] == "waiting_approval":
                pass  # _resolve_calls waits without holding the GPU
            else:
                await self._acquire(sid)
            await self._loop(sid)
        except asyncio.CancelledError:
            if sid in self.user_cancelled:
                self._record_cancel(sid)
                await self._end_run(sid)
            raise
        except SandboxUnavailable as e:
            self.bus.emit(sid, "error", {"message": str(e)})
            self.set_status(sid, "failed", stop_reason=f"sandbox_unavailable: {e}")
            await self._end_run(sid)
        except Exception as e:  # noqa: BLE001 - a crash must not leave the session looking active
            log.exception("session %s crashed", sid)
            self.bus.emit(sid, "error", {"message": f"{type(e).__name__}: {e}"})
            self.set_status(sid, "failed", stop_reason=f"internal_error: {type(e).__name__}: {e}")
            await self._end_run(sid)
        finally:
            self.scheduler.release(sid)
            self.user_cancelled.discard(sid)

    async def _loop(self, sid: str) -> None:
        while True:
            s = self.db.get_session(sid)
            if s["status"] not in ACTIVE:
                return
            pending = unresolved_calls(s["context"])
            if pending:
                if await self._resolve_calls(s, pending):
                    await self._end_run(sid)
                    return
                continue

            if s["inbox"]:
                context = s["context"] + [{"role": "user", "content": m} for m in s["inbox"]]
                self.db.update_session(sid, context=context, inbox=[])
                continue

            run = s["run"]
            if run["turns"] >= self.cfg.max_turns or run["completion_tokens"] >= self.cfg.max_completion_tokens:
                reason = "budget_turns" if run["turns"] >= self.cfg.max_turns else "budget_tokens"
                self.set_status(sid, "done", stop_reason=reason)
                await self._end_run(sid)
                return

            s = await self._maybe_compact(s)
            if await self._generate(s):
                await self._end_run(sid)
                return

    # model turn
    async def _generate(self, s: dict) -> bool:
        """One model call. Returns True when the run ended with a final answer."""
        sid = s["id"]
        model = self.cfg.models[s["model"]]
        ws = self.workspace(s)
        buffer = {"content": "", "reasoning": ""}
        last_flush = [time.monotonic()]

        def flush() -> None:
            for kind, text in buffer.items():
                if text:
                    self.bus.ephemeral(sid, "delta", {"kind": kind, "text": text})
                    buffer[kind] = ""
            last_flush[0] = time.monotonic()

        # A sleeping model takes about a minute to reload; tell the user instead of looking stuck.
        waking_since = None
        if await self.warmer.state(model) in (SLEEPING, WAKING):
            waking_since = time.monotonic()
            self.bus.emit(sid, "model_waking", {"model": model.name, "expected_seconds": EXPECTED_WAKE_SECONDS})

        async def on_delta(kind: str, text: str) -> None:
            nonlocal waking_since
            if waking_since is not None:
                self.bus.emit(sid, "model_ready", {"model": model.name,
                                                   "seconds": round(time.monotonic() - waking_since)})
                waking_since = None
            buffer[kind] += text
            if time.monotonic() - last_flush[0] > 0.25:
                flush()

        run = s["run"]
        tools = tool_schemas(ws.read_lines)
        completion = None
        for attempt in range(4):
            try:
                completion = await self.chat(model, s["context"], tools, on_delta)
                break
            except llm.LLMError as e:
                flush()
                if not e.retryable or attempt == 3:
                    raise
                if "HTTP 500" in str(e):
                    run["invalid_tool_calls"] += 1
                self.bus.emit(sid, "llm_retry", {"attempt": attempt + 1, "error": str(e)[:500]})
                await asyncio.sleep(2 * attempt)
        flush()

        run["turns"] += 1
        run["prompt_tokens"] += completion.prompt_tokens
        run["completion_tokens"] += completion.completion_tokens
        if completion.prompt_tokens > 2000:
            # prompt_tokens covers the whole prompt (cached or not), including the tool schemas.
            chars = sum(compaction.message_chars(m) for m in s["context"]) + len(json.dumps(tools))
            run["chars_per_token"] = min(6.0, max(1.5, chars / completion.prompt_tokens))
            run["context_tokens"] = completion.prompt_tokens + completion.completion_tokens

        msg: dict = {"role": "assistant", "content": completion.content}
        if completion.reasoning:
            msg["reasoning_content"] = completion.reasoning
        if completion.tool_calls:
            msg["tool_calls"] = completion.tool_calls
        event = {"content": completion.content, "reasoning": completion.reasoning,
                 "tool_calls": completion.tool_calls, "finish_reason": completion.finish_reason,
                 "prompt_tokens": completion.prompt_tokens, "completion_tokens": completion.completion_tokens,
                 "prompt_tps": round(completion.prompt_tps, 1), "gen_tps": round(completion.gen_tps, 1)}
        context = s["context"] + [msg]
        totals = self._add_totals(s["totals"], completion)

        final = not completion.tool_calls and completion.content.strip() and completion.finish_reason != "length"
        with self.db.tx():
            if final:
                run["idle"] = 0
                self.db.update_session(sid, context=context, run=run, totals=totals, status="done",
                                       stop_reason="final_message", answer=completion.content)
                self.bus.emit(sid, "assistant", event)
                self.bus.emit(sid, "status", {"status": "done", "stop_reason": "final_message",
                                              "answer": completion.content})
                return True
            if not completion.tool_calls:
                run["idle"] += 1
                if completion.finish_reason == "length":
                    nudge = "Your reply was cut off by the output limit. Continue, using the tools if needed."
                else:
                    nudge = "Continue the task using the tools. When you are done, give your final answer."
                context.append({"role": "user", "content": nudge})
            else:
                run["idle"] = 0
            self.db.update_session(sid, context=context, run=run, totals=totals)
            self.bus.emit(sid, "assistant", event)
        if run["idle"] >= 3:
            self.set_status(sid, "done", stop_reason="empty_replies")
            return True
        return False

    @staticmethod
    def _add_totals(totals: dict, c: llm.Completion) -> dict:
        totals = dict(totals)
        totals["turns"] = totals.get("turns", 0) + 1
        totals["prompt_tokens"] = totals.get("prompt_tokens", 0) + c.prompt_tokens
        totals["completion_tokens"] = totals.get("completion_tokens", 0) + c.completion_tokens
        return totals

    # tool calls
    async def _resolve_calls(self, s: dict, pending: list[dict]) -> bool:
        """Run the unresolved tool calls of the last assistant message. Returns True if `finish` was called."""
        sid = s["id"]
        executing = s["run"].get("executing") or {}
        model = self.cfg.models[s["model"]]
        # Parallel reads can overflow the window in one turn, so all results of a turn share one budget.
        budget = int(0.35 * model.context_tokens * s["run"].get("chars_per_token", 3.0))
        for i, call in enumerate(pending):
            fn = call.get("function") or {}
            name = fn.get("name", "")
            if executing.get("id") == call["id"]:
                self._record_result(sid, call, name, INTERRUPTED, ok=False)
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
            except ValueError as e:
                self._bump(sid, "invalid_tool_calls")
                self._record_result(sid, call, name, f"Error: tool arguments were not a valid JSON object ({e}).",
                                    ok=False)
                continue

            if name == "finish":
                answer = str(args.get("answer", ""))
                self._record_result(sid, call, name, "Task finished.", ok=True)
                for rest in pending[i + 1:]:
                    self._record_result(sid, rest, rest["function"].get("name", ""),
                                        "Not run: the task was already finished.", ok=False)
                self.set_status(sid, "done", stop_reason="finished", answer=answer)
                return True

            if name == "update_notes" and isinstance(args.get("notes"), str):
                with self.db.tx():
                    self.db.update_session(sid, run={**self.db.get_session(sid)["run"], "notes": args["notes"]})
                    self.bus.emit(sid, "notes", {"notes": args["notes"]})
                self._record_result(sid, call, name, f"Notes saved ({len(args['notes'])} characters).", ok=True)
                continue

            ws = self.workspace(s)
            schemas = {t["function"]["name"]: t for t in tool_schemas(ws.read_lines)}
            if name not in schemas:
                self._bump(sid, "invalid_tool_calls")
                self._record_result(sid, call, name, f"Error: unknown tool '{name}'. Available: "
                                                     f"{', '.join(schemas)}.", ok=False)
                continue
            try:
                args = validate_args(schemas[name], args)
            except ToolError as e:
                self._bump(sid, "invalid_tool_calls")
                self._record_result(sid, call, name, f"Error: bad arguments for {name}: {e}", ok=False)
                continue

            output = await self._authorize(s, call, name, args, ws)
            if output is None:
                output = await self._execute(sid, call, name, args, ws, max_chars=max(2000, budget))
            budget -= len(output)
            s = self.db.get_session(sid)
        return False

    async def _authorize(self, s: dict, call: dict, name: str, args: dict, ws: Workspace) -> str | None:
        """Apply the policy. Returns None to proceed, or the tool result to record instead of running it."""
        sid = s["id"]
        existing = self.db.approval_for_call(sid, call["id"])
        if existing is None:
            decision = self.policy(s).decide(name, args)
            self.bus.emit(sid, "tool_call", {"id": call["id"], "name": name, "args": args,
                                             "decision": decision.action, "reason": decision.reason})
            if decision.action == ALLOW:
                return None
            if decision.action != ASK:
                output = f"Error: blocked by policy ({decision.reason or 'not allowed'}). Don't retry this."
                self._record_result(sid, call, name, output, ok=False)
                return output
            existing = {"id": "a-" + uuid.uuid4().hex[:8], "session_id": sid, "tool_call_id": call["id"],
                        "tool": name, "args": args, "reason": decision.reason,
                        "detail": ws.preview_diff(name, args) if name in ("write_file", "edit_file") else ""}
            with self.db.tx():
                self.db.insert_approval(existing)
                self.bus.emit(sid, "approval_requested", {k: existing[k] for k in
                                                          ("id", "tool_call_id", "tool", "args", "reason", "detail")})
            existing["status"] = "pending"

        if existing["status"] == "pending":
            self.scheduler.release(sid)
            self.set_status(sid, "waiting_approval")
            existing = await self._wait_approval(existing["id"])
            await self._acquire(sid)
        if existing["status"] == "approved":
            return None
        note = f" Their note: {existing['note']}" if existing.get("note") else ""
        output = f"Error: the user denied this {name} call.{note} Don't retry it; choose another approach or explain."
        self._record_result(sid, call, name, output, ok=False)
        return output

    async def _wait_approval(self, aid: str) -> dict:
        event = self.approval_events.setdefault(aid, asyncio.Event())
        try:
            while True:
                approval = self.db.get_approval(aid)
                if approval["status"] != "pending":
                    return approval
                try:
                    await asyncio.wait_for(event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                event.clear()
        finally:
            self.approval_events.pop(aid, None)

    async def _execute(self, sid: str, call: dict, name: str, args: dict, ws: Workspace,
                       max_chars: int = 10**9) -> str:
        await self._acquire(sid)  # e.g. resumed after a restart with the approval already granted
        s = self.db.get_session(sid)
        run = s["run"]
        run["executing"] = {"id": call["id"], "name": name}
        self.db.update_session(sid, run=run)
        started = time.monotonic()
        ok = True
        try:
            output = await ws.call(name, args)
        except (ToolError, OSError, UnicodeError) as e:
            ok = False
            output = f"Error: {e}"
        if len(output) > max_chars:
            output = (output[:max_chars] + f"\n... [output cut at {max_chars} characters: this turn's tool results "
                      "would overflow the context window. Request less at once, e.g. a smaller line range.]")
        self._record_result(sid, call, name, output, ok=ok, seconds=time.monotonic() - started)
        return output

    def _bump(self, sid: str, counter: str) -> None:
        s = self.db.get_session(sid)
        run = s["run"]
        run[counter] = run.get(counter, 0) + 1
        self.db.update_session(sid, run=run)

    def _record_result(self, sid: str, call: dict, name: str, output: str, ok: bool, seconds: float = 0.0) -> None:
        with self.db.tx():
            s = self.db.get_session(sid)
            run = s["run"]
            run["tool_calls"] = run.get("tool_calls", 0) + 1
            if not ok:
                run["tool_errors"] = run.get("tool_errors", 0) + 1
            if (run.get("executing") or {}).get("id") == call["id"]:
                run["executing"] = None
            context = s["context"] + [{"role": "tool", "tool_call_id": call["id"], "content": output}]
            self.db.update_session(sid, context=context, run=run)
            self.bus.emit(sid, "tool_result", {"id": call["id"], "name": name, "ok": ok,
                                               "seconds": round(seconds, 2),
                                               "output": truncate_middle(output, 20000)})

    # compaction
    async def _maybe_compact(self, s: dict) -> dict:
        sid = s["id"]
        model = self.cfg.models[s["model"]]
        n = model.context_tokens
        cpt = s["run"].get("chars_per_token", 3.0)
        # Tool schemas are part of every prompt but not of the context list.
        overhead = int(len(json.dumps(tool_schemas(2000))) / cpt)
        before = compaction.estimate_tokens(s["context"], cpt) + overhead
        if before < self.cfg.elide_at * n:
            return s
        context, saved = compaction.elide(s["context"])
        after = compaction.estimate_tokens(context, cpt) + overhead
        data = {"tier": "elide", "tokens_before": before, "tokens_after": after}
        if after >= self.cfg.summarize_at * n:
            split = compaction.split_for_summary(context, keep_chars=int(self.cfg.keep_recent * n * cpt))
            if split:
                start, end = split
                self.bus.ephemeral(sid, "compacting", {"messages": end - start})
                request = compaction.summary_request(context, start, end, max_chars=int(0.45 * n * cpt))
                try:
                    summary = await self.chat(model, request, None, None, max_tokens=4096,
                                              extra={"chat_template_kwargs": {"enable_thinking": False}})
                except llm.LLMError as e:
                    self.bus.emit(sid, "error", {"message": f"compaction summary failed: {e}"})
                else:
                    if summary.content.strip():
                        context = compaction.apply_summary(context, start, end, summary.content,
                                                           notes=s["run"].get("notes", ""))
                        after = compaction.estimate_tokens(context, cpt) + overhead
                        data.update(tier="summary", tokens_after=after, summarized_messages=end - start,
                                    summary=summary.content)
        with self.db.tx():
            self.db.update_session(sid, context=context)
            self.bus.emit(sid, "compaction", data)
        return self.db.get_session(sid)

    # run end
    def _record_cancel(self, sid: str) -> None:
        s = self.db.get_session(sid)
        executing = (s["run"].get("executing") or {}).get("id")
        for call in unresolved_calls(s["context"]):
            text = ("Cancelled by the user while running." if call["id"] == executing
                    else "Not run: the user cancelled the task.")
            self._record_result(sid, call, call["function"].get("name", ""), text, ok=False)
        for approval in self.db.pending_approvals(sid):
            self.db.decide_approval(approval["id"], "cancelled")
        self.set_status(sid, "cancelled", stop_reason="cancelled")

    async def _end_run(self, sid: str) -> None:
        s = self.db.get_session(sid)
        self.bus.emit(sid, "run_finished", {"status": s["status"], "stop_reason": s["stop_reason"],
                                            "answer": s["answer"], "run": s["run"]})
        try:
            from .transcript import write_transcript
            write_transcript(self.db, self.cfg.transcripts_dir, sid)
        except OSError:
            log.exception("could not write transcript for %s", sid)
        await asyncio.shield(self.sandbox(s).stop())
