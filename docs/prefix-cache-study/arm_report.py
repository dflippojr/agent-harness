"""#898: compare canary arms from `--capture-prompts` files and the runner's outcome files.

    python docs/prefix-cache-study/arm_report.py runs/ah898 [--json OUT]

The directory holds one `<arm>-<task>-r<repeat>.prompts.jsonl` per session and the `.stdout` of the invocation that
made it (its last line names the `runs/canary-*.json` outcome file). Per arm: pass rate, requests, prompt tokens, reuse,
prefill and decode seconds, and, by context length (the request's prompt tokens), prefill and decode rates and seconds
per request. Summary calls count in the totals and the bins; the per-session break count is agent calls whose prompt
did not reuse most of the previous one.

Only graded attempts count, by the canary's own rule (`harness.canary.valid`: status `done`, `failed` or
`wall_limit`). A `suspended` attempt, or one with no outcome file (interrupted), is listed with `graded: false` and
left out of the arm totals and the rate bins.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bakeoff.prefix_diff import completed, is_summary_call, read_jsonl  # noqa: E402 (after the path insert)
from harness.canary import valid  # noqa: E402 (after the path insert)

BINS = ((0, 8192), (8192, 16384), (16384, 24576), (24576, 32768), (32768, 49152), (49152, 65536))
NAME = re.compile(r"^(?P<arm>[^-]+)-(?P<task>.+)-r(?P<rep>\d+)\.prompts\.jsonl$")
RESULT = re.compile(r"results in (\S+\.json)")


def outcome(stdout: Path, root: Path) -> dict | None:
    match = RESULT.search(stdout.read_text(encoding="utf-8")) if stdout.exists() else None
    if not match:
        return None
    path = Path(match.group(1))
    path = path if path.is_absolute() else root / path
    outcomes = json.loads(path.read_text(encoding="utf-8"))["outcomes"]
    return outcomes[0] if outcomes else None


def session_stats(rows: list[dict]) -> dict:
    rows = completed(rows)
    breaks = 0
    prev = None
    for r in rows:
        if is_summary_call(r):
            continue
        if prev is not None and (r.get("cache_tokens") or 0) < 0.9 * (prev.get("prompt_tokens") or 0):
            breaks += 1
        prev = r
    return {"requests": len(rows), "summary_calls": sum(is_summary_call(r) for r in rows),
            "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in rows),
            "cached": sum(r.get("cache_tokens") or 0 for r in rows),
            "prefill_s": sum(r.get("prompt_ms") or 0 for r in rows) / 1000,
            "decode_s": sum(r.get("decode_ms") or 0 for r in rows) / 1000,
            "completion_tokens": sum(r.get("completion_tokens") or 0 for r in rows),
            "peak_prompt": max((r.get("prompt_tokens") or 0 for r in rows), default=0), "breaks": breaks}


def bin_of(tokens: int) -> str:
    for lo, hi in BINS:
        if tokens < hi:
            return f"{lo // 1024}-{hi // 1024}K"
    return f"{BINS[-1][1] // 1024}K+"


def report(root: Path) -> dict:
    sessions = []
    bins: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for capture in sorted(root.glob("*.prompts.jsonl")):
        m = NAME.match(capture.name)
        if not m:
            continue
        rows = read_jsonl(capture)
        o = outcome(capture.with_name(capture.name.replace(".prompts.jsonl", ".stdout")), root.parent.parent)
        graded = bool(o and valid([o]))
        sessions.append({**m.groupdict(), **session_stats(rows), "ok": o.get("ok") if o else None,
                         "status": o.get("status") if o else None, "seconds": o.get("seconds") if o else None,
                         "graded": graded})
        if not graded:
            continue
        for r in completed(rows):
            b = bins[m["arm"]][bin_of(r.get("prompt_tokens") or 0)]
            read = (r.get("prompt_tokens") or 0) - (r.get("cache_tokens") or 0)
            b["requests"] += 1
            b["read"] += read
            b["prefill_ms"] += r.get("prompt_ms") or 0
            b["completion_tokens"] += r.get("completion_tokens") or 0
            b["decode_ms"] += r.get("decode_ms") or 0
    arms: dict[str, dict] = {}
    for s in sessions:
        if not s["graded"]:
            continue
        a = arms.setdefault(s["arm"], defaultdict(float))
        a["sessions"] += 1
        a["passed"] += bool(s["ok"])
        for k in ("requests", "summary_calls", "prompt_tokens", "cached", "prefill_s", "decode_s", "completion_tokens",
                  "breaks"):
            a[k] += s[k]
        a["wall_s"] += s["seconds"] or 0
    for a in arms.values():
        a["reuse"] = round(a["cached"] / a["prompt_tokens"], 3) if a["prompt_tokens"] else None
    rates = {arm: {name: {"requests": int(b["requests"]),
                          "prefill_tok_s": round(b["read"] / b["prefill_ms"] * 1000) if b["prefill_ms"] else None,
                          "decode_tok_s": round(b["completion_tokens"] / b["decode_ms"] * 1000, 1)
                          if b["decode_ms"] else None,
                          "s_per_request": round((b["prefill_ms"] + b["decode_ms"]) / b["requests"] / 1000, 1)}
                   for name, b in sorted(per.items(), key=lambda kv: int(kv[0].split("-")[0].rstrip("K+")))}
             for arm, per in bins.items()}
    return {"arms": {k: dict(v) for k, v in arms.items()}, "rates_by_context": rates, "sessions": sessions}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    result = report(a.root)
    if a.json:
        a.json.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps({"arms": result["arms"], "rates_by_context": result["rates_by_context"]}, indent=1))
    for s in result["sessions"]:
        print(f"{s['arm']:4} {s['task']:20} r{s['rep']} ok={s['ok']} graded={s['graded']} req={s['requests']} prompt={s['prompt_tokens']} "
              f"reuse={s['cached'] / s['prompt_tokens'] if s['prompt_tokens'] else 0:.0%} prefill={s['prefill_s']:.1f}s "
              f"decode={s['decode_s']:.1f}s breaks={s['breaks']} peak={s['peak_prompt']} wall={s['seconds']}")


if __name__ == "__main__":
    main()
