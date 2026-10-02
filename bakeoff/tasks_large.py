"""Large-repo task group for the repo-map study (#264, docs/repo-map-study.md).

The hard suite's sandboxes are tiny synthetic repos where a map is near-trivial. These tasks run against a pinned
checkout of this repository, so there are ~100 Python files for the map to rank. Each has a hidden check in the same
style as `tasks_hard.py`. The checkout leaves out `tests/` (the existing tests need packages the sandbox lacks) and
non-text files; the prompts say how to verify.
"""

from __future__ import annotations

import functools
import subprocess
from pathlib import Path

from .tasks import PYTEST_INI, Context, Task, has_all, pytest_ok, write

REPO_ROOT = Path(__file__).resolve().parent.parent
PINNED_SHA = "0dd8560d5f22ec361eb9c3b8cc2956f387210ffc"  # origin/main when #264 was implemented
INCLUDE_DIRS = ("harness", "bakeoff", "docs", "ops", "scripts", "config", "install", "sdk", "macrunner")
INCLUDE_FILES = ("README.md", "requirements.txt")
MAX_FILE_BYTES = 300_000
LARGE_LIMITS = {"max_turns": 50, "wall_limit": 1800}
VERIFY_NOTE = (" The sandbox has only Python and pytest (no third-party packages) and the repo's own tests are not "
               "included, so check your work by importing the module with `python -c`.")


def _git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, check=True).stdout


@functools.lru_cache(maxsize=1)
def checkout_files() -> tuple[tuple[str, str], ...]:
    """(path, text) for the pinned commit's text files, via git plumbing only (nothing is checked out or run)."""
    names = _git("ls-tree", "-r", "-z", "--name-only", PINNED_SHA).decode("utf-8").split("\0")
    out = []
    for name in sorted(n for n in names if n):
        top = name.split("/", 1)[0]
        if name not in INCLUDE_FILES and top not in INCLUDE_DIRS:
            continue
        blob = _git("show", f"{PINNED_SHA}:{name}")
        if len(blob) > MAX_FILE_BYTES:
            continue
        try:
            out.append((name, blob.decode("utf-8")))
        except UnicodeDecodeError:
            continue
    return tuple(out)


def large_files() -> dict[str, str]:
    return {"pytest.ini": PYTEST_INI, **dict(checkout_files())}


def _replace(ctx: Context, rel: str, old: str, new: str, count: int = 1) -> None:
    text = (ctx.ws / rel).read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {rel}"
    write(ctx, rel, text.replace(old, new, count))


def _hidden(ctx: Context, name: str, body: str) -> tuple[bool, str]:
    write(ctx, f"hidden_checks/{name}.py", body)
    return pytest_ok(ctx, f"hidden_checks/{name}.py")


# --- 1. elide_ends -----------------------------------------------------------------------------------------

ELIDE_PROMPT = (
    "Context compaction's first tier shortens old, oversized tool output by keeping 600 characters from each end. "
    "Make how much is kept at the start and at the end configurable through two new keyword parameters on that "
    "function, `head_chars` and `tail_chars`, both defaulting to 600 so existing behavior is unchanged. Leave the "
    "callers alone." + VERIFY_NOTE)

ELIDE_HIDDEN = '''from harness import compaction

TEXT = "".join(chr(97 + i % 26) for i in range(3000))


def msgs():
    out = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    for i in range(5):
        out.append({"role": "assistant", "content": "", "tool_calls": [{"id": str(i)}]})
        out.append({"role": "tool", "tool_call_id": str(i), "content": TEXT if i == 0 else "ok"})
    out.append({"role": "assistant", "content": "done"})
    return out


def test_custom_ends():
    out, saved = compaction.elide(msgs(), head_chars=100, tail_chars=40)
    text = out[3]["content"]
    assert text.startswith(TEXT[:100] + "\\n")
    assert text.endswith("\\n" + TEXT[-40:])
    assert len(text) < 100 + 40 + 200  # the middle is gone
    assert "3000" in text
    assert saved > 0


def test_defaults_unchanged():
    default, saved_default = compaction.elide(msgs())
    explicit, saved_explicit = compaction.elide(msgs(), head_chars=600, tail_chars=600)
    assert default == explicit and saved_default == saved_explicit
    assert default[3]["content"].startswith(TEXT[:600] + "\\n")
    assert default[3]["content"].endswith("\\n" + TEXT[-600:])


def test_positional_args_still_work():
    out, _ = compaction.elide(msgs(), 6, 1500)
    assert out[3]["content"].startswith(TEXT[:600])
'''


