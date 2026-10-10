"""The disk watchdog for a running sandbox command (#525): what it may write, and the polling that says when it has
passed a limit. Nothing here knows about Docker; the sandbox stops the container when the watchdog says so."""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .fileops import ToolError, dir_size


@dataclass(frozen=True)
class AccountLimit:
    """A member's account quota, and how to measure the whole account now (#525)."""
    quota_bytes: int
    usage: Callable[[], int]


@dataclass(frozen=True)
class DiskLimits:
    """What a command may write (#525). `quota_bytes` caps the workspace; `min_free_bytes` is kept free on its drive.
    For a member, `account` caps the whole account as measured while the command runs, so commands running at once in
    several of its sessions share one budget instead of each getting all of what is left."""
    quota_bytes: int
    min_free_bytes: int
    account: AccountLimit | None = None


class DiskLimitExceeded(ToolError):
    """The disk watchdog stopped a command that wrote past the workspace quota or the data drive's free-space floor."""


FREE_POLL_SECONDS = 0.25         # free space is one cheap syscall, so it is polled often
MIN_SCAN_SECONDS = 1.0           # a full workspace scan runs at least this far apart...
SCAN_DUTY = 4                    # ...and at least this many times its own duration apart, so big trees cost little
FREE_SLACK_BYTES = 256 * 2**20   # a command that starts under the free-space floor may still use this much (or half)


class DiskWatch:
    """Watch one running command's workspace and say when it passes its limits (#525).

    Free space on the workspace drive is polled every FREE_POLL_SECONDS. Whatever the workspace grows also comes off
    that free space, so the size at the last scan plus the free space lost since bounds the size now: a full scan runs
    as soon as that bound passes the cap, and on an adaptive interval otherwise (sparse files, writes the drive does
    not see). Scans run in a thread while free space is still polled, and once more when the command ends. Shrinking
    is always allowed, so a workspace already over its quota can still be cleaned up."""

    def __init__(self, root: Path, limits: DiskLimits):
        self.root = root
        self.limits = limits
        self.account = limits.account
        self.cap = self.account_cap = 0
        self.floor = 0
        self._size = self._account_used = self._free_at_scan = 0
        self._scanned = self._scan_cost = 0.0

    def _free(self) -> int:
        return shutil.disk_usage(self.root).free

    def _scan(self) -> tuple[int, int]:
        """Measure the workspace (and the account): (size, account usage). Blocking: run it in a thread."""
        began = time.monotonic()
        free = self._free()     # before the walk, so what is written during it counts as space lost since the scan
        size = dir_size(self.root)
        used = self.account.usage() if self.account else 0
        self._size, self._account_used, self._free_at_scan = size, used, free
        self._scanned = time.monotonic()
        self._scan_cost = self._scanned - began
        return size, used

    def start(self) -> None:
        """Measure the starting point. Blocking: run it in a thread."""
        self._scan()
        self.cap = max(self.limits.quota_bytes, self._size)
        if self.account:
            self.account_cap = max(self.account.quota_bytes, self._account_used)
        floor, free = self.limits.min_free_bytes, self._free_at_scan
        self.floor = floor if free >= floor else free - min(FREE_SLACK_BYTES, free // 2)

    def _floor_reason(self, free: int) -> str:
        if free < self.floor:
            return (f"the data drive is down to {free / 2**30:.1f} GB free, under its "
                    f"{self.limits.min_free_bytes / 2**30:.1f} GB minimum")
        return ""

    def _may_be_over(self, free: int) -> bool:
        lost = max(0, self._free_at_scan - free)
        return self._size + lost > self.cap or (self.account and self._account_used + lost > self.account_cap)

    def _scan_reason(self) -> str:
        """Scan, then '' while within the quotas, else why the command must stop. Blocking: run it in a thread."""
        size, used = self._scan()   # its own results: a cancelled scan still running in another thread may also write
        if size > self.cap:
            return (f"the workspace grew to {size / 2**20:.0f} MB, past its "
                    f"{self.limits.quota_bytes / 2**20:.0f} MB quota")
        if self.account and used > self.account_cap:
            return (f"the account grew to {used / 2**20:.0f} MB, past its "
                    f"{self.account.quota_bytes / 2**20:.0f} MB disk quota")
        return ""

    async def _scan_polling(self) -> str:
        """Scan in a thread and keep polling the free-space floor until it finishes, so a big tree never leaves the
        drive unwatched. '' or why the command must stop."""
        scan = asyncio.ensure_future(asyncio.to_thread(self._scan_reason))
        try:
            while True:
                done, _ = await asyncio.wait({scan}, timeout=FREE_POLL_SECONDS)
                reason = (scan.result() if done else "") or self._floor_reason(await asyncio.to_thread(self._free))
                if reason or done:
                    return reason
        finally:
            scan.cancel()

    async def run(self) -> str:
        """Poll until a limit is passed and return why. The caller cancels it when the command ends first."""
        while True:
            await asyncio.sleep(FREE_POLL_SECONDS)
            free = await asyncio.to_thread(self._free)
            since = time.monotonic() - self._scanned
            due = self._may_be_over(free) or since >= max(MIN_SCAN_SECONDS, SCAN_DUTY * self._scan_cost)
            reason = self._floor_reason(free) or (await self._scan_polling() if due else "")
            if reason:
                return reason

    async def final(self) -> str:
        """One last look once the command has ended, so a burst between two polls is still caught. Always a full scan:
        free space cannot rule growth out (other sessions may free space at the same time), and the command has just
        warmed the tree, so the walk is cheap."""
        return self._floor_reason(await asyncio.to_thread(self._free)) or await self._scan_polling()
