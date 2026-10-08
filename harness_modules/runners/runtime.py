"""Remote transport and native pairing lifecycle."""
import os
import secrets
from pathlib import Path

from harness.modules import ModuleRuntime, HarnessError, credential_audit
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

    def create_runner_pairing_code(self, name: str, runner: str, ttl_seconds: int, ctx=None) -> tuple[dict, str]:
        self._runner_token(runner, create=True)
        db = self.manager.db

        def commit():
            row, code = db.main.create_runner_pairing_code(name, runner, ttl_seconds)
            credential_audit.record(db, ctx or credential_audit.unknown_context("legacy_api"),
                                    "runner_pairing.create", row["id"], "ok", "pairing", {"pairing_id": row["id"]})
            return row, code
        return db.main.write(commit)

    def revoke_runner_pairing_code(self, pid: str, ctx) -> bool:
        db = self.manager.db

        def commit() -> bool:
            ok = db.main.revoke_runner_pairing_code(pid)
            credential_audit.record(db, ctx, "runner_pairing.revoke", pid if ok else "", "ok" if ok else "noop",
                                    "pairing", {"pairing_id": pid} if ok else {"reason": "not_found"})
            return ok
        return db.main.write(commit)

    def redeem_runner_pairing_code(self, code: str, request_base_url: str) -> tuple[dict | None, str]:
        db = self.manager.db

        class _Refused(Exception):
            pass

        def commit():
            pairing, key, owner_token, error = db.main.redeem_runner_pairing_code(code)
            if pairing is None or key is None:
                credential_audit.record(db, credential_audit.unknown_context(), "runner_pairing.redeem", "",
                                        "denied", "pairing", {"reason": credential_audit.pairing_reason(error)})
                return None, None, "", error
            if self.manager.cfg.runners.get(pairing["runner"]) is None:
                raise _Refused  # undo the minted key: nothing was handed out
            runner_token = self._runner_token(pairing["runner"], create=True)
            credential_audit.record(db, credential_audit.device_context(key["id"], "owner", "app_api"),
                                    "runner_pairing.redeem", key["id"], "ok", "api_key",
                                    {"key_id": key["id"], "pairing_id": pairing["id"], "kind": "owner"})
            return pairing, key, owner_token, runner_token
        try:
            pairing, key, owner_token, runner_token = db.main.write(commit)
        except _Refused:
            credential_audit.record(db, credential_audit.unknown_context(), "runner_pairing.redeem", "", "denied",
                                    "pairing", {"reason": "runner_unavailable"})
            return None, "paired runner is no longer configured"
        if pairing is None:
            return None, runner_token  # the refusal text
        name = pairing["runner"]
        runner = self.manager.cfg.runners[name]
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
