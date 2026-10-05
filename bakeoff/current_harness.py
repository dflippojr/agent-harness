"""Reference adapter wrapping the canary's production Manager/runner path."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from pathlib import Path

from .canary import CanaryRunner
from .tasks import hash_tree
from .results import error_metrics


def session_result(final: dict, events: list[dict]) -> dict:
    totals = final["totals"]
    compactions = [e["data"] for e in events if e["type"] == "compaction"]
    tools = [e["data"] for e in events if e["type"] == "tool_result"]
    failures = [e for e in events if e["type"] == "error"]
    return {"answer": final.get("answer", ""), "status": final["status"],
            "finished": final["status"] == "done", "stop_reason": final.get("stop_reason", ""),
            "turns": totals.get("turns", 0), "prompt_tokens": totals.get("prompt_tokens", 0),
            "completion_tokens": totals.get("completion_tokens", 0),
            "context_tokens": final["run"].get("context_tokens", 0),
            "tool_calls": len(tools), "tool_errors": sum(not t.get("ok", True) for t in tools),
            "invalid_tool_calls": final["run"].get("invalid_tool_calls", 0),
            "retries": sum(e["type"] == "llm_retry" for e in events),
            "compactions": sum(c.get("tier") != "mask" for c in compactions),
            "masking_events": sum(c.get("tier") == "mask" for c in compactions),
            "compaction_failures": sum("compaction" in str(e["data"]).lower() for e in failures),
            "compaction_events": compactions,
            **error_metrics([*failures, *[e for e in events if e["type"] == "llm_retry"]])}


async def run_current(task, run_dir: Path, model: str, base_url: str, chat=None) -> dict:
    from harness.config import CanaryConfig, Config, ModelConfig, Project, SandboxConfig
    from harness.llm import chat as real_chat
    from harness.manager import Manager

    # Construct a throwaway config; never read the production config or start its services.
    token = uuid.uuid4().hex[:10]
    cfg = Config(host="127.0.0.1", port=0, data_dir=run_dir / "manager", repos_dir=run_dir / "repos",
                 default_model=model, models={model: ModelConfig(name=model, base_url=base_url)},
                 sandbox=SandboxConfig(network=f"bakeoff-sandbox-{token}", egress_network=f"bakeoff-egress-{token}"),
                 projects={"scratch": Project(name="scratch")})
    manager = Manager(cfg, chat=chat or real_chat)
    workspace = None

    def prepare(t, ws):
        nonlocal workspace
        workspace = ws
        # reference.prepare has already materialized the common fixture/setup.
        shutil.copytree(run_dir / "workspace", ws, dirs_exist_ok=True)
        return hash_tree(ws)

    # The caller owns the shared checker; CanaryRunner still drives and bounds the session.
    runner = CanaryRunner(manager, CanaryConfig(total_cap_seconds=task.wall_limit + 5, start_wait_seconds=1),
                          suite={"hard": [task], "web": [], "repeats": 1}, poll_seconds=.05,
                          prepare_hard=prepare, grade_hard=lambda *args: (True, "graded by reference"))
    started = time.monotonic()
    try:
        report = await runner.run("bakeoff")
        if workspace is None:
            return {"answer": "", "finished": False, "turns": 0, "tool_calls": 0, "tool_errors": 0,
                    "stop_reason": report.status, "adapter_error": "Manager did not create a session",
                    "wall_seconds": time.monotonic() - started}
        final = manager.db.get_session(workspace.name)
        events = manager.db.events(final["id"])
        result = session_result(final, events)
        if report.outcomes[0]["status"] == "wall_limit":
            result.update(status="timeout", stop_reason="wall_limit", finished=False)
        shutil.copytree(workspace, run_dir / "workspace", dirs_exist_ok=True)
        (run_dir / "agent-harness.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
        return {**result, "wall_seconds": round(time.monotonic() - started, 1)}
    finally:
        await manager.stop()
        manager.db.close()
