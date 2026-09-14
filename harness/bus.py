"""Event fan-out. Persisted events carry a `seq`; ephemeral ones (token deltas, queue moves) don't."""

from __future__ import annotations

import asyncio

from .db import Database


class Subscription:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        # Set when a persisted event didn't fit; the reader re-reads from the DB by seq.
        self.overflowed = False


class EventBus:
    def __init__(self, db: Database):
        self.db = db
        self._subs: dict[str, set[Subscription]] = {}

    def emit(self, sid: str, type_: str, data: dict) -> dict:
        event = self.db.insert_event(sid, type_, data)
        self._publish(sid, event)
        return event

    def ephemeral(self, sid: str, type_: str, data: dict) -> None:
        self._publish(sid, {"seq": None, "session_id": sid, "type": type_, "data": data})

    def _publish(self, sid: str, event: dict) -> None:
        for sub in list(self._subs.get(sid, ())):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                if event["seq"] is not None:
                    sub.overflowed = True

    def subscribe(self, sid: str) -> Subscription:
        sub = Subscription()
        self._subs.setdefault(sid, set()).add(sub)
        return sub

    def unsubscribe(self, sid: str, sub: Subscription) -> None:
        self._subs.get(sid, set()).discard(sub)
