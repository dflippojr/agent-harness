"""Web research suite on a recorded web (Phase 7e).

Runs research questions through the real harness (Manager, runner, web_search / web_fetch) with the always-on
model, but with `web.fixture_dir` set, so every run sees the same search results and pages and nothing touches the
network. Answers are graded with regular expressions.

    python -m bakeoff.web_suite record                      # capture the fixture (needs SearXNG and internet)
    python -m bakeoff.web_suite run --repeats 2             # replay; stop other GPU work first
    python -m bakeoff.web_suite run --tasks pdf_transformer

The fixture holds third-party pages, so it stays outside the repository (default D:/Agents/harness/web-fixture).
Results go to runs/web-<timestamp>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = Path("D:/Agents/harness/web-fixture")


@dataclass
class WebTask:
    id: str
    prompt: str
    must_match: list[str]                    # every pattern must appear in the answer (case-insensitive)
    must_not_match: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)  # recorded, with their top results
    urls: list[str] = field(default_factory=list)     # recorded as well

    def check(self, answer: str) -> tuple[bool, str]:
        missing = [p for p in self.must_match if not re.search(p, answer, re.I)]
        wrong = [p for p in self.must_not_match if re.search(p, answer, re.I)]
        ok = not missing and not wrong
        return ok, "ok" if ok else f"missing {missing} wrong {wrong}"


TASKS = [
    WebTask(
        id="pdf_transformer",
        prompt=("In the original Transformer paper, 'Attention Is All You Need' (arXiv 1706.03762), how many attention "
                "heads does the base model use, and what are d_model and d_ff? Take the numbers from the paper itself "
                "(the PDF), not from summaries, and say which page they're on."),
        must_match=[r"\b8\b", r"\b512\b", r"\b2048\b"],
        queries=["Attention Is All You Need arxiv 1706.03762 pdf", "attention is all you need paper"],
        urls=["https://arxiv.org/pdf/1706.03762"],
    ),
    WebTask(
        id="llama_sleep_endpoints",
        prompt=("When llama.cpp's llama-server runs with --sleep-idle-seconds, which HTTP endpoints can be called "
                "without waking a sleeping model? Check the server's documentation and cite it."),
        must_match=[r"/health", r"/props", r"/metrics"],
        must_not_match=[r"/v1/chat/completions[^.\n]*(doesn't|does not|won't|without) wak"],
        queries=["llama.cpp server --sleep-idle-seconds", "llama-server sleep idle seconds endpoints",
                 "llama.cpp server README"],
        urls=["https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md",
              "https://raw.githubusercontent.com/ggml-org/llama.cpp/master/tools/server/README.md"],
    ),
    WebTask(
        id="searxng_license",
        prompt="What license is the SearXNG metasearch engine released under? Give the license's full name and cite a source.",
        must_match=[r"AGPL|Affero"],
        queries=["SearXNG license", "searxng github", "SearXNG metasearch engine"],
        urls=["https://github.com/searxng/searxng"],
    ),
]


def _norm(text: str) -> str:
    # Letters and digits only: PDF text layers break spacing and subscripts ("df f = 2048" for d_ff = 2048), and
    # models add Markdown to quotes.
    return re.sub(r"[^a-z0-9]", "", text.lower())


def ungrounded_quotes(answer: str, tool_outputs: list[str], min_chars: int = 25) -> list[str]:
    """Quoted passages in the answer that don't appear in anything the agent's tools returned (a made-up citation).
    A quote with an ellipsis counts as grounded when each part appears."""
    source = _norm("\n".join(tool_outputs))
    quotes = re.findall(r'["“]([^"”\n]{%d,400})["”]' % min_chars, answer)
    missing = []
    for q in quotes:
        parts = [_norm(x) for x in re.split(r"\.\.\.|…", q) if len(_norm(x)) >= 12]
        if parts and not all(part in source for part in parts):
            missing.append(q)
    return missing


async def record(fixture: Path, fetch_top: int) -> None:
    from harness.web_fixture import record as record_fixture
    queries = [q for t in TASKS for q in t.queries]
    urls = [u for t in TASKS for u in t.urls]
    await record_fixture(fixture, queries, urls, fetch_top, "http://127.0.0.1:8888")


async def run_one(task: WebTask, fixture: Path, repeat: int) -> dict:
    from harness import config as config_mod
    from harness.config import Project, WebConfig
    from harness.manager import Manager

    base = config_mod.load()
    tmp = Path(tempfile.mkdtemp(prefix="web-suite-"))
    cfg = config_mod.Config(
        host="127.0.0.1", port=0, data_dir=tmp, repos_dir=tmp / "repos", default_model=base.default_model,
        models=base.models, sandbox=base.sandbox, projects={"scratch": Project(name="scratch")},
        web=WebConfig(enabled=True, page_chars=base.web.page_chars, fixture_dir=str(fixture)),
    )
    m = Manager(cfg)
    await m.start(maintenance=False)
    started = time.monotonic()
    s = m.create(task.prompt, title=f"web suite {task.id} #{repeat}")
    try:
        while m.db.get_session(s["id"])["status"] not in ("done", "failed", "cancelled"):
            await asyncio.sleep(1)
            if time.monotonic() - started > 1500:
                await m.cancel(s["id"])
                break
        final = m.db.get_session(s["id"])
        results = [e["data"] for e in m.db.events(s["id"]) if e["type"] == "tool_result"]
        calls = [r["name"] for r in results]
        requests = [c["function"] for e in m.db.events(s["id"]) if e["type"] == "assistant"
                    for c in e["data"].get("tool_calls") or []]
        ok, note = task.check(final["answer"])
        made_up = ungrounded_quotes(final["answer"], [r["output"] for r in results])
        if made_up:
            ok, note = False, f"{note}; quotes not in any fetched text: {made_up}"
        return {"task": task.id, "repeat": repeat, "ok": ok and final["status"] == "done", "note": note,
                "ungrounded_quotes": made_up,
                "status": final["status"], "stop_reason": final["stop_reason"],
                "seconds": round(time.monotonic() - started, 1), "turns": final["totals"].get("turns", 0),
                "prompt_tokens": final["totals"].get("prompt_tokens", 0),
                "completion_tokens": final["totals"].get("completion_tokens", 0),
                "context_tokens": final["run"].get("context_tokens", 0), "tool_calls": calls,
                "fixture_misses": list(m.runner.web.fixture.misses), "answer": final["answer"],
                # for auditing citations: what each call asked for and the start of what it got back
                "tool_log": [{"call": q, "ok": r["ok"], "output": r["output"][:2000]} for q, r in zip(requests, results)]}
    finally:
        await m.stop()
        m.db.close()
        shutil.rmtree(tmp, ignore_errors=True)


async def run(fixture: Path, task_ids: list[str], repeats: int) -> Path:
    tasks = [t for t in TASKS if not task_ids or t.id in task_ids]
    results = []
    for task in tasks:
        for r in range(repeats):
            res = await run_one(task, fixture, r)
            results.append(res)
            print(f"{'PASS' if res['ok'] else 'FAIL'} {task.id:24} #{r} {res['seconds']:6.1f}s {res['turns']:2} turns "
                  f"{len(res['tool_calls']):2} calls  misses={len(res['fixture_misses'])}  {res['note']}")
    out = ROOT / "runs" / f"web-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"fixture": str(fixture), "results": results}, indent=1), encoding="utf-8")
    passed = sum(r["ok"] for r in results)
    print(f"\n{passed}/{len(results)} passed · results in {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bakeoff.web_suite")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    rec.add_argument("--fetch-top", type=int, default=3)
    go = sub.add_parser("run")
    go.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    go.add_argument("--tasks", default="")
    go.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args(argv)
    if args.command == "record":
        asyncio.run(record(args.fixture, args.fetch_top))
    else:
        asyncio.run(run(args.fixture, [t for t in args.tasks.split(",") if t], args.repeats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
