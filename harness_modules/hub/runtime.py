"""Hub metrics use the same state projection as the owner inventory."""
import time
from collections import Counter

from harness.modules import ModuleRuntime
from .service import STATES, apps


class HubRuntime(ModuleRuntime):
    def metrics(self, out, db):
        rows = apps(self.manager.db, time.time()) if self.effective() else []
        counts = Counter(row["state"] for row in rows)
        out.metric("harness_hub_apps", "gauge", "Paired keys by connection state.",
                   [({"state": state}, counts[state]) for state in STATES])
        out.metric("harness_hub_app_errors", "gauge", "Paired keys with a recorded error in the last 24 hours.",
                   [({}, sum(row["errors"] for row in rows))])
