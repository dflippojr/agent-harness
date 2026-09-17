"""Daemon configuration: config/harness.yaml plus config/projects.yaml."""

from __future__ import annotations

import os
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MODULE_NAMES = (
    "local_model", "homelab", "memory_library", "images", "jobs", "gpu_guard", "runners",
    "remote_control", "web", "search", "endpoint", "notifications", "backup",
)


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
    api_key_file: str = "D:/Agents/harness/secrets/claude-api-key"
    stop_at_utilization: float = 0.0


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
    image_archive_keep_days: int = 0       # owner-triggered retention only; 0 keeps images indefinitely
    image_archive_min_free_gb: float = 1   # warn without invalidating a database snapshot


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
    # Extra local folders exposed only to native Claude Remote Control, never as harness session projects.
    folders: dict[str, str] = field(default_factory=dict)


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
    # Embeddings need a dedicated llama-server started with --embedding and an embedding model. The normal
    # generation server cannot safely provide them, so the route stays unavailable until both values are set.
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_context_tokens: int = 8192


@dataclass
class ImagesConfig:
    """Local image generation with ComfyUI (images.py). The language model is unloaded while jobs run."""
    enabled: bool = False
    comfy_dir: str = "C:/AI/ComfyUI"          # portable install (python_embeded + ComfyUI)
    port: int = 8188
    work_dir: str = "D:/Agents/harness/images-work"  # ComfyUI output/temp and the harness's PNGs (images/)
    log_dir: str = "D:/Agents/harness/logs"
    linger_seconds: float = 0                  # unused; kept so existing YAML still loads. GPU is released when the queue is empty.
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
class ModulesConfig:
    """Effective optional modules. The service profile defaults every one off."""
    local_model: bool = True
    homelab: bool = True
    memory_library: bool = True
    images: bool = True
    jobs: bool = True
    gpu_guard: bool = True
    runners: bool = True
    remote_control: bool = True
    web: bool = True
    search: bool = True
    endpoint: bool = True
    notifications: bool = True
    backup: bool = True


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
    images: bool = True          # give sessions generate_image (when images is enabled)
    session_search: bool = True  # give sessions session_search / session_read (when search is enabled)
    owner_id: str = "owner"     # stable v1 Control Center owner scope
    managed: bool = False        # loaded from data_dir/projects.yaml rather than checked-in config


@dataclass
class GuestAccess:
    """Time-boxed read-only Control Center access for a tailnet login that is not the owner."""
    login: str
    until: str = ""  # ISO-8601 datetime; empty means until the entry is removed from config


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
    profile: str = "full"
    modules: ModulesConfig = field(default_factory=ModulesConfig)
    # Opaque names to owner-managed files. Only the names may be stored in SQLite; paths stay in local config.
    provider_secret_files: dict[str, str] = field(default_factory=dict)
    backends: dict[str, BackendConfig] = field(default_factory=dict)
    public_url: str = ""               # how the phone reaches the daemon, e.g. https://host.tailnet.ts.net
    allowed_logins: list[str] = field(default_factory=list)  # Tailscale logins allowed through `tailscale serve`
    guests: list[GuestAccess] = field(default_factory=list)  # read-only demo logins; ignored if allowed_logins is empty
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

    @property
    def projects_overlay_path(self) -> Path:
        return self.data_dir / "projects.yaml"

    def capabilities(self) -> dict:
        """Machine-readable service profile and module catalog for first- and third-party clients."""
        return {
            "profile": self.profile,
            "required": {
                "sessions": True, "provider_adapters": True, "approvals": True, "events": True,
                "scoped_tokens": True, "storage": True, "capability_discovery": True,
            },
            "modules": asdict(self.modules),
            "hosted_backends": [name for name, cfg in self.backends.items() if cfg.enabled],
        }


PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROJECT_WRITE_LOCK = threading.RLock()