def elide_check(ctx: Context) -> tuple[bool, str]:
    return _hidden(ctx, "test_elide", ELIDE_HIDDEN)


def elide_solve(ctx: Context) -> str:
    rel = "harness/compaction.py"
    _replace(ctx, rel, "def elide(messages: list[dict], keep_last: int = 6, max_chars: int = 1500)",
             "def elide(messages: list[dict], keep_last: int = 6, max_chars: int = 1500,\n"
             "          head_chars: int = 600, tail_chars: int = 600)")
    _replace(ctx, rel, "{text[:600]}", "{text[:head_chars]}")
    _replace(ctx, rel, "{text[-600:]}", "{text[-tail_chars:]}")
    return "added head_chars and tail_chars to compaction.elide"


# --- 2. rate_limit_not_dead_end ------------------------------------------------------------------------------

RATE_PROMPT = (
    "The efficiency metrics count a repeated identical failing tool call as a dead-end retry. A tool result whose "
    "output starts with `Rate limited:` is the provider throttling us, not the agent being stuck, so it must not "
    "count as the start of a dead end, the same way policy blocks and user denials don't. Make that change." + VERIFY_NOTE)

RATE_HIDDEN = '''from harness import efficiency


def test_rate_limited_does_not_seed():
    assert efficiency.seeds_failure({"ok": False, "output": "Rate limited: retry in 5s"}, None) is False
    assert efficiency.seeds_failure({"ok": False, "output": "Rate limited: slow down"}, {"decision": "allow"}) is False


def test_ordinary_failures_still_seed():
    assert efficiency.seeds_failure({"ok": False, "output": "Error: file not found"}, None) is True
    assert efficiency.seeds_failure({"ok": False, "output": "Error: Rate limited: x"}, None) is True


def test_existing_exclusions_remain():
    assert efficiency.seeds_failure({"ok": False, "output": "blocked by policy: rm"}, None) is False
    assert efficiency.seeds_failure({"ok": False, "output": "the user denied this"}, None) is False
    assert efficiency.seeds_failure({"ok": True, "output": "fine"}, None) is False
'''


def rate_check(ctx: Context) -> tuple[bool, str]:
    return _hidden(ctx, "test_rate", RATE_HIDDEN)


def rate_solve(ctx: Context) -> str:
    _replace(ctx, "harness/efficiency.py", '    if "blocked by policy" in output:',
             '    if output.startswith("Rate limited:"):\n        return False\n    if "blocked by policy" in output:')
    return "rate-limited results no longer seed dead ends"


# --- 3. quote_min_length --------------------------------------------------------------------------------------

QUOTE_PROMPT = (
    "The check that quotes in a final answer actually came from something the agent read only looks at quoted "
    "passages of at least 25 characters. Lower that minimum to 15 so shorter quotes are checked too." + VERIFY_NOTE)

QUOTE_HIDDEN = '''from harness import grounding

ANSWER = 'The page says "the quick brown fox" about it.'  # 19 characters quoted


def test_short_made_up_quote_is_flagged():
    assert grounding.ungrounded_quotes(ANSWER, ["nothing relevant here at all"]) == ["the quick brown fox"]


def test_short_real_quote_is_grounded():
    assert grounding.ungrounded_quotes(ANSWER, ["... the quick brown fox jumps ..."]) == []


def test_quotes_under_the_new_minimum_are_ignored():
    assert grounding.ungrounded_quotes('He said "hello there" once.', ["x"]) == []  # 11 characters


def test_longer_quotes_still_checked():
    long_quote = "this sentence was never in any source document"
    assert grounding.ungrounded_quotes(f'It says "{long_quote}".', ["unrelated"]) == [long_quote]
'''


