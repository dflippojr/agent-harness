"""Core contracts for remote session execution; implementations belong to the runners add-on."""


class RunnerOffline(Exception):
    pass


class RunnerError(Exception):
    """A remote operation failed: tool, git, restarted, timeout, or internal."""

    def __init__(self, message: str, kind: str = "internal", status: int = 409):
        super().__init__(message)
        self.kind = kind
        self.status = status


class RemoteWorkspace:
    """Marker for a workspace whose calls run remotely rather than on the tower."""


class NoRunnerHub:
    """Local-only stand-in when the runners package is absent or uninstalled."""

    def __init__(self):
        self.state = {}

    def status(self):
        return []

    def online(self, name):
        return False

    def startup_grace(self):
        return 0

    def close(self):
        pass

    async def wait_online(self, name):
        raise RunnerOffline("runners module is unavailable")

    async def call(self, name, op, params, **kwargs):
        raise RunnerOffline("runners module is unavailable")

    def sandbox(self, target, sid):
        raise RunnerOffline("runners module is unavailable")

    def workspace(self, target, sid, context_tokens, **kwargs):
        raise RunnerOffline("runners module is unavailable")
