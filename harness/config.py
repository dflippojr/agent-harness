"""Daemon configuration: config/harness.yaml plus config/projects.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ModelConfig:
    name: str
    base_url: str
    context_tokens: int = 32768
    max_tokens: int = 8192
    sampling: dict = field(default_factory=dict)


@dataclass
class SandboxConfig:
    image: str = "agent-harness-sandbox:py312"
    memory: str = "2g"
    cpus: str = "2"
    pids: int = 512
    network: str = "harness-sandbox"
    egress_network: str = "harness-egress"


@dataclass
class BackendConfig:
    """A hosted CLI used as a session backend (Phase 8a)."""
    enabled: bool = False
    image: str = "agent-harness-cli:1"
    model: str = "claude-opus-5"
    effort: str = "high"
    permission_mode: str = "default"
    max_sessions: int = 2
    billing: str = "subscription"
    auth: str = "subscription"
    proxy: str = "http://harness-egress-claude:8888"
    volume: str = "harness-auth-claude"
    network: str = "harness-cli-claude"


@dataclass
class NotifyConfig:
    enabled: bool = False
    server: str = "http://127.0.0.1:8095"  # where the daemon publishes (local)
    topic: str = "agent-harness"
    token_file: str = ""                   # file holding an ntfy access token with write access to the topic


@dataclass
class HomelabService:
    name: str
    stack: str             # compose project directory under homelab.docker_root
    container: str = ""    # defaults to the name
    service: str = ""      # compose service; defaults to the name


@dataclass
class HomelabConfig:
    docker_root: str = "D:/Docker"
    prometheus_url: str = "http://127.0.0.1:9090"
    services: dict[str, HomelabService] = field(default_factory=dict)
    # Never readable through read_service_config, matched against the path relative to docker_root.
    deny: list[str] = field(default_factory=lambda: [
        "*/secrets", "*/secrets/*", "*/data", "*/data/*", "*/.git", "*/.git/*", "*.env", "*/.env*", "*.token",
        "*.key", "*.pem", "*password*", "*credential*"])


@dataclass
class CleanupConfig:
    interval_minutes: int = 60
    container_idle_hours: float = 24      # remove a finished session's stopped container after this long
    workspace_retention_days: float = 14  # delete a finished session's workspace after this long
    workspace_quota_mb: int = 5000        # per-session workspace limit (projects can override with quota_mb)
    min_free_gb: float = 20               # refuse new sessions when the data drive has less free space


@dataclass
class GpuGuardConfig:
    """Pause the queue and unload the model while a game or a Plex hardware transcode needs the GPU (gpu_guard.py)."""
    enabled: bool = False
    poll_seconds: float = 10
    resume_after_seconds: float = 180     # the GPU must stay clear this long before the model is reloaded
    drain_timeout_seconds: float = 300    # longest wait for the current model turn before the server is stopped
    # The model server's supervisor (ops/llama-server/run-qwen.ps1) doesn't restart the server while this file exists.
    pause_flag: str = "C:/AI/llama-server.paused"
    # A running executable whose path contains one of these (case-insensitive) is a game ...
    game_dirs: list[str] = field(default_factory=lambda: [
        "\\steamapps\\common\\", "\\Epic Games\\", "\\GOG Galaxy\\Games\\", "\\XboxGames\\"])
    # ... unless its path also contains one of these (tools that live in game folders).
    ignore_paths: list[str] = field(default_factory=lambda: ["\\wallpaper_engine\\", "\\Steamworks Shared\\"])
    game_processes: list[str] = field(default_factory=list)  # extra executable names, e.g. [Game.exe]
    # A visible window with one of these titles (Sunshine's "Steam Big Picture" app) also counts.
    window_titles: list[str] = field(default_factory=lambda: ["Steam Big Picture Mode"])
    plex_url: str = "http://127.0.0.1:32400"  # hardware transcodes; the token is read from Plex's registry key
    plex: bool = True


@dataclass
class BackupConfig:
    """Nightly copy of the session database and transcripts (maintenance.py)."""
    enabled: bool = False
    dir: str = "D:/My Backups/agent-harness"
    at: str = "03:30"        # local time
    keep_days: int = 14


@dataclass
class MemoryLibraryConfig:
    """The user's memory library for agents (memory_library.py): reads, approved writes, and the agent profile."""
    enabled: bool = False
    repo: str = ""                      # git URL or path; the daemon keeps its own clone
    clone_dir: str = "D:/Agents/memory-library"
    refresh_minutes: float = 10         # `git pull` at most this often, when an agent uses the tools
    categories: list[str] = field(default_factory=list)  # readable categories; everything else is invisible
    writes: bool = False                # memory_edit / memory_write: every change needs approval, then commit + push
    profile_path: str = ""              # e.g. agent-profile.md: put in every new session's prompt; readable/writable
    profile_max_chars: int = 6000       # about 1.5K tokens


