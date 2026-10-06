"""Session lifecycle; no container is started for hosted, member or tools-only sessions."""

from harness.modules import ModuleRuntime, validate_mcp_servers

from .service import SessionTools


class McpRuntime(ModuleRuntime):
    def __init__(self, manager, module):
        super().__init__(manager, module)
        self.sessions = {}

    async def prepare_session(self, session):
        if not self.effective() or session["id"] in self.sessions:
            return
        if (session.get("backend", "local") != "local" or session.get("kind", "agent") != "agent"
                or session.get("owner_id", "owner") != "owner" or session.get("target", "tower") != "tower"):
            return
        project = self.manager.runner.project_for(session)
        if project is None or project.owner_id != "owner" or not project.mcp_servers:
            return
        servers = validate_mcp_servers(project.mcp_servers, project.owner_id)
        kit = SessionTools(session, servers, self.manager.runner.sandbox(session).cfg)
        self.sessions[session["id"]] = kit
        try:
            await kit.start()
        except BaseException:
            await self.end_session(session["id"])
            raise

    def session_toolkit(self, session):
        return self.sessions.get(session["id"]) if self.effective() else None

    def owns_toolkit(self, kit):
        return any(value is kit for value in self.sessions.values())

    async def end_session(self, sid):
        kit = self.sessions.pop(sid, None)
        if kit is not None:
            await kit.close()

    async def stop(self):
        for sid in list(self.sessions):
            await self.end_session(sid)
