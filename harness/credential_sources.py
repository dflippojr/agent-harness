"""Pluggable credential sources for hosted CLI sessions (#365; #393 reuses this for per-member API keys).

A *source* decides whose credential a hosted session runs under when it is not the default (the backend's own login,
the owner's token or an App's API key, which `runner._backend_credential` still resolves). A source:

- `selects` a session (by its App and end user) or leaves it to the next source and, failing all, to the default;
- says what the session's container gets: `docker_args` (mounts and env by name) and `secret_env` (values the docker
  client passes by name, never on the command line);
- checks the credential is usable before the container starts (`ready`), refusing with a stable `code`;
- names a `lock_key` when two sessions on one credential must not run at once (one refresh token, #390), and a
  `label` the usage log records as the credential source.

A source never falls back: a session it selects either runs on that source's credential or is refused. Sources are
tried in registration order. Register one at import time with `register`.
"""

from __future__ import annotations

import asyncio
import contextlib


class CredentialRefused(Exception):
    """The source has no usable credential for this session. `code` is the stable code an App sees."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CredentialSource:
    name = ""
    label = ""  # the value recorded in `usage.credential_source`

    def selects(self, app_id: str, end_user: str) -> bool:
        return False

    def docker_args(self, backend: str, cfg, app_id: str, end_user: str) -> list[str]:
        raise NotImplementedError

    def secret_env(self, backend: str, cfg, app_id: str, end_user: str) -> dict[str, str]:
        return {}

    async def ready(self, backend: str, cfg, app_id: str, end_user: str) -> None:
        """Raise `CredentialRefused` unless a session can start on this credential."""

    def lock_key(self, backend: str, app_id: str, end_user: str) -> str:
        return ""


_SOURCES: list[CredentialSource] = []


def register(source: CredentialSource) -> CredentialSource:
    if all(s.name != source.name for s in _SOURCES):
        _SOURCES.append(source)
    return source


def select(app_id: str, end_user: str) -> CredentialSource | None:
    return next((s for s in _SOURCES if s.selects(app_id, end_user)), None)


class KeyedLocks:
    """One asyncio lock per key, dropped once nobody holds or waits for it. `hold(key)` queues behind the holder."""

    def __init__(self) -> None:
        self._locks: dict[str, list] = {}  # key -> [lock, holders and waiters]

    @contextlib.asynccontextmanager
    async def hold(self, key: str):
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._locks.pop(key, None)
