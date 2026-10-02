"""Compare repo-map off/on bake-off runs and apply the pre-registered go/no-go rule (#264).

    python -m bakeoff.repomap_report --large runs/<off> runs/<on> --hard runs/<off> runs/<on>

Each argument pair is (map-off run dir, map-on run dir), each holding the summaries.json bakeoff.run wrote.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

DROP_THRESHOLD = 0.15


def load(run_dir: Path, model: str | None = None) -> list[dict]:
    summaries = json.loads((run_dir / "summaries.json").read_text(encoding="utf-8"))
    return [t for s in summaries if model in (None, s["model"]) for t in s["tasks"]]


def arm_stats(tasks: list[dict]) -> dict:
    return {
        "runs": len(tasks),
        "pass_rate": sum(t["passed"] for t in tasks) / len(tasks),
        "turns": statistics.mean(t["turns"] for t in tasks),
        "prompt_tokens": statistics.mean(t["prompt_tokens"] for t in tasks),
        "wall_seconds": statistics.mean(t["wall_seconds"] for t in tasks),
    }


def reduction(off: float, on: float) -> float:
    return (off - on) / off if off else 0.0


def decide(large: tuple[dict, dict], hard: tuple[dict, dict]) -> tuple[bool, str]:
    """Go if, on the large-repo group, mean turns or total prompt tokens drop >=15% with pass rate no lower than off,
    and the hard-suite group shows no pass-rate regression."""
    (l_off, l_on), (h_off, h_on) = large, hard
    turns, tokens = reduction(l_off["turns"], l_on["turns"]), reduction(l_off["prompt_tokens"], l_on["prompt_tokens"])
    large_ok = (turns >= DROP_THRESHOLD or tokens >= DROP_THRESHOLD) and l_on["pass_rate"] >= l_off["pass_rate"]
    hard_ok = h_on["pass_rate"] >= h_off["pass_rate"]
    note = (f"large: turns {turns:+.1%}, prompt tokens {tokens:+.1%} (drop needed >= {DROP_THRESHOLD:.0%}), "
            f"pass {l_off['pass_rate']:.0%} -> {l_on['pass_rate']:.0%}; hard: pass {h_off['pass_rate']:.0%} -> "
            f"{h_on['pass_rate']:.0%}")
    return large_ok and hard_ok, note


def table_rows(label: str, off: dict, on: dict) -> list[str]:
    def row(arm: str, s: dict) -> str:
        return (f"| {label} | {arm} | {s['runs']} | {s['pass_rate']:.0%} | {s['turns']:.1f} "
                f"| {s['prompt_tokens']:.0f} | {s['wall_seconds']:.0f} |")
    return [row("off", off), row("on", on)]


def main() -> None:
    parser = argparse.ArgumentParser()
    for group in ("large", "hard"):
        parser.add_argument(f"--{group}", nargs=2, type=Path, metavar=("OFF_DIR", "ON_DIR"), required=True)
    parser.add_argument("--model", help="restrict to one model name")
    args = parser.parse_args()
    stats = {g: tuple(arm_stats(load(d, args.model)) for d in getattr(args, g)) for g in ("large", "hard")}
    print("| Group | Map | Runs | Pass rate | Mean turns | Mean prompt tokens | Mean wall s |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for group, label in (("large", "large-repo"), ("hard", "hard suite")):
        print("\n".join(table_rows(label, *stats[group])))
    go, note = decide(stats["large"], stats["hard"])
    print(f"\n{'GO' if go else 'NO-GO'}: {note}")


if __name__ == "__main__":
    main()
