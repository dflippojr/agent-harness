"""Fake model mode: a scripted stand-in for llama-server, for GPU-free full-stack UI testing (#588, docs/ui-testing.md).

With ``HARNESS_FAKE_MODEL=<fixture dir>`` the daemon answers every local model call from fixture scripts instead of
llama-server. Everything else is the real server: the runner loop, policy and approvals, the file tools and sandbox,
compaction, events and the web client. Only the disposable instance (``python -m harness.fake_instance``) can turn it
on: the daemon refuses the variable unless its data_dir holds the marker that command writes, and no YAML key enables
it, so neither the production config nor a stray environment variable can swap the real model for a script.

Fake mode never touches the GPU: it drops the local-model supervision, image and inference-endpoint modules, so there
is no warmer, no GPU guard (the manual hold has nothing to act on) and nothing that reads a model's base_url. Nor does
it touch the Docker engine the real harness shares: sessions get sandbox.NoDockerSandbox, so the shell tools return an
error, and maintenance leaves session containers alone.

A fixture file (YAML; every ``*.yaml`` / ``*.yml`` in the directory, in name order) looks like:

    context_tokens: 16384       # the fake model's context; compaction thresholds scale from it
    summary: "## Done so far ..."  # what a compaction summary request gets back
    scripts:
      - name: approval-sheet    # slug; it also goes into the tool-call ids
        match: "protected"      # case-insensitive regex searched in the user's message (or `prompt:`, exact)
        turns:                  # one model call each; every turn but the last needs tool_calls
          - reasoning: "Checking the rules."
            text: ["I'll update ", "the protected file."]   # streamed chunk by chunk (a string is split by words)
            delay: 0.05         # seconds between chunks
            wait: 1.0           # seconds before the first chunk
            tool_calls:
              - {name: write_file, args: {path: protected/settings.json, content: "{}"}}
          - error: {message: "HTTP 500: simulated", retryable: true, times: 1}  # fails 1-3 times, then:
            text: "Done."
            filler_chars: 40000 # a long reply: pads the text so the next turn compacts

The next turn comes from the tool-call ids on the latest assistant message (``fake-<script>-<turn>-<n>-<random>``), so a
run continues deterministically after an approval, a compaction (which always keeps the newest turn) or a daemon
restart. A new user message picks a script again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import compaction, grounding
from .llm import Completion, LLMError

log = logging.getLogger("harness.fake_model")

ENV = "HARNESS_FAKE_MODEL"
MARKER = "fake-model-instance"          # in data_dir: written only by harness.fake_instance
MODEL_NAME = "fake"
UNROUTABLE = "http://127.0.0.1:9"       # discard port: nothing should ever call it
DEFAULT_CONTEXT = 16384
DEFAULT_FIXTURES = Path(__file__).resolve().parent / "fake_fixtures"
# Add-on packages that supervise llama-server, hold the GPU or serve GPU work; fake mode never loads them.
GPU_PACKAGES = ("harness_modules.local_model", "harness_modules.endpoint", "harness_modules.images")
GPU_SWITCHES = ("gpu_guard", "endpoint", "images", "image_edit")
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
CALL_ID = re.compile(r"^fake-([a-z0-9][a-z0-9-]*)-(\d+)-\d+-[0-9a-f]{12}$")
TURN_KEYS = {"text", "reasoning", "delay", "wait", "tool_calls", "error", "filler_chars", "finish_reason"}
DEFAULT_SUMMARY = ("## User requests\nnone\n## Done so far\nThe fake model ran a scripted task.\n## Findings\n"
                   "Nothing real: this is fake model mode.\n## Answer so far\nnone yet\n## Next steps\n"
                   "Continue the script.")
FILLER = ("This paragraph is filler from fake model mode. It makes the reply long enough that the next turn "
          "crosses the compaction threshold, so Agent Harness Web shows the compaction rows. ")


class FixtureError(ValueError):
    pass


class FakeModeRefused(RuntimeError):
    pass


@dataclass
class Turn:
    text: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    delay: float = 0.02
    wait: float = 0.0
    tool_calls: list[dict] = field(default_factory=list)
    error: dict | None = None
    filler_chars: int = 0
    finish_reason: str = ""


@dataclass
class Script:
    name: str
    turns: list[Turn]
    prompt: str | None = None
    match: re.Pattern | None = None

    def matches(self, text: str) -> bool:
        if self.prompt is not None:
            return text.strip() == self.prompt.strip()
        return bool(self.match and self.match.search(text))


@dataclass
class Fixtures:
    scripts: list[Script]
    context_tokens: int = DEFAULT_CONTEXT
    summary: str = DEFAULT_SUMMARY
    sources: list[str] = field(default_factory=list)

    def by_name(self, name: str) -> Script | None:
        return next((s for s in self.scripts if s.name == name), None)

    def find(self, text: str) -> Script | None:
        return next((s for s in self.scripts if s.matches(text)), None)


def _chunks(value, where: str) -> list[str]:
    """A string streams word by word (whitespace kept); a list streams as given."""
    if value is None:
        return []
    if isinstance(value, str):
        return re.findall(r"\S+\s*|\s+", value)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise FixtureError(f"{where}: must be a string or a list of strings")


def _seconds(value, where: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 60:
        raise FixtureError(f"{where}: must be a number of seconds from 0 to 60")
    return float(value)


def _tool_call(raw, where: str) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not raw["name"]:
        raise FixtureError(f"{where}: needs a name")
    args = raw.get("args", {})
    if isinstance(args, str):  # raw JSON text: lets a fixture send malformed arguments on purpose
        return {"name": raw["name"], "arguments": args}
    if not isinstance(args, dict):
        raise FixtureError(f"{where}: args must be a mapping or a JSON string")
    return {"name": raw["name"], "arguments": json.dumps(args)}


def _turn(raw, where: str) -> Turn:
    if not isinstance(raw, dict):
        raise FixtureError(f"{where}: must be a mapping")
    unknown = sorted(set(raw) - TURN_KEYS)
    if unknown:
        raise FixtureError(f"{where}: unknown keys {unknown}; known: {', '.join(sorted(TURN_KEYS))}")
    error = raw.get("error")
    if error is not None:
        if not isinstance(error, dict) or not isinstance(error.get("message"), str):
            raise FixtureError(f"{where}.error: needs a message")
        from .runner import MODEL_CALL_ATTEMPTS
        retries = MODEL_CALL_ATTEMPTS - 1   # more failures than retries would fail the run instead
        retryable = bool(error.get("retryable", False))
        times = error.get("times", 1)
        if not retryable and "times" in error:
            raise FixtureError(f"{where}.error.times: a non-retryable error fails every call; drop times")
        if isinstance(times, bool) or not isinstance(times, int) or not 1 <= times <= retries:
            raise FixtureError(f"{where}.error.times: must be 1 to {retries} (for a failed run, use retryable: false)")
        error = {"message": error["message"], "retryable": retryable, "times": times}
    filler = raw.get("filler_chars", 0)
    if isinstance(filler, bool) or not isinstance(filler, int) or not 0 <= filler <= 2_000_000:
        raise FixtureError(f"{where}.filler_chars: must be an integer from 0 to 2000000")
    calls = raw.get("tool_calls") or []
    if not isinstance(calls, list):
        raise FixtureError(f"{where}.tool_calls: must be a list")
    return Turn(text=_chunks(raw.get("text"), f"{where}.text"),
                reasoning=_chunks(raw.get("reasoning"), f"{where}.reasoning"),
                delay=_seconds(raw.get("delay"), f"{where}.delay", 0.02),
                wait=_seconds(raw.get("wait"), f"{where}.wait", 0.0),
                tool_calls=[_tool_call(c, f"{where}.tool_calls[{i}]") for i, c in enumerate(calls)],
                error=error, filler_chars=filler, finish_reason=str(raw.get("finish_reason") or ""))


def _script(raw, where: str) -> Script:
    if not isinstance(raw, dict):
        raise FixtureError(f"{where}: must be a mapping")
    name = raw.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        raise FixtureError(f"{where}.name: must be a lowercase slug (a-z, 0-9, -)")
    if ("prompt" in raw) == ("match" in raw):
        raise FixtureError(f"{where}: needs exactly one of prompt (exact) or match (regex)")
    pattern = None
    if "match" in raw:
        try:
            pattern = re.compile(str(raw["match"]), re.IGNORECASE)
        except re.error as e:
            raise FixtureError(f"{where}.match: {e}") from None
    turns = raw.get("turns")
    if not isinstance(turns, list) or not turns:
        raise FixtureError(f"{where}.turns: needs at least one turn")
    return Script(name=name, prompt=str(raw["prompt"]) if "prompt" in raw else None, match=pattern,
                  turns=[_turn(t, f"{where}.turns[{i}]") for i, t in enumerate(turns)])


def load_fixtures(path: Path | str) -> Fixtures:
    """Read a fixture file or every fixture file in a directory (name order). Scripts are tried in that order."""
    path = Path(path)
    files = sorted(p for p in path.iterdir() if p.suffix in (".yaml", ".yml")) if path.is_dir() else [path]
    if not path.exists() or not files:
        raise FixtureError(f"{path}: no fixture files")
    fixtures = Fixtures(scripts=[])
    for file in files:
        try:
            raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            raise FixtureError(f"{file}: {e}") from None
        if not isinstance(raw, dict) or not isinstance(raw.get("scripts", []), list):
            raise FixtureError(f"{file}: needs a scripts list")
        if "context_tokens" in raw:
            tokens = raw["context_tokens"]
            if isinstance(tokens, bool) or not isinstance(tokens, int) or not 2048 <= tokens <= 1_048_576:
                raise FixtureError(f"{file}.context_tokens: must be an integer from 2048 to 1048576")
            fixtures.context_tokens = tokens
        if "summary" in raw:
            fixtures.summary = str(raw["summary"])
        for i, spec in enumerate(raw.get("scripts") or []):
            script = _script(spec, f"{file.name}.scripts[{i}]")
            if fixtures.by_name(script.name):
                raise FixtureError(f"{file.name}: script {script.name!r} is defined twice")
            fixtures.scripts.append(script)
        fixtures.sources.append(str(file))
    return fixtures


def _call_ids(message: dict) -> tuple[str, int] | None:
    """(script, turn) from an assistant message's fake tool-call ids."""
    for call in message.get("tool_calls") or []:
        found = CALL_ID.match(str(call.get("id") or ""))
        if found:
            return found.group(1), int(found.group(2))
    return None


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):  # OpenAI content parts
        return " ".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return str(content or "")


