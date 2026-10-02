"""Model load state and warm-up.

The always-on llama-server unloads the model after 10 idle minutes (`--sleep-idle-seconds 600`), and reloading Qwen
takes about a minute. `/props` reports `is_sleeping` without waking the server; a one-token chat request wakes it.

With the resource guard's `lazy_load` the server can also be parked: stopped, with the pause flag left in place so
its supervisor doesn't start it (and load the model) until something needs it. That is `unloaded` here.
`ensure_loaded` (a queued turn or an endpoint request) and `warm` (the user selected the local model in the app)
remove the flag; `load_now` also pins the model for a while, sending a one-token request every `keepalive_seconds`
so the idle unload doesn't fire. Page navigation alone never loads the model (docs/resource-guard.md).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

import httpx

from .config import ModelConfig

log = logging.getLogger("harness.warmup")

SLEEPING, WAKING, READY, UNREACHABLE, PAUSED = "sleeping", "waking", "ready", "unreachable", "paused"
UNLOADED, LOW_MEMORY = "unloaded", "low_memory"
EXPECTED_WAKE_SECONDS = 60  # Qwen reloads took 13-57 s in Phase 0 and ~50 s in the Phase 2 exit test
HEALTH_TIMEOUT_SECONDS = 300
HEALTH_POLL_SECONDS = 2.0


class ModelWarmer:
    def __init__(self) -> None:
        self._waking: dict[str, asyncio.Task] = {}
        self._wake_started: dict[str, float] = {}
        self.blocked = lambda: False  # the GPU guard has the model server stopped; don't wake it
        self.control: Callable[[], object | None] = lambda: None  # gpu_guard.ServerControl, when the harness parks it
        self.managed_model = ""       # the model served by that server (cfg.default_model)
        self.memory_low = lambda: False
        self.keepalive_seconds = 300.0
        self.pinned_until: float | None = None  # epoch seconds; "Load local model now" keeps it loaded until then
        self._keepalive: asyncio.Task | None = None
        self._changed = asyncio.Event()  # set by notify(): the guard changed state, so re-check a load in progress

    def _control_for(self, model: ModelConfig):
        return self.control() if model.name == self.managed_model else None

    def parked(self, model: ModelConfig) -> bool:
        ctl = self._control_for(model)
        return ctl is not None and ctl.flagged()

    def pinned(self) -> bool:
        return self.pinned_until is not None and time.time() < self.pinned_until

    async def state(self, model: ModelConfig) -> str:
        if self.blocked():
            return PAUSED
        if model.name in self._waking and not self._waking[model.name].done():
            return WAKING
        if self.parked(model):
            return UNLOADED
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{model.base_url}/props")
            if resp.status_code != 200:
                return UNREACHABLE
            return SLEEPING if resp.json().get("is_sleeping") else READY
        except (httpx.HTTPError, ValueError):
            return UNREACHABLE

    async def warm(self, model: ModelConfig, *, force: bool = False) -> str:
        """Start loading the model if it's asleep or parked. Returns the state before warming, or `low_memory`
        when RAM is short (nothing is loaded unless `force`)."""
        state = await self.state(model)
        if state in (SLEEPING, UNLOADED):
            if self.memory_low() and not force:
                return LOW_MEMORY
            self._start_wake(model)
        return state

    async def ensure_loaded(self, model: ModelConfig) -> None:
        """Before a model call: if the server is parked, start it and wait until it has loaded the model (or the
        guard stops it again). A sleeping server needs nothing; the call itself wakes it."""
        if self._control_for(model) is None:
            return
        task = self._waking.get(model.name)
        if (task is None or task.done()) and self.parked(model) and not self.blocked():
            task = self._start_wake(model)
        if task is not None and not task.done():
            await asyncio.shield(task)

    async def load_now(self, model: ModelConfig, seconds: float) -> str:
        """Load the model and keep it loaded for `seconds`. Returns the state before loading."""
        self.pinned_until = time.time() + seconds
        state = await self.warm(model, force=True)
        if self._keepalive is None or self._keepalive.done():
            self._keepalive = asyncio.create_task(self._keep_loaded(model), name=f"keepalive-{model.name}")
        return state

    def notify(self) -> None:
        """The guard's state or the pause flag changed; a load waiting on /health re-checks now."""
        self._changed.set()

    def unpin(self) -> None:
        self.pinned_until = None
        if self._keepalive is not None:
            self._keepalive.cancel()
            self._keepalive = None

    def waking_for(self, model: ModelConfig) -> float | None:
        """Seconds since a warm-up started, if one is running."""
        task = self._waking.get(model.name)
        if task is None or task.done():
            return None
        return time.monotonic() - self._wake_started[model.name]

    def _start_wake(self, model: ModelConfig) -> asyncio.Task:
        self._wake_started[model.name] = time.monotonic()
        task = asyncio.create_task(self._wake(model), name=f"warm-{model.name}")
        self._waking[model.name] = task
        return task

    async def _wake(self, model: ModelConfig) -> None:
        ctl = self._control_for(model)
        if ctl is not None and ctl.flagged():
            await self._unpark(model, ctl)
            return
        await self._ping(model, "warmed")

    async def _unpark(self, model: ModelConfig, ctl) -> None:
        """Remove the pause flag; the supervisor starts llama-server, which loads the model before /health is OK."""
        started = time.monotonic()
        log.info("loading %s (removing the pause flag)", model.name)
        await ctl.start()
        deadline = started + HEALTH_TIMEOUT_SECONDS
        while (remaining := deadline - time.monotonic()) > 0:
            self._changed.clear()
            if self.blocked() or ctl.flagged():
                log.info("loading %s stopped: the guard holds the GPU again", model.name)
                return
            if await ctl.healthy():
                log.info("loaded %s in %.0f s", model.name, time.monotonic() - started)
                return
            try:  # /health has no push; a guard change (notify) cuts the wait short
                await asyncio.wait_for(self._changed.wait(), min(HEALTH_POLL_SECONDS, remaining))
            except asyncio.TimeoutError:
                pass
        log.warning("%s did not answer /health within %d s", model.name, HEALTH_TIMEOUT_SECONDS)

    async def _ping(self, model: ModelConfig, verb: str) -> None:
        payload = {"model": model.name, "messages": [{"role": "user", "content": "ok"}], "max_tokens": 1,
                   "chat_template_kwargs": {"enable_thinking": False}}
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
                resp = await client.post(f"{model.base_url}/v1/chat/completions", json=payload)
            log.info("%s %s in %.0f s (HTTP %s)", verb, model.name, time.monotonic() - started, resp.status_code)
        except httpx.HTTPError as e:
            log.warning("%s %s failed: %s", verb, model.name, e)

    async def _keep_loaded(self, model: ModelConfig) -> None:
        """While pinned, a one-token request every keepalive_seconds resets llama-server's idle timer."""
        while self.pinned():
            await asyncio.sleep(max(0.05, min(self.keepalive_seconds, self.pinned_until - time.time())))
            if not self.pinned():
                break
            state = await self.state(model)
            if state in (SLEEPING, UNLOADED):
                self._start_wake(model)  # it went down anyway (a GPU hold ended, an unload): bring it back
            elif state == READY:
                await self._ping(model, "kept")
        self.pinned_until = None