@dataclass
class WebConfig:
    """web_search / web_fetch for agents (web_tools.py). Both run in the daemon; the sandbox stays offline."""
    enabled: bool = False
    searxng_url: str = "http://127.0.0.1:8888"
    page_chars: int = 15000          # characters per web_fetch call (~4K tokens)
    max_bytes: int = 5 * 2**20       # refuse larger downloads
    max_document_bytes: int = 25 * 2**20  # PDFs and Word documents
    fixture_dir: str = ""            # replay recorded searches and pages instead of the network (web_fixture.py)
    timeout_seconds: float = 20
    user_agent: str = "agent-harness/1.0 (personal research agent)"
    quote_check: bool = True         # final answers: quotes must appear in something the agent read (grounding.py)


@dataclass
class RemoteControlConfig:
    """`remote_control` in harness.yaml."""
    enabled: bool = False
    claude_path: str = ""                 # default: `claude` on PATH (the npm shim is fine)
    spawn: str = "worktree"               # worktree | same-dir | session (see `claude remote-control --help`)
    permission_mode: str = "default"      # for sessions opened from the phone
    capacity: int = 4                     # max concurrent sessions per server
    projects: list[str] | None = None     # which projects may be launched; default: every tower project with a local repo


@dataclass
class JobsConfig:
    """Scheduled jobs (jobs.py): recurring agent tasks on cron schedules, managed from the app."""
    enabled: bool = False
    poll_seconds: float = 30


@dataclass
class SearchConfig:
    """Full-text search over past sessions (search.py): the app's search box and the session_search tools."""
    enabled: bool = False


@dataclass
class EndpointConfig:
    """OpenAI/Anthropic-compatible inference endpoint for other tools (endpoint.py)."""
    enabled: bool = False
    default_model: str = ""                 # model for unknown names; default: the harness default model
    model_aliases: dict[str, str] = field(default_factory=dict)  # fnmatch pattern -> configured model
    max_waiting: int = 4                    # endpoint requests waiting for the GPU before new ones get 429
    agent_fair_seconds: float = 90          # after an agent turn waits this long, new endpoint requests queue behind it
    request_timeout_seconds: float = 1800


@dataclass
class ImagesConfig:
    """Local image generation with ComfyUI (images.py). The language model is unloaded while jobs run."""
    enabled: bool = False
    comfy_dir: str = "C:/AI/ComfyUI"          # portable install (python_embeded + ComfyUI)
    port: int = 8188
    work_dir: str = "D:/Agents/harness/images-work"  # ComfyUI output/temp and the harness's PNGs (images/)
    log_dir: str = "D:/Agents/harness/logs"
    linger_seconds: float = 60                 # keep ComfyUI loaded this long for more jobs before restoring Qwen
    start_timeout_seconds: float = 180
    job_timeout_seconds: float = 1200


@dataclass
class RunnerConfig:
    """A machine that runs tool calls for sessions targeting it (Phase 4: the MacBook). The model stays on the tower."""
    name: str
    token_file: str = ""        # shared secret the runner sends as a bearer token
    min_free_gb: float = 10     # refuse new workspaces when the runner's disk has less free space
    workspace_quota_mb: int = 3000


@dataclass
class Project:
    name: str
    description: str = ""
    instructions: str = ""
    rules: list[dict] = field(default_factory=list)
    sandbox: dict = field(default_factory=dict)
    repo: str = ""          # local path or URL: each session works on its own branch of a clone
    base_branch: str = ""   # branch sessions start from; default: the repo's current branch
    homelab: bool = False   # give sessions the homelab tools
    quota_mb: int = 0       # workspace quota override
    target: str = "tower"   # where tools run: tower, or a runner name such as macbook (repo is then a path there)
    memory_library: bool = True  # give sessions the memory-library tools (when memory_library is enabled)
    web: bool = True             # give sessions web_search / web_fetch (when web is enabled)
    images: bool = True          # give tower sessions generate_image (when images is enabled)
    session_search: bool = True  # give sessions session_search / session_read (when search is enabled)