def _project_from_spec(name: str, spec: dict | None, *, owner_id: str = "owner", managed: bool = False) -> Project:
    spec = spec or {}
    return Project(
        name=name,
        description=str(spec.get("description") or ""),
        instructions=str(spec.get("instructions") or ""),
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
        owner_id=str(spec.get("owner_id") or owner_id),
        managed=managed,
    )


def _project_spec(project: Project) -> dict:
    return {
        "description": project.description,
        "target": project.target,
        "repo": project.repo,
        "owner_id": project.owner_id,
    }


def add_project(cfg: Config, project: Project) -> Project:
    """Persist and hot-add one owner-created project without touching config/projects.yaml."""
    project.name = project.name.strip().lower()
    project.description = project.description.strip()
    project.repo = project.repo.strip()
    if not PROJECT_NAME.fullmatch(project.name):
        raise ValueError("project name must be 1-64 lowercase letters, numbers, dots, dashes, or underscores")
    if len(project.description) > 240:
        raise ValueError("project description is too long")
    if project.target not in ("tower", "macbook"):
        raise ValueError("project target must be tower or macbook")
    if len(project.repo) > 2048 or "\x00" in project.repo:
        raise ValueError("project repository is invalid")
    project.managed = True

    path = cfg.projects_overlay_path
    with _PROJECT_WRITE_LOCK:
        if project.name in cfg.projects:
            raise ValueError(f"project {project.name!r} already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
        saved = dict(raw.get("projects") or {})
        if project.name in saved:
            raise ValueError(f"project {project.name!r} already exists")
        saved[project.name] = _project_spec(project)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump({"projects": saved}, sort_keys=False, allow_unicode=True), encoding="utf-8")
        tmp.replace(path)
        cfg.projects[project.name] = project
    return project


