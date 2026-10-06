"""Homelab workspace tools and project guidance."""
from harness.modules import ModuleRuntime, app_allows
from .service import Homelab

HOMELAB_PROMPT = """Homelab access: you can inspect the allowlisted services on this server with homelab_services, container_logs, read_service_config, and prometheus_query, ask to restart one with restart_service, and, after a code or Dockerfile change has been merged into a stack, ask to rebuild it with rebuild_service (the user approves restarts and rebuilds). These run on the host; the Linux sandbox can't reach Docker or the services. Diagnose from state and logs before proposing a restart, and afterwards check that the service stayed up."""


class HomelabRuntime(ModuleRuntime):
    def init(self):
        if self.effective():
            self.service = Homelab(self.cfg.homelab)

    def workspace_toolkit(self, project, defaults, member):
        if project and project.homelab and not member and app_allows(defaults, "homelab"):
            return self.service
        return None

    def project_prompt(self, spec, defaults) -> str:
        if self.service is None or not spec.homelab or not app_allows(defaults, "homelab"):
            return ""
        extra = "\n\n" + HOMELAB_PROMPT
        if not spec.repo:
            repos = [p.name for p in self.cfg.projects.values() if p.repo and p.target == "tower"]
            extra += ("\n\nThis project has no repository, so you can't change files on the server (the workspace "
                      "is an empty scratch directory the services never see). If the fix needs a code or config "
                      "change, don't look for a way around that: finish with the diagnosis, the exact change, and "
                      "which project to run it in" + (f" ({', '.join(repos)})" if repos else "") + ".")
        return extra

