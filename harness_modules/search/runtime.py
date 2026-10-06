"""Session search inside a running daemon: the ModuleRuntime the core drives (harness/modules.py)."""

from __future__ import annotations

from harness.modules import ModuleRuntime

from .service import SessionSearch


class SearchRuntime(ModuleRuntime):
    def init(self) -> None:
        if self.effective("search"):
            self.service = SessionSearch(self.manager.db)

    def toolkit(self):
        return self.service
