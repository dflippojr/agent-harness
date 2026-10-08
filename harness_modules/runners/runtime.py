"""Remote transport and native pairing lifecycle."""
import os
import secrets
from pathlib import Path

from harness.modules import ModuleRuntime, HarnessError
from .service import RunnerHub


class RunnersRuntime(ModuleRuntime):
    def init(self):
        if self.effective():
            self.service = RunnerHub(self.cfg.runners, keep_awake=self.manager._keep_awake)
            self.manager.hub = self.service
            self.manager.runner.hub = self.service

    async def stop(self):
        if self.service is not None:
            self.service.close()

    def features(self):
        return {"runner_pairing": bool(self.service and self.cfg.runners)}

    # owner-approved Agent Harness for Mac pairing (issue #16)
    def _runner_token(self, name: str, create: bool = False) -> str:
        runner = self.manager.cfg.runners.get(name)
        if runner is None:
            raise HarnessError(404, f"unknown runner {name!r}")
        if not runner.token_file:
            raise HarnessError(400, f"runner {name!r} has no token_file configured")
        path = Path(runner.token_file).expanduser()
        try:
            token = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
            if not token and create:
                path.parent.mkdir(parents=True, exist_ok=True)
                token = secrets.token_urlsafe(32)
                path.write_text(token + "\n", encoding="utf-8")
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
        except OSError as exc:
            raise HarnessError(500, f"runner token file is unavailable: {exc}") from exc
        if not token:
            raise HarnessError(500, f"runner token file for {name!r} is empty")
        return token

    def create_runner_pairing_code(self, name: str, runner: str, ttl_seconds: int) -> tuple[dict, str]:
        self._runner_token(runner, create=True)
        return self.manager.db.create_runner_pairing_code(name, runner, ttl_seconds)

    def redeem_runner_pairing_code(self, code: str, request_base_url: str) -> tuple[dict | None, str]:
        pairing, key, owner_token, error = self.manager.db.redeem_runner_pairing_code(code)
        if pairing is None or key is None:
            return None, error
        name = pairing["runner"]
        runner = self.manager.cfg.runners.get(name)
        if runner is None:
            return None, "paired runner is no longer configured"
        runner_token = self._runner_token(name, create=True)
        server = self.manager.cfg.public_url or request_base_url.rstrip("/")
        return {
            "server": server,
            "owner_token": owner_token,
            "owner_key": key,
            "runner": {
                "server": server,
                "name": name,
                "token": runner_token,
                "repo_roots": ["~/Projects"],
                "min_free_gb": runner.min_free_gb,
            },
        }, ""

