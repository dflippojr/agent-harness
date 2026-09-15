"""Homelab tools: narrow, allowlisted actions the daemon runs on the agent's behalf.

Agents never get the Docker socket or a host shell. They can look at allowlisted services (state, logs, config
files, Prometheus) and ask to restart one; the restart always goes through an approval (see policy.py).
Environment variables and mounts are left out of what they see, since those hold secrets.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import time
from pathlib import Path

import httpx

from .config import HomelabConfig
from .sandbox import run_cmd
from .tools import ToolError

TOOLS = ("homelab_services", "container_logs", "read_service_config", "prometheus_query", "restart_service",
         "rebuild_service")


class HomelabError(ToolError):
    pass


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required or []},
    }}


def schemas(cfg: HomelabConfig) -> list[dict]:
    names = ", ".join(cfg.services) or "none"
    service = {"type": "string", "description": f"One of: {names}"}
    return [
        _fn("homelab_services", "Show every homelab service the agent may manage: container state, health, exit "
                                "code, restart count, start/finish times, and its compose stack.", {}),
        _fn("container_logs", "Read a service container's recent logs (stdout and stderr, with timestamps).", {
            "service": service,
            "tail": {"type": "integer", "description": "Last N lines. Default 200, max 2000."},
            "since": {"type": "string", "description": "Only logs newer than this, e.g. '30m', '2h', or an "
                                                       "RFC 3339 time."},
        }, ["service"]),
        _fn("read_service_config", "Read a config file (or list a directory) of a homelab stack. Paths are relative "
                                   f"to {cfg.docker_root}, e.g. 'plex-webhook/docker-compose.yml'. Secrets and data "
                                   "directories are not readable.", {
            "path": {"type": "string"},
        }, ["path"]),
        _fn("prometheus_query", "Run a PromQL query against the homelab Prometheus. Without range_minutes it's an "
                                "instant query; with it, a range query ending now.", {
            "query": {"type": "string"},
            "range_minutes": {"type": "integer", "description": "Look back this many minutes (max 10080)."},
            "step_seconds": {"type": "integer", "description": "Range step. Default: about 60 points."},
        }, ["query"]),
        _fn("restart_service", "Restart a homelab service (starts it if it's stopped, recreates it with docker "
                               "compose if the container is gone). Always needs the user's approval.", {
            "service": service,
        }, ["service"]),
        _fn("rebuild_service", "Rebuild a homelab service's image from its stack directory and recreate the "
                               "container (docker compose up -d --build). Use it after a code or Dockerfile change "
                               "was merged into the stack. Always needs the user's approval.", {
            "service": service,
        }, ["service"]),
    ]


class Homelab:
    tool_names = TOOLS

    def __init__(self, cfg: HomelabConfig):
        self.cfg = cfg

    def _service(self, name: str):
        svc = self.cfg.services.get(name)
        if svc is None:
            raise HomelabError(f"unknown service {name!r}; allowed: {', '.join(self.cfg.services) or 'none'}")
        return svc

    async def homelab_services(self) -> str:
        if not self.cfg.services:
            return "No services are allowlisted."
        containers = [s.container or s.name for s in self.cfg.services.values()]
        code, out, err = await run_cmd(["docker", "inspect", *containers], timeout=60)
        found = {}
        try:
            for item in json.loads(out or "[]"):
                found[item["Name"].lstrip("/")] = item
        except ValueError:
            raise HomelabError(f"docker inspect failed: {(err or out).strip()[:300]}")
        lines = []
        for svc in self.cfg.services.values():
            item = found.get(svc.container or svc.name)
            if item is None:
                lines.append(f"- {svc.name}: container missing (stack {svc.stack})")
                continue
            st = item["State"]
            health = (st.get("Health") or {}).get("Status")
            policy = (item.get("HostConfig") or {}).get("RestartPolicy", {}).get("Name", "")
            parts = [st.get("Status", "?")]
            if health:
                parts.append(f"health {health}")
            if st.get("Status") != "running" or st.get("ExitCode"):
                parts.append(f"exit code {st.get('ExitCode')}")
            if st.get("OOMKilled"):
                parts.append("OOM-killed")
            if st.get("Error"):
                parts.append(f"error: {st['Error']}")
            parts += [f"started {st.get('StartedAt', '')[:19]}", f"finished {st.get('FinishedAt', '')[:19]}",
                      f"restarts {item.get('RestartCount', 0)}", f"restart policy {policy or 'no'}",
                      f"image {item.get('Config', {}).get('Image', '')}", f"stack {svc.stack}"]
            lines.append(f"- {svc.name}: " + ", ".join(parts))
        return "\n".join(lines)

    async def container_logs(self, service: str, tail: int = 200, since: str = "") -> str:
        svc = self._service(service)
        tail = max(1, min(int(tail), 2000))
        args = ["docker", "logs", "--timestamps", "--tail", str(tail)]
        if since:
            if not re.fullmatch(r"\d+[smhd]|\d{4}-\d{2}-\d{2}[T ][\d:.]+(Z|[+-]\d{2}:?\d{2})?", since.strip()):
                raise HomelabError("since must look like 30m, 2h, 1d, or an RFC 3339 time")
            args += ["--since", since.strip()]
        code, out, err = await run_cmd(args + [svc.container or svc.name], timeout=60)
        if code != 0 and not out:
            raise HomelabError(f"docker logs failed: {err.strip()[:500]}")
        # docker logs writes the container's stderr to stderr; interleave by timestamp.
        lines = sorted((out + err).splitlines())
        return "\n".join(lines) if lines else "(no log lines)"

    def read_service_config(self, path: str) -> str:
        rel = path.strip().replace("\\", "/").lstrip("/")
        root = Path(self.cfg.docker_root).resolve()
        target = (root / rel).resolve()
        if target != root and not target.is_relative_to(root):
            raise HomelabError(f"path escapes {self.cfg.docker_root}: {path}")
        rel = target.relative_to(root).as_posix() if target != root else "."
        stacks = {s.stack for s in self.cfg.services.values()}
        if rel != "." and rel.split("/", 1)[0] not in stacks:
            raise HomelabError(f"not a managed stack: {rel.split('/', 1)[0]}; stacks: {', '.join(sorted(stacks))}")
        if self._denied(rel):
            raise HomelabError(f"{rel} is not readable (secrets and data are off limits)")
        if target.is_dir():
            if rel == ".":
                return "\n".join(sorted(f"{s}/" for s in stacks))
            entries = []
            for child in sorted(target.iterdir()):
                child_rel = f"{rel}/{child.name}"
                if not self._denied(child_rel) and child.name != ".git":
                    entries.append(child.name + ("/" if child.is_dir() else ""))
            return "\n".join(entries) or "(empty directory)"
        if not target.is_file():
            raise HomelabError(f"no such file: {rel}")
        if target.stat().st_size > 200_000:
            raise HomelabError(f"{rel} is too large to read ({target.stat().st_size} bytes)")
        return target.read_text(encoding="utf-8", errors="replace")

    def _denied(self, rel: str) -> bool:
        return any(fnmatch.fnmatch(rel.lower(), g.lower()) for g in self.cfg.deny)

    async def prometheus_query(self, query: str, range_minutes: int | None = None,
                               step_seconds: int | None = None) -> str:
        base = self.cfg.prometheus_url.rstrip("/")
        async with httpx.AsyncClient(timeout=30) as client:
            if range_minutes:
                minutes = max(1, min(int(range_minutes), 10080))
                end = time.time()
                step = int(step_seconds) if step_seconds else max(15, minutes * 60 // 60)
                resp = await client.get(f"{base}/api/v1/query_range", params={
                    "query": query, "start": end - minutes * 60, "end": end, "step": step})
            else:
                resp = await client.get(f"{base}/api/v1/query", params={"query": query})
        try:
            body = resp.json()
        except ValueError:
            raise HomelabError(f"Prometheus answered HTTP {resp.status_code}: {resp.text[:300]}")
        if body.get("status") != "success":
            raise HomelabError(f"Prometheus error: {body.get('error', resp.text[:300])}")
        return format_prometheus(body["data"])

    async def restart_service(self, service: str) -> str:
        svc = self._service(service)
        container = svc.container or svc.name
        code, out, _ = await run_cmd(["docker", "inspect", "-f", "{{.State.Status}}", container], timeout=30)
        if code == 0:
            code, out, err = await run_cmd(["docker", "restart", "-t", "20", container], timeout=180)
            action = "restarted"
        else:
            stack = Path(self.cfg.docker_root) / svc.stack
            code, out, err = await run_cmd(["docker", "compose", "--project-directory", str(stack), "up", "-d",
                                            "--no-build", svc.service or svc.name], timeout=600)
            action = "recreated with docker compose"
        if code != 0:
            raise HomelabError(f"restart failed: {(err or out).strip()[:800]}")
        _, state, _ = await run_cmd(["docker", "inspect", "-f", "{{.State.Status}} since {{.State.StartedAt}}",
                                     container], timeout=30)
        return f"{service} {action}; now {state.strip()}"

    async def rebuild_service(self, service: str) -> str:
        svc = self._service(service)
        stack = Path(self.cfg.docker_root) / svc.stack
        code, out, err = await run_cmd(["docker", "compose", "--project-directory", str(stack), "up", "-d", "--build",
                                        svc.service or svc.name], timeout=1800)
        log = (out + err).strip()
        if code != 0:
            raise HomelabError(f"rebuild failed (exit {code}):\n{log[-3000:]}")
        _, state, _ = await run_cmd(["docker", "inspect", "-f", "{{.State.Status}} since {{.State.StartedAt}}, image "
                                     "{{.Image}}", svc.container or svc.name], timeout=30)
        return f"{service} rebuilt and recreated; now {state.strip()}\n{log[-1500:]}"

    async def call(self, name: str, args: dict) -> str:
        if name == "read_service_config":
            return await asyncio.to_thread(self.read_service_config, **args)
        return await getattr(self, name)(**args)


def format_prometheus(data: dict, limit: int = 60) -> str:
    kind, result = data.get("resultType"), data.get("result")
    if kind in ("scalar", "string"):
        return f"{kind}: {result[1]}"
    if not result:
        return "no data"

    def labels(metric: dict) -> str:
        name = metric.get("__name__", "")
        rest = ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()) if k != "__name__")
        return f"{name}{{{rest}}}" if rest else name or "{}"

    lines = []
    for series in result[:limit]:
        if kind == "vector":
            lines.append(f"{labels(series['metric'])} {series['value'][1]}")
        else:
            values = series.get("values", [])
            nums = [float(v[1]) for v in values if v[1] not in ("NaN", "+Inf", "-Inf")]
            summary = (f"min {min(nums):.4g}, max {max(nums):.4g}, last {values[-1][1]}" if nums else "no numbers")
            points = values if len(values) <= 12 else values[:: max(1, len(values) // 12)]
            lines.append(f"{labels(series['metric'])}: {len(values)} points, {summary}; samples "
                         + ", ".join(f"{int(float(t))}={v}" for t, v in points))
    if len(result) > limit:
        lines.append(f"... {len(result) - limit} more series")
    return "\n".join(lines)
