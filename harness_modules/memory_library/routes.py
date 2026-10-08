"""Owner memory management, also exposed through the admin API."""
import asyncio
from fastapi import Request
from pydantic import BaseModel
from harness.modules import HarnessError, RouteTable, ToolError, manager

owner_routes = RouteTable()

class MemoryProfileUpdate(BaseModel):
    content: str
    summary: str = "Update agent profile"


@owner_routes.get("/memory")
async def memory(request: Request):
    """The agent profile new sessions get, and the latest change agents saved to the memory library."""
    m = manager(request)
    lib, cfg = m.memory_library, m.cfg.memory_library
    if lib is None:
        return {"enabled": False}
    profile = await asyncio.to_thread(lib.profile_text)
    return {"enabled": True, "writes": cfg.writes, "categories": cfg.categories, "profile_path": cfg.profile_path,
            "profile": profile, "profile_chars": len(profile), "profile_max_chars": cfg.profile_max_chars,
            "last_commit": lib.last_commit, "refresh_error": lib.refresh_error,
            "refresh_state": lib.refresh_state, "changed_paths": lib.changed_paths,
            "refresh_failures": lib.failures, "last_success": lib.last_success}


@owner_routes.put("/memory/profile")
async def update_memory_profile(body: MemoryProfileUpdate, request: Request):
    """Owner edit of the agent profile from Settings. Commits and pushes like an approved memory write."""
    m = manager(request)
    lib, cfg = m.memory_library, m.cfg.memory_library
    if lib is None:
        raise HarnessError(400, "the memory library is disabled in config/harness.yaml")
    if not cfg.profile_path:
        raise HarnessError(400, "memory_library.profile_path is not set")
    try:
        saved = await lib.owner_write(cfg.profile_path, body.content, body.summary)
    except ToolError as e:
        raise HarnessError(400, str(e))
    profile = await asyncio.to_thread(lib.profile_text)
    return {"profile": profile, "profile_chars": len(profile), "profile_max_chars": cfg.profile_max_chars,
            "last_commit": saved}
