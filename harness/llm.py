"""Streaming client for llama-server's OpenAI-compatible chat endpoint."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

from .config import ModelConfig


class LLMError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Completion:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tps: float = 0.0
    gen_tps: float = 0.0


DeltaCallback = Callable[[str, str], Awaitable[None]]  # (kind: "content" | "reasoning", text)
# (processed, total, cache): prompt tokens evaluated so far (cached prefix included), the whole prompt, and the
# cached prefix (-1 if the server doesn't say)
ProgressCallback = Callable[[int, int, int], Awaitable[None]]


async def _apply_chunk(chunk: dict, out: Completion, calls: dict[int, dict],
                       on_delta: DeltaCallback | None, on_progress: ProgressCallback | None) -> None:
    progress = chunk.get("prompt_progress")
    if progress and on_progress:
        await on_progress(int(progress.get("processed", 0)), int(progress.get("total", 0)),
                          int(progress.get("cache", -1)))
    if chunk.get("usage"):
        out.prompt_tokens = chunk["usage"].get("prompt_tokens", 0)
        out.completion_tokens = chunk["usage"].get("completion_tokens", 0)
    timings = chunk.get("timings") or {}
    if timings:
        out.prompt_tps = timings.get("prompt_per_second", 0.0)
        out.gen_tps = timings.get("predicted_per_second", 0.0)
    for choice in chunk.get("choices") or []:
        await _apply_choice(choice, out, calls, on_delta)


async def _apply_choice(choice: dict, out: Completion, calls: dict[int, dict],
                        on_delta: DeltaCallback | None) -> None:
    delta = choice.get("delta") or {}
    if delta.get("reasoning_content"):
        out.reasoning += delta["reasoning_content"]
        if on_delta:
            await on_delta("reasoning", delta["reasoning_content"])
    if delta.get("content"):
        out.content += delta["content"]
        if on_delta:
            await on_delta("content", delta["content"])
    for tc in delta.get("tool_calls") or []:
        slot = calls.setdefault(tc.get("index", 0), {
            "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
        if tc.get("id"):
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        slot["function"]["name"] += fn.get("name") or ""
        slot["function"]["arguments"] += fn.get("arguments") or ""
    if choice.get("finish_reason"):
        out.finish_reason = choice["finish_reason"]


async def chat(
    model: ModelConfig,
    messages: list[dict],
    tools: list[dict] | None = None,
    on_delta: DeltaCallback | None = None,
    max_tokens: int | None = None,
    extra: dict | None = None,
    timeout: float = 1800,
    on_progress: ProgressCallback | None = None,
) -> Completion:
    payload = {
        "model": model.name,
        "messages": messages,
        "max_tokens": max_tokens or model.max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        **model.sampling,
        **(extra or {}),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if on_progress:
        payload["return_progress"] = True  # llama-server streams prompt_progress chunks while reading the prompt

    out = Completion()
    calls: dict[int, dict] = {}
    # Cold model loads take up to a minute and long prompts ~70 s, so only the connect timeout is short.
    limits = httpx.Timeout(timeout, connect=10)
    try:
        async with httpx.AsyncClient(timeout=limits) as client:
            async with client.stream("POST", f"{model.base_url}/v1/chat/completions", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    # llama-server answers 500 when it can't parse the model's tool-call syntax: resample.
                    raise LLMError(f"HTTP {resp.status_code}: {body[:500]}", retryable=resp.status_code >= 500)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if "error" in chunk:
                        raise LLMError(f"stream error: {json.dumps(chunk['error'])[:500]}", retryable=True)
                    await _apply_chunk(chunk, out, calls, on_delta, on_progress)
    except httpx.HTTPError as e:
        raise LLMError(f"{type(e).__name__}: {e}", retryable=isinstance(e, (httpx.ReadError, httpx.RemoteProtocolError,
                                                                              httpx.ConnectError)))
    for index in sorted(calls):
        call = calls[index]
        if not call["id"]:
            call["id"] = f"call_{int(time.time() * 1000)}_{index}"
        out.tool_calls.append(call)
    return out
