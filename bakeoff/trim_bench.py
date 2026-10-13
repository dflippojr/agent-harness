"""Replay fixed synthetic transcripts through compaction with the trim tier on and off (#637, docs/compaction.md).

Each transcript is a scripted list of tool calls with canned outputs. Before every agent model call the replay does
what Runner._maybe_compact does on a local agent session without saved state: mask used results, and at elide_at
trim (when on), elide, and summarize at summarize_at with a fixed-length stand-in summary. Nothing calls a model.

Arms: baseline (trim off), trim on, config-only (trim off, a 25-point elide/summarize gap as in arXiv 2609.20804),
and both. Reported per arm: characters sent to the model (agent prompts plus summary requests), summary calls,
compaction passes, the share of passes that ended under summarize_at without a summary call (elide-only), and
"needed again": head-and-tail trimmed command outputs whose identical call recurs within 10 turns (the proxy from
docs/jev-compaction-study.md; the scripts are fixed, so this counts recurrences, it does not cause them).

    python -m bakeoff.trim_bench            # the tables in docs/compaction.md
    python -m bakeoff.trim_bench --no-mask  # sessions where read_artifact is unavailable (no masking)
    python -m bakeoff.trim_bench --context 32768
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field

from harness import compaction, tools
from harness.compaction import TrimRules

CONTEXT_TOKENS = 65536      # config/harness.yaml's local models
CHARS_PER_TOKEN = 3.0       # the runner's starting estimate
KEEP_RECENT = 0.20
MASK_MIN_CHARS = 2000
SUMMARY_CHARS = 2400        # a typical handoff note
NEEDED_AGAIN_TURNS = 10
OVERHEAD_CHARS = len(json.dumps(tools.tool_schemas(400)))  # tool schemas: in every prompt, not in the context list
SUMMARY = "## Done so far\n" + "Synthetic handoff note. " * (SUMMARY_CHARS // 24)


@dataclass(frozen=True)
class Step:
    name: str
    args: dict
    output: str
    ok: bool = True
    text: str = ""


@dataclass(frozen=True)
class Arm:
    name: str
    trim: bool
    elide_at: float = 0.55
    summarize_at: float = 0.65


ARMS = (Arm("baseline", False), Arm("trim", True), Arm("config-only (0.55/0.80)", False, summarize_at=0.80),
        Arm("trim + 0.55/0.80", True, summarize_at=0.80))


@dataclass
class Result:
    chars_sent: int = 0
    summary_chars: int = 0
    summary_calls: int = 0
    passes: int = 0
    elide_only: int = 0
    trim_chars: int = 0
    head_tail: list = field(default_factory=list)   # (turn, call signature) of each head-and-tail trimmed output
    needed_again: int = 0


# synthetic transcripts

def _ansi(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m"


def _pytest_output(rng: random.Random, failing: int, lines: int = 260) -> str:
    out = [_ansi("============================= test session starts ==============================", "1"),
           "platform linux -- Python 3.12.3, pytest-8.3.2", "collected 412 items", ""]
    for i in range(lines):
        mark = _ansi("F", "31") if i % 37 == 0 and failing else _ansi(".", "32")
        out.append(f"tests/test_module_{i % 23:02d}.py::test_case_{i:03d} {mark} [{100 * i // lines:3d}%]")
    for i in range(failing):
        out += [_ansi(f"___ test_case_{i * 37:03d} ___", "31;1"), "    def test_case():",
                f">       assert parse(row) == {rng.randint(10, 99)}", f"E       assert {rng.randint(100, 999)} == 42",
                "", f"src/parser.py:{rng.randint(10, 300)}: AssertionError"]
    out.append(_ansi(f"=== {failing} failed, {412 - failing} passed in 12.31s ===", "31" if failing else "32"))
    return "\n".join(out)


def _install_output(packages: int = 40) -> str:
    out = []
    for i in range(packages):
        bar = "".join(f"\r  {p:3d}% |{'#' * (p // 5):<20}| {p * 12} kB" for p in range(0, 101, 5))
        out.append(f"Collecting package-{i:02d}==1.{i}.0{bar}")
    out.append("Successfully installed " + " ".join(f"package-{i:02d}-1.{i}.0" for i in range(packages)))
    return "\n".join(out)


def _source(path: str, lines: int, seed: int) -> str:
    rng = random.Random(seed)
    body = [f"{i + 1:6d}\t{'    ' * rng.randint(0, 3)}value_{rng.randint(0, 999)} = compute({path!r}, {i})"
            for i in range(lines)]
    return "\n".join(body)


def _git_status(dirty: int) -> str:
    return "On branch work\nChanges not staged for commit:\n" + "".join(
        f"\tmodified:   src/module_{i:02d}.py\n" for i in range(dirty)) + "\nno changes added to commit"


def fix_loop() -> list[Step]:
    """Run the failing suite, read and edit the culprit, re-run, check git status; the suite passes at the end."""
    rng = random.Random(1)
    steps: list[Step] = [Step("run_shell", {"command": "pip install -e ."}, _install_output())]
    for cycle in range(40):
        failing = max(0, 6 - cycle // 6)
        path = f"src/module_{cycle % 5:02d}.py"
        steps += [
            Step("run_shell", {"command": "pytest -q"}, _pytest_output(rng, failing), ok=failing == 0),
            Step("read_file", {"path": path}, _source(path, 60 + cycle * 3, cycle)),
            Step("edit_file", {"path": path, "old_text": "value_1", "new_text": "value_2"}, f"Edited {path}"),
            Step("read_file", {"path": path}, _source(path, 60 + cycle * 3, cycle + 100)),
            Step("run_shell", {"command": "git status"}, _git_status(3), text="Checking what changed."),
        ]
    steps.append(Step("run_shell", {"command": "pytest -q"}, _pytest_output(rng, 0)))
    return steps


def exploration() -> list[Step]:
    """Read around a codebase, coming back to the same files and listings, under the masking threshold."""
    steps: list[Step] = []
    files = [f"harness/part_{i}.py" for i in range(8)]
    listing = "\n".join(f"harness/part_{i}.py" for i in range(40))
    for rnd in range(30):
        steps.append(Step("list_files", {"path": "harness"}, listing))
        for i, path in enumerate(files):
            if (i + rnd) % 3 == 0:
                steps.append(Step("read_file", {"path": path}, _source(path, 45, i)))
        steps.append(Step("search", {"pattern": "compute\\("},
                          "\n".join(f"{p}:{k}: value = compute()" for p in files for k in range(4))))
        steps.append(Step("run_shell", {"command": "python -m mypy harness"},
                          _ansi("Success: no issues found in 40 source files", "32") * 3))
    return steps


def build_failures() -> list[Step]:
    """A failing build with long, noisy logs re-run after each fix."""
    rng = random.Random(3)
    steps: list[Step] = []
    for attempt in range(50):
        log = "\n".join([_ansi(f"[{t:4d}/1200] Compiling unit_{t}.c", "36") for t in range(0, 1200, 4)] +
                        [_ansi(f"error: undefined reference to `sym_{rng.randint(0, 9)}'", "31;1")] * max(1, 8 - attempt // 6))
        steps += [Step("run_shell", {"command": "make -j4"}, log, ok=attempt == 49),
                  Step("read_file", {"path": "src/link.c", "start_line": 1, "end_line": 80},
                       _source("src/link.c", 80, attempt)),
                  Step("edit_file", {"path": "src/link.c", "old_text": "a", "new_text": "b"}, "Edited src/link.c")]
    return steps


TRANSCRIPTS = {"test-fix loop": fix_loop, "exploration": exploration, "build failures": build_failures}


# replay

def _sig(name: str, args: dict) -> str:
    return name + json.dumps(args, sort_keys=True)


def _tokens(context: list[dict]) -> int:
    return int((sum(compaction.message_chars(m) for m in context) + OVERHEAD_CHARS) / CHARS_PER_TOKEN)


def _compact(context: list[dict], arm: Arm, outcomes: dict, mask: bool, turn: int, sigs: dict,
             result: Result, n: int) -> list[dict]:
    if mask:
        context, _, _ = compaction.mask_used_results(context, outcomes, MASK_MIN_CHARS)
    if _tokens(context) < arm.elide_at * n:
        return context
    result.passes += 1
    if arm.trim:
        trimmed, saved = compaction.trim(context, TrimRules(), int(KEEP_RECENT * n * CHARS_PER_TOKEN), outcomes)
        result.trim_chars += saved
        for old, new in zip(context, trimmed):
            if (old.get("content") != new.get("content") and old["role"] == "tool"
                    and compaction.LINES_OMITTED.split("{")[0] in (new.get("content") or "")):
                result.head_tail.append((turn, sigs[old["tool_call_id"]]))
        context = trimmed
    context, _ = compaction.elide(context)
    if _tokens(context) < arm.summarize_at * n:
        result.elide_only += 1
        return context
    split = compaction.split_for_summary(context, keep_chars=int(KEEP_RECENT * n * CHARS_PER_TOKEN))
    if not split:
        return context
    request = compaction.summary_request(context, *split, max_chars=int(0.45 * n * CHARS_PER_TOKEN))
    result.summary_calls += 1
    result.summary_chars += sum(compaction.message_chars(m) for m in request)
    return compaction.apply_summary(context, *split, SUMMARY)


def replay(steps: list[Step], arm: Arm, mask: bool = True, n: int = CONTEXT_TOKENS) -> Result:
    result = Result()
    context = [{"role": "system", "content": "You are a software agent. " * 40},
               {"role": "user", "content": "Make the test suite pass and report what you changed."}]
    outcomes: dict[str, dict] = {}
    sigs: dict[str, str] = {}
    for turn, step in enumerate(steps):
        context = _compact(context, arm, outcomes, mask, turn, sigs, result, n)
        result.chars_sent += sum(compaction.message_chars(m) for m in context) + OVERHEAD_CHARS
        call_id = f"call-{turn}"
        call = {"id": call_id, "type": "function", "function": {"name": step.name, "arguments": json.dumps(step.args)}}
        context = context + [{"role": "assistant", "content": step.text, "tool_calls": [call]},
                             {"role": "tool", "tool_call_id": call_id, "content": step.output}]
        outcomes[call_id] = {"ok": step.ok, "name": step.name}
        sigs[call_id] = _sig(step.name, step.args)
    later = [(turn, _sig(s.name, s.args)) for turn, s in enumerate(steps)]
    result.needed_again = sum(1 for at, sig in result.head_tail
                              if any(sig == other and at <= turn < at + NEEDED_AGAIN_TURNS for turn, other in later))
    result.chars_sent += result.summary_chars
    return result


def table(mask: bool = True, n: int = CONTEXT_TOKENS) -> str:
    rows = ["| Transcript | Arm | Chars sent | Summary calls | Passes | Elide-only | Trim saved | Head-tail / needed again |",
            "|---|---|---:|---:|---:|---:|---:|---:|"]
    for name, make in TRANSCRIPTS.items():
        steps = make()
        for arm in ARMS:
            r = replay(steps, arm, mask, n)
            share = f"{r.elide_only}/{r.passes} ({100 * r.elide_only // r.passes}%)" if r.passes else "0/0"
            rows.append(f"| {name} ({len(steps)} turns) | {arm.name} | {r.chars_sent:,} | {r.summary_calls} | "
                        f"{r.passes} | {share} | {r.trim_chars:,} | {len(r.head_tail)} / {r.needed_again} |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-mask", action="store_true", help="replay without observation masking")
    parser.add_argument("--context", type=int, default=CONTEXT_TOKENS, help="context window in tokens")
    args = parser.parse_args(argv)
    print(table(mask=not args.no_mask, n=args.context))


if __name__ == "__main__":
    main()
