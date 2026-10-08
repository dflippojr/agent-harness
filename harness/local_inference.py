"""Core inference lifecycle vocabulary and the stand-in when supervision is absent.

The local backend can call a separately managed server without supervising or probing it.
"""

SLEEPING, WAKING, READY, UNREACHABLE, PAUSED = "sleeping", "waking", "ready", "unreachable", "paused"
UNLOADED, LOW_MEMORY = "unloaded", "low_memory"
EXPECTED_WAKE_SECONDS = 60


class NoWarmer:
    """An externally managed model needs no warm-up before an inference call."""

    blocked = staticmethod(lambda: False)

    async def state(self, model):
        return READY

    async def ensure_loaded(self, model):
        pass

    def parked(self, model):
        return False

    def waking_for(self, model):
        return None