@dataclass
class Config:
    host: str
    port: int
    data_dir: Path
    repos_dir: Path
    default_model: str
    models: dict[str, ModelConfig]
    sandbox: SandboxConfig
    projects: dict[str, Project]
    backends: dict[str, BackendConfig] = field(default_factory=dict)
    public_url: str = ""               # how the phone reaches the daemon, e.g. https://host.tailnet.ts.net
    allowed_logins: list[str] = field(default_factory=list)  # Tailscale logins allowed through `tailscale serve`
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    homelab: HomelabConfig = field(default_factory=HomelabConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    runners: dict[str, RunnerConfig] = field(default_factory=dict)
    gpu_guard: GpuGuardConfig = field(default_factory=GpuGuardConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    memory_library: MemoryLibraryConfig = field(default_factory=MemoryLibraryConfig)
    web: WebConfig = field(default_factory=WebConfig)
    endpoint: EndpointConfig = field(default_factory=EndpointConfig)
    images: ImagesConfig = field(default_factory=ImagesConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    jobs: JobsConfig = field(default_factory=JobsConfig)
    remote_control: RemoteControlConfig = field(default_factory=RemoteControlConfig)
    max_turns: int = 80
    max_completion_tokens: int = 200000
    elide_at: float = 0.55
    summarize_at: float = 0.65
    keep_recent: float = 0.20

    @property
    def db_path(self) -> Path:
        return self.data_dir / "harness.sqlite3"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"


def load(config_dir: Path | None = None, data_dir: Path | None = None) -> Config:
    config_dir = Path(config_dir or os.environ.get("HARNESS_CONFIG_DIR") or ROOT / "config")
    raw = yaml.safe_load((config_dir / "harness.yaml").read_text(encoding="utf-8")) or {}
    # Machine-specific values (tailnet URL, logins) live in an untracked harness.local.yaml; its top-level
    # sections are merged over harness.yaml's (one level deep).
    local_file = config_dir / "harness.local.yaml"
    if local_file.exists():
        for key, value in (yaml.safe_load(local_file.read_text(encoding="utf-8")) or {}).items():
            if isinstance(value, dict) and isinstance(raw.get(key), dict):
                raw[key] = {**raw[key], **value}
            else:
                raw[key] = value
    projects_file = config_dir / "projects.yaml"
    raw_projects = (yaml.safe_load(projects_file.read_text(encoding="utf-8")) or {}) if projects_file.exists() else {}

    models = {
        name: ModelConfig(name=name, **spec) for name, spec in (raw.get("models") or {}).items()
    }
    projects = {}
    for name, spec in (raw_projects.get("projects") or {}).items():
        spec = spec or {}
        projects[name] = Project(
            name=name,
            description=spec.get("description", ""),
            instructions=spec.get("instructions", ""),
            rules=(spec.get("policy") or {}).get("rules") or [],
            sandbox=spec.get("sandbox") or {},
            repo=str(spec.get("repo") or ""),
            base_branch=str(spec.get("base_branch") or ""),
            homelab=bool(spec.get("homelab", False)),
            quota_mb=int(spec.get("quota_mb") or 0),
            target=str(spec.get("target") or "tower"),
            memory_library=bool(spec.get("memory_library", True)),
            web=bool(spec.get("web", True)),
            images=bool(spec.get("images", True)),
            session_search=bool(spec.get("session_search", True)),
        )
    if not projects:
        projects["scratch"] = Project(name="scratch", description="Empty workspace for each session.")

    raw_homelab = dict(raw.get("homelab") or {})
    services = {
        name: HomelabService(name=name, **(spec or {})) for name, spec in (raw_homelab.pop("services", None) or {}).items()
    }
    homelab = HomelabConfig(**raw_homelab, services=services)

    runners = {name: RunnerConfig(name=name, **(spec or {})) for name, spec in (raw.get("runners") or {}).items()}
    backends = {name: BackendConfig(**(spec or {})) for name, spec in (raw.get("backends") or {}).items()}
    listen = raw.get("listen") or {}
    budgets = raw.get("budgets") or {}
    compaction = raw.get("compaction") or {}
    cfg = Config(
        host=listen.get("host", "127.0.0.1"),
        port=int(listen.get("port", 8100)),
        data_dir=Path(data_dir or os.environ.get("HARNESS_DATA_DIR") or raw.get("data_dir", ROOT / "data")),
        repos_dir=Path(raw.get("repos_dir", ROOT / "data" / "repos")),
        default_model=raw.get("default_model") or next(iter(models), ""),
        models=models,
        sandbox=SandboxConfig(**(raw.get("sandbox") or {})),
        projects=projects,
        backends=backends,
        public_url=(raw.get("public_url") or "").rstrip("/"),
        allowed_logins=list(raw.get("allowed_logins") or []),
        notify=NotifyConfig(**(raw.get("notify") or {})),
        homelab=homelab,
        cleanup=CleanupConfig(**(raw.get("cleanup") or {})),
        runners=runners,
        gpu_guard=GpuGuardConfig(**(raw.get("gpu_guard") or {})),
        backup=BackupConfig(**(raw.get("backup") or {})),
        memory_library=MemoryLibraryConfig(**(raw.get("memory_library") or {})),
        web=WebConfig(**(raw.get("web") or {})),
        endpoint=EndpointConfig(**(raw.get("endpoint") or {})),
        images=ImagesConfig(**(raw.get("images") or {})),
        search=SearchConfig(**(raw.get("search") or {})),
        jobs=JobsConfig(**(raw.get("jobs") or {})),
        remote_control=RemoteControlConfig(**(raw.get("remote_control") or {})),
        max_turns=int(budgets.get("max_turns", 80)),
        max_completion_tokens=int(budgets.get("max_completion_tokens", 200000)),
        elide_at=float(compaction.get("elide_at", 0.55)),
        summarize_at=float(compaction.get("summarize_at", 0.65)),
        keep_recent=float(compaction.get("keep_recent", 0.20)),
    )
    if cfg.default_model not in cfg.models:
        raise ValueError(f"default_model {cfg.default_model!r} is not in models")
    for project in cfg.projects.values():
        if project.target != "tower":
            if project.target not in cfg.runners:
                raise ValueError(f"project {project.name}: target {project.target!r} is not in runners")
            if project.homelab:
                raise ValueError(f"project {project.name}: homelab tools only run on the tower")
    return cfg
