"""Owner-file-only configuration for pinned stdio MCP servers (#260)."""

import re


def validate_servers(raw, owner_id="owner") -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("mcp_servers must be a list")
    if raw and owner_id != "owner":
        raise ValueError("member projects cannot configure mcp_servers")
    names = set()
    for server in raw:
        if not isinstance(server, dict):
            raise ValueError("each MCP server must be a mapping")
        if "url" in server:
            raise ValueError("remote HTTP MCP servers are not supported in v1")
        if set(server) - {"name", "image", "command", "env", "mount_workspace", "rules"}:
            raise ValueError("unknown MCP server configuration key")
        name = server.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]+", name) or name == "harness":
            raise ValueError("MCP name must be [a-z0-9_]+; harness is reserved")
        if name in names:
            raise ValueError("MCP server names must be unique per project")
        names.add(name)
        if not re.fullmatch(r"[^\s@]+@sha256:[a-fA-F0-9]{64}", str(server.get("image", ""))):
            raise ValueError("MCP image must be pinned by digest (repo@sha256:64 hex digits)")
        command = server.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(a, str) or "\0" in a for a in command):
            raise ValueError("MCP command must be a nonempty argv list")
        mount = server.get("mount_workspace", False)
        if mount is not False and mount not in ("ro", "rw"):
            raise ValueError("MCP mount_workspace must be false, ro or rw")
        env = server.get("env", {})
        if not isinstance(env, dict) or any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k)) or not isinstance(v, dict)
            or set(v) != {"secret_file"} or not isinstance(v["secret_file"], str) or not v["secret_file"]
            for k, v in env.items()
        ):
            raise ValueError("MCP env values must be {secret_file: path} references")
        rules = server.get("rules", [])
        if not isinstance(rules, list) or any(
            not isinstance(r, dict) or r.get("action") not in ("allow", "ask", "deny")
            or not isinstance(r.get("tool"), str) or not r["tool"].startswith(f"mcp__{name}__")
            for r in rules
        ):
            raise ValueError("MCP rules must name this server's tools and use allow, ask or deny")
    return raw