def _load_guests(raw) -> list[GuestAccess]:
    guests = []
    for item in raw or []:
        if isinstance(item, str) and item.strip():
            guests.append(GuestAccess(login=item.strip()))
        elif isinstance(item, dict) and str(item.get("login") or "").strip():
            guests.append(GuestAccess(login=str(item["login"]).strip(), until=str(item.get("until") or "").strip()))
    return guests


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
    # install.ps1 writes this small overlay independently of harness.yaml so switching between the full and service
    # profiles never destroys an existing machine's paths, secrets, or integration settings.
    profile_file = config_dir / "profile.yaml"
    if profile_file.exists():
        profile_raw = yaml.safe_load(profile_file.read_text(encoding="utf-8")) or {}
        if not isinstance(profile_raw, dict):
            raise ValueError("profile.yaml must contain a mapping")
        for key in ("profile", "modules"):
            if key in profile_raw:
                raw[key] = profile_raw[key]
        # A profile can supply a local-model starter when a hosted-only install is later promoted to full. Existing
        # full-install model choices remain authoritative.
        if not (raw.get("models") or {}) and profile_raw.get("models"):
            raw["models"] = profile_raw["models"]
            raw["default_model"] = profile_raw.get("default_model") or ""
        # A service overlay supplies safe hosted-provider defaults only when an existing full install did not
        # already configure that backend. Machine-specific choices in harness.yaml/local.yaml always win.
        profile_backends = profile_raw.get("backends") or {}
        if not isinstance(profile_backends, dict):
            raise ValueError("profile backends must be a mapping")
        existing_backends = raw.get("backends") or {}
        if not isinstance(existing_backends, dict):
            raise ValueError("backends must be a mapping")
        raw["backends"] = {name: {**(spec or {}), **(existing_backends.get(name) or {})}
                           for name, spec in profile_backends.items()} | existing_backends
    profile = str(raw.get("profile") or "full").strip().lower()
    if profile not in ("full", "service"):
        raise ValueError("profile must be 'full' or 'service'")
    raw_modules = raw.get("modules") or {}
    if not isinstance(raw_modules, dict):
        raise ValueError("modules must be a mapping")
    unknown_modules = sorted(set(raw_modules) - set(MODULE_NAMES))
    if unknown_modules:
        raise ValueError(f"unknown modules {unknown_modules}; known: {', '.join(MODULE_NAMES)}")
    module_defaults = profile == "full"
    selected = ModulesConfig(**{name: bool(raw_modules.get(name, module_defaults)) for name in MODULE_NAMES})
    provider_secret_files = raw.get("provider_secret_files") or {}
    if not isinstance(provider_secret_files, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                          for k, v in provider_secret_files.items()):
        raise ValueError("provider_secret_files must map opaque names to file paths")
    resolved_data_dir = Path(data_dir or os.environ.get("HARNESS_DATA_DIR") or raw.get("data_dir", ROOT / "data"))
    projects_file = config_dir / "projects.yaml"
    raw_projects = (yaml.safe_load(projects_file.read_text(encoding="utf-8")) or {}) if projects_file.exists() else {}
    overlay_file = resolved_data_dir / "projects.yaml"
    raw_overlay = (yaml.safe_load(overlay_file.read_text(encoding="utf-8")) or {}) if overlay_file.exists() else {}

    models = {
        name: ModelConfig(name=name, **spec) for name, spec in (raw.get("models") or {}).items()
    }
    projects: dict[str, Project] = {}
    for name, spec in (raw_projects.get("projects") or {}).items():
        spec = spec or {}
        target = str(spec.get("target") or "tower")
        if target != "tower" and not selected.runners:
            continue
        project = _project_from_spec(name, spec)
        project.homelab = project.homelab and selected.homelab
        project.memory_library = project.memory_library and selected.memory_library
        project.web = project.web and selected.web
        project.images = project.images and selected.images
        projects[name] = project
    if not projects:
        projects["scratch"] = Project(name="scratch", description="Empty workspace for each session.")
    # The checked-in catalog wins if an owner later defines the same name there. The generated overlay is private
    # daemon state and is deliberately never written back to config/projects.yaml.
    for name, spec in (raw_overlay.get("projects") or {}).items():
        if name not in projects:
            project = _project_from_spec(name, spec, managed=True)
            if project.target != "tower" and not selected.runners:
                continue
            project.homelab = project.homelab and selected.homelab
            project.memory_library = project.memory_library and selected.memory_library
            project.web = project.web and selected.web
            project.images = project.images and selected.images
            projects[name] = project

    raw_homelab = dict(raw.get("homelab") or {})
    services = {
        name: HomelabService(name=name, **(spec or {})) for name, spec in (raw_homelab.pop("services", None) or {}).items()
    }
    homelab = HomelabConfig(**raw_homelab, services=services if selected.homelab else {})

    runners = ({name: RunnerConfig(name=name, **(spec or {})) for name, spec in (raw.get("runners") or {}).items()}
               if selected.runners else {})
    backends = {name: BackendConfig(**(spec or {})) for name, spec in (raw.get("backends") or {}).items()}
    listen = raw.get("listen") or {}
    budgets = raw.get("budgets") or {}
    compaction = raw.get("compaction") or {}
    notify = NotifyConfig(**(raw.get("notify") or {}))
    gpu_guard = GpuGuardConfig(**(raw.get("gpu_guard") or {}))
    backup = BackupConfig(**(raw.get("backup") or {}))
    memory_library = MemoryLibraryConfig(**(raw.get("memory_library") or {}))
    web = WebConfig(**(raw.get("web") or {}))
    endpoint = EndpointConfig(**(raw.get("endpoint") or {}))
    images = ImagesConfig(**(raw.get("images") or {}))
    search = SearchConfig(**(raw.get("search") or {}))
    jobs = JobsConfig(**(raw.get("jobs") or {}))
    remote_control = RemoteControlConfig(**(raw.get("remote_control") or {}))
    def module_enabled(name: str, configured: bool) -> bool:
        # In the service profile, an explicit module opt-in is the enable switch. Full-profile settings keep their
        # historical two-level behavior: a module must be selected and enabled in its own config section.
        return bool(raw_modules.get(name)) if profile == "service" else configured and getattr(selected, name)

    notify.enabled = module_enabled("notifications", notify.enabled)
    gpu_guard.enabled = module_enabled("gpu_guard", gpu_guard.enabled)
    backup.enabled = module_enabled("backup", backup.enabled)
    memory_library.enabled = module_enabled("memory_library", memory_library.enabled)
    web.enabled = module_enabled("web", web.enabled)
    endpoint.enabled = module_enabled("endpoint", endpoint.enabled)
    images.enabled = module_enabled("images", images.enabled)
    search.enabled = module_enabled("search", search.enabled)
    jobs.enabled = module_enabled("jobs", jobs.enabled)
    remote_control.enabled = module_enabled("remote_control", remote_control.enabled)
    modules = ModulesConfig(
        local_model=selected.local_model,
        homelab=selected.homelab,
        memory_library=memory_library.enabled,
        images=images.enabled,
        jobs=jobs.enabled,
        gpu_guard=gpu_guard.enabled,
        runners=selected.runners,
        remote_control=remote_control.enabled,
        web=web.enabled,
        search=search.enabled,
        endpoint=endpoint.enabled,
        notifications=notify.enabled,
        backup=backup.enabled,
    )
    if not selected.local_model:
        models = {}
    cfg = Config(
        host=listen.get("host", "127.0.0.1"),
        port=int(listen.get("port", 8100)),
        data_dir=resolved_data_dir,
        repos_dir=Path(raw.get("repos_dir", ROOT / "data" / "repos")),
        default_model=(raw.get("default_model") or next(iter(models), "")) if models else "",
        models=models,
        sandbox=SandboxConfig(**(raw.get("sandbox") or {})),
        projects=projects,
        profile=profile,
        modules=modules,
        provider_secret_files=provider_secret_files,
        backends=backends,
        public_url=(raw.get("public_url") or "").rstrip("/"),
        allowed_logins=list(raw.get("allowed_logins") or []),
        guests=_load_guests(raw.get("guests")),
        notify=notify,
        homelab=homelab,
        cleanup=CleanupConfig(**(raw.get("cleanup") or {})),
        runners=runners,
        gpu_guard=gpu_guard,
        backup=backup,
        memory_library=memory_library,
        web=web,
        endpoint=endpoint,
        images=images,
        search=search,
        jobs=jobs,
        remote_control=remote_control,
        max_turns=int(budgets.get("max_turns", 80)),
        max_completion_tokens=int(budgets.get("max_completion_tokens", 200000)),
        elide_at=float(compaction.get("elide_at", 0.55)),
        summarize_at=float(compaction.get("summarize_at", 0.65)),
        keep_recent=float(compaction.get("keep_recent", 0.20)),
    )
    if cfg.modules.local_model and not cfg.models:
        raise ValueError("the local_model module requires at least one configured model")
    if cfg.default_model and cfg.default_model not in cfg.models:
        raise ValueError(f"default_model {cfg.default_model!r} is not in models")
    if cfg.profile == "service" and not any(backend.enabled for backend in cfg.backends.values()):
        raise ValueError("the service profile requires at least one enabled hosted backend")
    local_dependents = [name for name in ("endpoint", "images", "gpu_guard") if getattr(cfg.modules, name)]
    if local_dependents and not cfg.modules.local_model:
        raise ValueError(f"modules {', '.join(local_dependents)} require local_model")
    for project in cfg.projects.values():
        if project.target != "tower":
            if project.target not in cfg.runners:
                raise ValueError(f"project {project.name}: target {project.target!r} is not in runners")
            if project.homelab:
                raise ValueError(f"project {project.name}: homelab tools only run on the tower")
    return cfg