class FakeModel:
    """Drop-in for ``llm.chat``: answers from fixture scripts, streams through on_delta, reports usage."""

    def __init__(self, fixtures: Fixtures):
        self.fixtures = fixtures
        # A call's context -> retryable errors raised for it so far. Cleared once the call gets through (always within
        # the harness's retries), so the next session that reaches the same context fails the same way again.
        self._failures: dict[str, int] = {}

    @classmethod
    def from_path(cls, path: Path | str) -> "FakeModel":
        return cls(load_fixtures(path))

    def position(self, messages: list[dict]) -> tuple[Script | None, int]:
        """The script and turn for the next reply. Walks back from the newest message: an assistant message with
        fake tool calls continues its script; a user message that matches a script starts it; the user messages the
        harness adds itself (its nudges, a compaction summary) are skipped; any other user message gets the
        fallback, so a script never resumes past someone's unmatched message."""
        from .runner import CONTINUE_NUDGE, CUT_OFF_NUDGE
        for message in reversed(messages):
            role = message.get("role")
            if role == "assistant":
                found = _call_ids(message)
                if found is None:
                    return None, 0
                script = self.fixtures.by_name(found[0])
                return (script, found[1] + 1) if script else (None, 0)
            if role != "user":
                continue
            text = _text(message)
            if (text.startswith((compaction.SUMMARY_TAG, compaction.STATE_TAG))
                    or text in (compaction.ROUND_CONTINUE, CONTINUE_NUDGE, CUT_OFF_NUDGE)
                    or grounding.NUDGE_MARK in text):
                continue
            return self.fixtures.find(text), 0
        return None, 0

    async def __call__(self, _model, messages: list[dict], tools: list[dict] | None = None, on_delta=None,
                       max_tokens: int | None = None, extra: dict | None = None, timeout: float = 0,
                       on_progress=None) -> Completion:
        del max_tokens, extra, timeout  # llm.chat's signature; a script decides its own length
        prompt_chars = sum(compaction.message_chars(m) for m in messages) + len(json.dumps(tools or []))
        prompt_tokens = max(1, prompt_chars // 3)
        if on_progress:  # llama-server's prompt_progress: nothing cached, then the whole prompt read
            await on_progress(0, prompt_tokens, 0)
            await on_progress(prompt_tokens, prompt_tokens, 0)
        if messages and messages[0].get("content") == compaction.SUMMARY_SYSTEM:  # a compaction summary request
            return await self._summary(prompt_tokens, on_delta)
        script, index = self.position(messages)
        if script is None:
            log.info("fake model: no script matches; fallback reply")
            return await self._emit(self._fallback(), prompt_tokens, on_delta, "", 0, tools)
        if index >= len(script.turns):
            log.info("fake model: script %s has no turn %d; ending the run", script.name, index)
            turn = Turn(text=[f"[fake model] Script {script.name} has no turn {index + 1}; stopping here."])
        else:
            turn = script.turns[index]
        log.info("fake model: script %s turn %d", script.name, index)
        if turn.error is not None and not turn.error["retryable"]:
            raise LLMError(turn.error["message"], retryable=False)   # every time: a rerun fails the same way
        if turn.error is not None:
            key = hashlib.sha256(json.dumps(messages, sort_keys=True, default=str).encode()).hexdigest()
            seen = self._failures.get(key, 0)
            if seen < turn.error["times"]:
                self._failures[key] = seen + 1
                if turn.wait:
                    await asyncio.sleep(turn.wait)
                raise LLMError(turn.error["message"], retryable=turn.error["retryable"])
            self._failures.pop(key, None)
            if not turn.text and not turn.tool_calls and not turn.reasoning:
                turn = Turn(text=["[fake model] Recovered after the scripted error."])
        return await self._emit(turn, prompt_tokens, on_delta, script.name, index, tools)

    def _fallback(self) -> Turn:
        names = ", ".join(s.name for s in self.fixtures.scripts) or "none"
        return Turn(text=_chunks("[fake model] No fixture script matches this message. Scripts: " + names + ".",
                                 "fallback"))

    async def _emit(self, turn: Turn, prompt_tokens: int, on_delta, script: str, index: int,
                    tools: list[dict] | None) -> Completion:
        if turn.wait:
            await asyncio.sleep(turn.wait)
        out = Completion(prompt_tokens=prompt_tokens)
        chunks = list(turn.text)
        if turn.filler_chars:
            filler = (FILLER * (turn.filler_chars // len(FILLER) + 1))[:turn.filler_chars]
            chunks += ["\n\n"] + [filler[i:i + 2000] for i in range(0, len(filler), 2000)]
        for kind, parts in (("reasoning", turn.reasoning), ("content", chunks)):
            for i, part in enumerate(parts):
                if i and turn.delay:
                    await asyncio.sleep(turn.delay)
                if kind == "reasoning":
                    out.reasoning += part
                else:
                    out.content += part
                if on_delta:
                    await on_delta(kind, part)
        if tools:  # a request without tools (a summary, a delegate edit, Chat without web tools) gets no calls
            # Unique per call (the runner matches approvals and results by id); script and turn are what resume uses.
            out.tool_calls = [{"id": f"fake-{script}-{index}-{n}-{uuid.uuid4().hex[:12]}", "type": "function",
                               "function": dict(call)} for n, call in enumerate(turn.tool_calls)]
        out.finish_reason = turn.finish_reason or ("tool_calls" if out.tool_calls else "stop")
        out.completion_tokens = max(1, (len(out.content) + len(out.reasoning)) // 4)
        out.prompt_ms = 1.0
        out.decode_ms = 1.0
        return out

    async def _summary(self, prompt_tokens: int, on_delta) -> Completion:
        log.info("fake model: compaction summary")
        return await self._emit(Turn(text=_chunks(self.fixtures.summary, "summary"), delay=0.0), prompt_tokens,
                                on_delta, "", 0, None)


def apply(cfg, env=None) -> None:
    """Switch a loaded Config to fake model mode when HARNESS_FAKE_MODEL is set. Refuses outside a disposable
    instance: the data_dir must hold the marker only `python -m harness.fake_instance` writes."""
    raw = ((env if env is not None else os.environ).get(ENV) or "").strip()
    if not raw:
        return
    marker = Path(cfg.data_dir) / MARKER
    if not marker.is_file():
        raise FakeModeRefused(f"{ENV} is set but {cfg.data_dir} is not a disposable fake-model instance (no {MARKER} "
                              "marker). Fake model mode runs only from `python -m harness.fake_instance` "
                              "(docs/ui-testing.md); unset the variable to start normally.")
    fixtures = load_fixtures(raw)  # a broken fixture fails the start, not the first session
    from .config import ModelConfig
    from .modules import namespace_packages
    cfg.fake_model = Path(raw)
    cfg.models = {MODEL_NAME: ModelConfig(name=MODEL_NAME, base_url=UNROUTABLE,
                                          context_tokens=fixtures.context_tokens, max_tokens=2048)}
    cfg.default_model = MODEL_NAME
    cfg.modules.local_model = cfg.installed.local_model = True
    for name in GPU_SWITCHES:
        setattr(cfg.modules, name, False)
        setattr(cfg.installed, name, False)
    cfg.gpu_guard.enabled = cfg.images.enabled = cfg.images.edit_enabled = cfg.endpoint.enabled = False
    packages = cfg.module_packages if cfg.module_packages is not None else namespace_packages()
    cfg.module_packages = tuple(p for p in packages if p not in GPU_PACKAGES)


def banner(cfg) -> str:
    return "\n".join([
        "=" * 78,
        "FAKE MODEL MODE: every local model reply is scripted. No GPU, no llama-server.",
        f"  fixtures: {cfg.fake_model}",
        f"  data_dir: {cfg.data_dir} (disposable)",
        "  This is a test instance for UI testing (docs/ui-testing.md). Never point it at real data.",
        "=" * 78,
    ])
