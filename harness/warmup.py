"""Model sleep state and warm-up.

The always-on llama-server unloads the model after 30 idle minutes, and reloading Qwen takes about a minute.
`/props` reports `is_sleeping` without waking the server; a one-token chat request wakes it. The web app asks for
a warm-up when it opens, so the model is usually loaded by the time a task is typed.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .config import ModelConfig

log = logging.getLogger("harness.warmup")

SLEEPING, WAKING, READY, UNREACHABLE = "sleeping", "waking", "ready", "unreachable"
EXPECTED_WAKE_SECONDS = 60  # Qwen reloads took 13-57 s in Phase 0 and ~50 s in the Phase 2 exit test


class ModelWarmer:
    def __init__(self) -> None:
        self._waking: dict[str, asyncio.Task] = {}
        self._wake_started: dict[str, float] = {}

    async def state(self, model: ModelConfig) -> str:
        if model.name in self._waking and not self._waking[model.name].done():
            return WAKING
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{model.base_url}/props")
            if resp.status_code != 200:
                return UNREACHABLE
            return SLEEPING if resp.json().get("is_sleeping") else READY
        except (httpx.HTTPError, ValueError):
            return UNREACHABLE

    async def warm(self, model: ModelConfig) -> str:
        """Start loading the model if it's asleep. Returns the state before warming."""
        state = await self.state(model)
        if state == SLEEPING:
            self._wake_started[model.name] = time.monotonic()
            self._waking[model.name] = asyncio.create_task(self._wake(model), name=f"warm-{model.name}")
            return SLEEPING
        return state

    def waking_for(self, model: ModelConfig) -> float | None:
        """Seconds since a warm-up started, if one is running."""
        task = self._waking.get(model.name)
        if task is None or task.done():
            return None
        return time.monotonic() - self._wake_started[model.name]

    async def _wake(self, model: ModelConfig) -> None:
        payload = {"model": model.name, "messages": [{"role": "user", "content": "ok"}], "max_tokens": 1,
                   "chat_template_kwargs": {"enable_thinking": False}}
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
                resp = await client.post(f"{model.base_url}/v1/chat/completions", json=payload)
            log.info("warmed %s in %.0f s (HTTP %s)", model.name, time.monotonic() - started, resp.status_code)
        except httpx.HTTPError as e:
            log.warning("warming %s failed: %s", model.name, e)
