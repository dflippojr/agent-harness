"""Owner-approved instruction skills add-on (#334 stage c)."""
from __future__ import annotations
from harness.modules import Module, ToolGate

def _runtime(manager, module):
    from .runtime import SkillsRuntime
    return SkillsRuntime(manager, module)

def _routes():
    from .routes import owner_routes
    return owner_routes

def _settings():
    from .settings import specs
    return specs()

def _enabled(cfg, switch):
    return bool(cfg.skills.enabled)

def _eligible(kit, session):
    return kit.can_propose(session)

MODULE = Module(
    name="skills", switches=("skills",), title="Instruction skills", docs=("docs/modules.md",),
    runtime=_runtime, runtime_enabled=_enabled, owner_routes=_routes, settings=_settings,
    tool_names=("propose_skill",),
    tools=ToolGate(project_flag="", capability="", mcp=False, eligible=_eligible),
    admin_paths=frozenset({
        "/skills",
        "/skills/enabled",
        "/skills/proposals/{pid}",
        "/skills/proposals/{pid}/install",
        "/skills/proposals/{pid}/reject",
        "/skills/proposals/{pid}/reopen",
        "/skills/proposals/{pid}/review",
        "/skills/{slug}/enable",
        "/skills/{slug}/disable",
        "/skills/{slug}/rollback",
        "/skills/{slug}/uninstall",
        "/skills/{slug}/projects",
        "/skills/{slug}/export",
    }),
    cli=(
    ("skills list", "GET", "/skills", "list skills and proposals", ()),
    ("skills enabled", "GET", "/skills/enabled", "list enabled skills", ()),
    ("skills proposal", "GET", "/skills/proposals/{pid}", "show a skill proposal", ()),
    ("skills install", "POST", "/skills/proposals/{pid}/install", "install a proposal at its reviewed hash",
     ("content_hash",)),
    ("skills reject", "POST", "/skills/proposals/{pid}/reject", "reject a proposal", ("--reason",)),
    ("skills reopen", "POST", "/skills/proposals/{pid}/reopen", "reopen a rejected proposal", ()),
    ("skills review", "POST", "/skills/proposals/{pid}/review", "run the hosted review of a proposal", ()),
    ("skills delete-proposal", "DELETE", "/skills/proposals/{pid}", "delete a proposal", ()),
    ("skills enable", "POST", "/skills/{slug}/enable", "enable a skill", ()),
    ("skills disable", "POST", "/skills/{slug}/disable", "disable a skill", ()),
    ("skills rollback", "POST", "/skills/{slug}/rollback", "roll a skill back to its previous version", ()),
    ("skills uninstall", "POST", "/skills/{slug}/uninstall", "uninstall a skill", ()),
    ("skills projects", "PUT", "/skills/{slug}/projects", "set the projects that may use a skill", ("projects:list",)),
    ("skills export", "GET", "/skills/{slug}/export", "export a skill", ()),
    ),
    cli_groups={"skills": "instruction skills"},
)