def quote_check(ctx: Context) -> tuple[bool, str]:
    return _hidden(ctx, "test_quote", QUOTE_HIDDEN)


def quote_solve(ctx: Context) -> str:
    _replace(ctx, "harness/grounding.py", "MIN_QUOTE_CHARS = 25", "MIN_QUOTE_CHARS = 15")
    return "minimum quote length is now 15"


# --- 4. trash_is_a_deleter ------------------------------------------------------------------------------------

TRASH_PROMPT = (
    "The command policy refuses shell deletes outside the scratch area, but only recognizes `rm`, `rmdir`, `unlink` "
    "and `shred`. Make it treat `trash` and `trash-put` (the trash-cli commands) as deleters too, with exactly the "
    "same scratch-area rules." + VERIFY_NOTE)

TRASH_HIDDEN = '''from harness import policy


def deletes(command):
    return policy._delete_outside_scratch(command)


def test_trash_outside_scratch_is_a_delete():
    assert deletes("trash src/app.py")
    assert deletes("trash-put src/app.py docs/x.md")
    assert deletes("sudo trash-put /home/user/file")


def test_trash_in_scratch_is_fine():
    assert not deletes("trash /tmp/build.log")
    assert not deletes("trash-put scratch/notes.txt")


def test_existing_behavior_unchanged():
    assert deletes("rm src/app.py")
    assert not deletes("rm /tmp/x")
    assert not deletes("ls -la")
    assert not deletes("echo trashing nothing")
'''


def trash_check(ctx: Context) -> tuple[bool, str]:
    return _hidden(ctx, "test_trash", TRASH_HIDDEN)


def trash_solve(ctx: Context) -> str:
    rel = "harness/policy.py"
    _replace(ctx, rel, '_DELETERS = {"rm", "rmdir", "unlink", "shred"}', '_DELETERS = {"rm", "rmdir", "unlink", "shred", "trash", "trash-put"}')
    _replace(ctx, rel, "(rm|rmdir|unlink|shred)\\s", "(rm|rmdir|unlink|shred|trash-put|trash)\\s")
    return "trash and trash-put are deleters"


# --- 5. trace_round_reset (understand) ------------------------------------------------------------------------

TRACE_PROMPT = (
    "When the local model calls the `reset_round` tool, trace what happens. Answer with: the runner method that "
    "handles the call, the flag it sets in the session's run record, the compaction function that finally rewrites "
    "the message list, and the exact tag that prefixes the injected state message. Name each precisely; do not "
    "change any files.")

TRACE_TERMS = ("_reset_round_call", "pending_round_reset", "apply_round_reset", "[Task state]")


def trace_check(ctx: Context) -> tuple[bool, str]:
    if not has_all(ctx.answer, *TRACE_TERMS):
        return False, "answer missing: " + ", ".join(t for t in TRACE_TERMS if not has_all(ctx.answer, t))
    return True, "traced"


LARGE_TASKS = [
    Task("large_elide_ends", "code", ELIDE_PROMPT, large_files, elide_check, elide_solve),
    Task("large_rate_limit", "code", RATE_PROMPT, large_files, rate_check, rate_solve),
    Task("large_quote_min", "code", QUOTE_PROMPT, large_files, quote_check, quote_solve),
    Task("large_trash_deleter", "code", TRASH_PROMPT, large_files, trash_check, trash_solve),
    Task("large_trace_reset", "understand", TRACE_PROMPT, large_files, trace_check,
         lambda ctx: "_reset_round_call sets pending_round_reset; compaction.apply_round_reset injects a [Task state] message."),
]

for _task in LARGE_TASKS:
    _task.max_turns, _task.wall_limit = LARGE_LIMITS["max_turns"], LARGE_LIMITS["wall_limit"]
