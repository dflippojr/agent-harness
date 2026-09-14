"""Phone notifications through a self-hosted ntfy server.

Tapping a notification opens the session in the web app; long-pressing it shows Approve / Deny buttons, which
the ntfy iOS app sends straight to the daemon from the phone (so the phone must be on the tailnet). Those
buttons carry a per-approval secret instead of a login.

The iOS app doesn't dismiss a notification after an action button (ntfy issue #1728), so once an approval is
decided the notification is replaced, using the approval id as ntfy's sequence id.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from .config import Config
from .db import Database

log = logging.getLogger("harness.notify")

WATCHED = {"approval_requested", "approval_decided", "run_finished", "model_waking"}


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_call(tool: str, args: dict) -> str:
    if tool == "run_shell":
        return ("🌐 " if args.get("network") else "") + args.get("command", "")
    if tool in ("write_file", "edit_file"):
        return f"{tool} {args.get('path', '')}"
    if tool == "git_clone":
        return f"git clone {args.get('url', '')}"
    return f"{tool} {args}"


class Notifier:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._task: asyncio.Task | None = None
        self.sent: list[dict] = []  # last payloads, for tests and /notify/test

    @property
    def enabled(self) -> bool:
        return self.cfg.notify.enabled

    def _token(self) -> str:
        path = self.cfg.notify.token_file
        try:
            return Path(path).read_text(encoding="utf-8").strip() if path else ""
        except OSError:
            log.warning("ntfy token file %s is unreadable", path)
            return ""

    def listener(self, event: dict) -> None:
        if self.enabled and event["type"] in WATCHED:
            try:
                self.queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("notification queue full; dropping %s", event["type"])

    def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._run(), name="notifier")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                event = await self.queue.get()
                try:
                    payload = self.build(event)
                    if payload:
                        await self.publish(client, payload)
                except Exception:  # noqa: BLE001 - a failed notification must not stop later ones
                    log.exception("notification for event %s failed", event.get("seq"))

    async def publish(self, client: httpx.AsyncClient, payload: dict) -> None:
        self.sent = (self.sent + [payload])[-50:]
        headers = {}
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        for attempt in range(3):
            try:
                resp = await client.post(self.cfg.notify.server, json=payload, headers=headers)
                if resp.status_code < 300:
                    return
                log.warning("ntfy answered %s: %s", resp.status_code, resp.text[:200])
                if resp.status_code < 500:
                    return
            except httpx.HTTPError as e:
                log.warning("ntfy unreachable (%s)", e)
            await asyncio.sleep(2 * (attempt + 1))

    def link(self, path: str) -> str:
        return f"{self.cfg.public_url}{path}" if self.cfg.public_url else ""

    def build(self, event: dict) -> dict | None:
        sid, d = event["session_id"], event["data"]
        session = self.db.get_session(sid)
        if session is None:
            return None
        title = _short(session["title"], 60)
        base = {"topic": self.cfg.notify.topic}

        if event["type"] == "approval_requested":
            approval = self.db.get_approval(d["id"])
            if approval is None or approval["status"] != "pending":
                return None
            payload = {
                **base,
                "sequence_id": approval["id"],
                "title": f"Approve? {title}",
                "message": f"{d['reason'] or 'needs approval'}: {_short(describe_call(d['tool'], d['args']), 300)}",
                "priority": 4,
                "tags": ["warning"],
                "click": self.link(f"/#/s/{sid}/approval/{approval['id']}"),
            }
            if self.cfg.public_url and approval["token"]:
                payload["actions"] = [
                    {"action": "http", "label": "Approve", "method": "POST", "clear": True,
                     "url": self.link(f"/a/{approval['token']}/approve")},
                    {"action": "http", "label": "Deny", "method": "POST", "clear": True,
                     "url": self.link(f"/a/{approval['token']}/deny")},
                ]
            return payload

        if event["type"] == "model_waking":
            return {**base, "title": f"Waking the model: {title}", "priority": 2, "tags": ["hourglass"],
                    "message": f"The model was asleep; the first step takes about {d['expected_seconds']} s.",
                    "click": self.link(f"/#/s/{sid}")}

        if event["type"] == "approval_decided":
            approval = self.db.get_approval(d["id"])
            if approval is None:
                return None
            mark = "✅ Approved" if d["status"] == "approved" else "🚫 Denied"
            return {**base, "sequence_id": approval["id"], "title": f"{mark}: {title}",
                    "message": _short(describe_call(approval["tool"], approval["args"]), 300),
                    "priority": 2, "click": self.link(f"/#/s/{sid}")}

        if event["type"] == "run_finished":
            status = d["status"]
            if status == "done":
                head, tags, prio = "Done", ["white_check_mark"], 3
                if d.get("stop_reason", "").startswith("budget"):
                    head, tags = "Stopped (budget)", ["hourglass"]
            elif status == "failed":
                head, tags, prio = "Failed", ["x"], 4
            else:
                return None  # cancelled by the user: they already know
            body = d.get("answer") or d.get("stop_reason") or status
            return {**base, "title": f"{head}: {title}", "message": _short(body, 400), "priority": prio,
                    "tags": tags, "click": self.link(f"/#/s/{sid}")}
        return None
