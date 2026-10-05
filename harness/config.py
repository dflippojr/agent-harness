"""Daemon configuration: config/harness.yaml plus config/projects.yaml."""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_FILE = "projects.yaml"
MODULE_NAMES = (
    "local_model", "homelab", "memory_library", "images", "image_edit", "jobs", "gpu_guard", "runners",
    "remote_control", "web", "search", "endpoint", "notifications", "backup", "skills",
)
# Opt-in even on a full profile: the Qwen-Image-Edit weights are ~20 GB and must not arrive with an ordinary install.
OPT_IN_MODULES = frozenset({"image_edit"})
# extra_model_paths root shared by installer, doctor, and image components (Z-Image / quality / image_edit).
DEFAULT_IMAGES_MODELS_DIR = "C:/AI/comfy-models"
# Core switches whose on/off is ``cfg.<section>.enabled``.
MODULE_ENABLE_SECTIONS = {
    "web": "web",
    "search": "search",
    "jobs": "jobs",
    "endpoint": "endpoint",
    "gpu_guard": "gpu_guard",
    "notifications": "notify",
    "backup": "backup",
    "memory_library": "memory_library",
    "remote_control": "remote_control",
    "skills": "skills",
}
# Core switches the installer selects and nothing turns off at runtime.
INSTALL_ONLY_MODULES = frozenset({"local_model", "homelab", "runners"})
# The switches the core implements itself. Every other name in MODULE_NAMES belongs to an add-on module
# (harness/modules.py): the installer may write it whether or not the package is there, so the profile accepts it,
# and it only takes effect when a discovered module answers to it.
CORE_MODULE_NAMES = tuple(name for name in MODULE_NAMES
                          if name in MODULE_ENABLE_SECTIONS or name in INSTALL_ONLY_MODULES)


def module_effective(cfg: "Config", name: str) -> bool:
    """True when the module is installed and switched on.

    ``cfg.installed.<name>`` is installer/profile selection. ``cfg.<section>.enabled``
    is the operational switch (YAML, then managed overlay); an add-on module supplies
    its own (``Module.runtime_enabled``). ``cfg.modules`` stays the YAML-time snapshot
    and is not written by overlay setters. Capabilities, /health, /api/v1 features,
    and Manager tool construction all use this function.
    """
    installed = getattr(cfg, "installed", None)
    if installed is None or not bool(getattr(installed, name, False)):
        return False
    if name not in CORE_MODULE_NAMES:
        from .modules import claims, is_present
        module = claims(cfg, name)
        if module is None or not is_present(cfg, module):
            return False
        return bool(module.runtime_enabled(cfg, name)) if module.runtime_enabled else True
    section_name = MODULE_ENABLE_SECTIONS.get(name)
    if section_name is None:
        return True
    section = getattr(cfg, section_name, None)
    return bool(getattr(section, "enabled", False))


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
    # Claude Code and Codex: expose the daemon's tools over MCP through a per-session relay sidecar (#300, #373).
    mcp: bool = True


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
    """The resource guard (gpu_guard.py): pause the queue and unload the model while a game or a Plex hardware
    transcode needs the GPU, load the model only when something needs it, and hold new load while RAM is short.
    The YAML section keeps the `gpu_guard:` name until the guard's name is settled (Settings writes go there)."""
    enabled: bool = False
    poll_seconds: float = 10
    resume_after_seconds: float = 180     # the GPU must stay clear this long before the hold ends
    # A hold ending (or Images giving the GPU back) leaves the model unloaded until a turn, an endpoint request, a
    # local-model selection in the app, or "Load local model now" needs it. False restores the old eager reload.
    lazy_load: bool = True
    # Below this much available physical memory the harness doesn't load the model, start a worker container or
    # start a ComfyUI job; the work waits with a `waiting_memory` reason. 0 turns the RAM check off.
    min_available_ram_gb: float = 4
    load_now_default_minutes: int = 60    # default window for "Load local model now" (idle unload suspended)
    keepalive_seconds: float = 300        # a pinned model gets a one-token request this often; keep under the idle unload
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
class TelemetryConfig:
    """OpenTelemetry traces (#259, docs/observability.md). Off unless `otlp_endpoint` is set."""
    otlp_endpoint: str = ""        # OTLP/HTTP traces URL on loopback or the tailnet, e.g. http://127.0.0.1:4318/v1/traces
    service_name: str = "agent-harness"
    trace_url_template: str = ""   # optional Grafana Explore URL with a {trace_id} placeholder, for the Info tab


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
class GitHubConfig:
    """Owner-only, read-only GitHub task source. Token path must be under data_dir."""
    token_file: str = ""


@dataclass
class DiscoveryConfig:
    enabled: bool = False
    roots: list[str] = field(default_factory=list)
    max_depth: int = 3


@dataclass
class GitHubMemberAuthConfig:
    """`github_member_auth` in harness.yaml (issue #63): per-member GitHub sign-in through Git Credential Manager.

    Installer/file configuration only. Agent Harness Web can switch the feature on or off but never sets the
    executable or the store. Empty `gcm_path` means the feature is unavailable.
    """
    gcm_path: str = ""              # absolute, owner-controlled git-credential-manager executable
    credential_store: str = ""      # wincredman | dpapi (Windows), keychain (macOS), secretservice | gpg (Linux)
    gpg_pass_store_path: str = ""   # gpg only: an initialized `pass` store (has .gpg-id)
    gnupg_home: str = ""            # gpg only: GNUPGHOME holding the store's key
    git_path: str = ""              # optional absolute git executable; default: git on PATH


@dataclass
class GoogleSigninConfig:
    """`google_signin` in harness.yaml (issue #64): Google OpenID Connect for pre-provisioned household members.

    Off by default. Local file configuration only: the client secret stays in an owner-managed file and is never
    copied into SQLite, settings APIs, or the browser. `admitted_logins` are tailnet logins (for example a shared
    household device) that Tailscale admits only to use a linked Google member session; they hold no role of their
    own and must not be an owner, guest, or member login.
    """
    enabled: bool = False
    client_id: str = ""             # non-secret OAuth client ID (*.apps.googleusercontent.com)
    client_secret_file: str = ""    # absolute path; plain secret or Google's downloaded client JSON
    admitted_logins: list[str] = field(default_factory=list)


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
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)

    def __post_init__(self):
        if isinstance(self.discovery, dict):
            self.discovery = DiscoveryConfig(**self.discovery)


@dataclass
class JobsConfig:
    """Scheduled jobs (jobs.py): recurring agent tasks on cron schedules, managed from the app."""
    enabled: bool = False
    poll_seconds: float = 30


@dataclass
class SkillsConfig:
    """Instruction-only owner-approved skills (skills.py). Agents may only stage drafts."""
    enabled: bool = False
    local_review: bool = True          # full local profile: advisory Qwen review at true GPU idle
    proposal_rate_per_hour: int = 8
    reviewer_base_url: str = ""        # hosted-only review; never used unless the owner explicitly starts it
    reviewer_model: str = ""
    reviewer_api_key_file: str = ""    # owner-managed file; never returned by an API


@dataclass
class SearchConfig:
    """Full-text search over past sessions (search.py): the app's search box and the session_search tools."""
    enabled: bool = False


@dataclass
class RepoMapConfig:
    """Experiment (#264, docs/repo-map-study.md): ranked repository map appended to local-model system prompts.
    Off by default; needs requirements-repomap.txt."""
    enabled: bool = False
    budget_tokens: int = 1500  # estimated as chars / 4


@dataclass
class CanaryConfig:
    """Nightly agent regression canary (#265, docs/canary-evals.md): a pinned task set through the production path."""
    enabled: bool = False
    at: str = "03:00"                    # tower-local time of the one nightly run
    repeats: int = 2
    total_cap_seconds: int = 2700        # remaining tasks are recorded as `timeout` past this
    start_wait_seconds: int = 600        # how long a run waits for the GPU to be free before it gives up for the night
    baseline_runs: int = 5
    min_prior_runs: int = 3
    drop_points: float = 15.0
    metrics_limit: int = 30
    suite: str = "bakeoff/canary.yaml"
    fixture_dir: str = "D:/Agents/harness/web-fixture"  # the recorded web for the web tasks (bakeoff/web_suite.py)
    disabled_reason: str = ""            # set by the loader when `enabled` was turned off for a bad suite/fixture (doctor reports it)


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
    edit_enabled: bool = False                 # resolved opt-in; absent YAML follows modules.image_edit
    comfy_dir: str = "C:/AI/ComfyUI"          # portable install (python_embeded + ComfyUI)
    models_dir: str = DEFAULT_IMAGES_MODELS_DIR  # extra_model_paths root (diffusion_models / text_encoders / vae)
    port: int = 8188
    work_dir: str = "D:/Agents/harness/images-work"  # ComfyUI output/temp and the harness's PNGs (images/)
    log_dir: str = "D:/Agents/harness/logs"
    linger_seconds: float = 0                  # unused; kept so existing YAML still loads. GPU is released when the queue is empty.
    start_timeout_seconds: float = 180
    job_timeout_seconds: float = 1200
    max_upload_bytes: int = 20 * 2**20
    max_pixels: int = 20_000_000
    upscale_dir: str = ""                      # Real-ESRGAN weights; empty → <comfy_dir>/ComfyUI/models/upscale_models
    upscale_max_pixels: int = 36_000_000       # refuse 2×/4× outputs above this before allocating
    upscale_tile: int = 512                    # ComfyUI ImageUpscaleWithModel starting tile
    upscale_overlap: int = 32


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
    image_edit: bool = False     # optional Qwen-Image-Edit; off until explicitly enabled
    jobs: bool = True
    gpu_guard: bool = True
    runners: bool = True
    remote_control: bool = True
    web: bool = True
    search: bool = True
    endpoint: bool = True
    notifications: bool = True
    backup: bool = True
    skills: bool = True


@dataclass
class ToolOutputConfig:
    """Per-call tool result bounds. The agent may raise a limit up to the matching *_max."""
    read_file_lines: int = 400
    read_file_lines_max: int = 2000
    search_matches: int = 100
    search_matches_max: int = 500
    run_shell_chars: int = 20000
    run_shell_chars_max: int = 100000
    verify_summary_chars: int = 4000


@dataclass
class VerifyCheck:
    """One owner-configured check for the verify tool. Commands are trusted (no run_shell policy)."""
    name: str
    command: str
    timeout: int = 120
    parser: str = ""  # pytest | generic | empty (infer from the command)


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
    owner_id: str = "owner"     # stable v1 Agent Harness Web owner scope
    managed: bool = False        # loaded from data_dir/projects.yaml rather than checked-in config
    tool_output: dict = field(default_factory=dict)  # optional overlay on Config.tool_output
    verify: list = field(default_factory=list)       # list[VerifyCheck]; empty → verify returns an error


@dataclass
class GuestAccess:
    """Time-boxed read-only Agent Harness Web access for a tailnet login that is not the owner."""
    login: str
    until: str = ""  # ISO-8601 datetime; empty means until the entry is removed from config


def _smart_approvals_default():
    from .smart_approvals import SmartConfig
    return SmartConfig()


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
    installed: ModulesConfig = field(default_factory=ModulesConfig)
    config_dir: Path = field(default_factory=lambda: ROOT / "config")
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
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    memory_library: MemoryLibraryConfig = field(default_factory=MemoryLibraryConfig)
    web: WebConfig = field(default_factory=WebConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    github_member_auth: GitHubMemberAuthConfig = field(default_factory=GitHubMemberAuthConfig)
    google_signin: GoogleSigninConfig = field(default_factory=GoogleSigninConfig)
    endpoint: EndpointConfig = field(default_factory=EndpointConfig)
    images: ImagesConfig = field(default_factory=ImagesConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    jobs: JobsConfig = field(default_factory=JobsConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    remote_control: RemoteControlConfig = field(default_factory=RemoteControlConfig)
    smart_approvals: object = field(default_factory=_smart_approvals_default)
    max_turns: int = 80
    max_completion_tokens: int = 200000
    elide_at: float = 0.55
    summarize_at: float = 0.65
    keep_recent: float = 0.20
    reset_at: float = 0.60
    mask_min_chars: int = 2000
    state_max_chars: int = 8000
    repo_map: RepoMapConfig = field(default_factory=RepoMapConfig)
    canary: CanaryConfig = field(default_factory=CanaryConfig)
    tool_output: ToolOutputConfig = field(default_factory=ToolOutputConfig)
    # Add-on module packages (harness/modules.py); None: every package under the harness_modules namespace.
    module_packages: tuple[str, ...] | None = None

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
        return self.data_dir / PROJECTS_FILE

    def module_effective(self, name: str) -> bool:
        """Installed AND switched on. The single source for capabilities/features/Manager."""
        return module_effective(self, name)

    def capabilities(self) -> dict:
        """Machine-readable service profile and module catalog for first- and third-party clients."""
        return {
            "profile": self.profile,
            "required": {
                "sessions": True, "provider_adapters": True, "approvals": True, "events": True,
                "scoped_tokens": True, "storage": True, "capability_discovery": True,
            },
            "modules": {name: module_effective(self, name) for name in CORE_MODULE_NAMES} | self._module_capabilities(),
            "hosted_backends": [name for name, cfg in self.backends.items() if cfg.enabled],
            "config_registry": True,
            "supervised_restart": os.environ.get("HARNESS_SUPERVISED", "").strip() in ("1", "true", "yes"),
        }

    def _module_capabilities(self) -> dict:
        """Each present add-on module's switches. An absent module has no entry at all."""
        from .modules import present_switches
        return {name: module_effective(self, name) for name in present_switches(self)}


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
        tool_output=spec.get("tool_output") if isinstance(spec.get("tool_output"), dict) else {},
        verify=_verify_checks(spec.get("verify")),
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


def _load_google_signin(raw) -> GoogleSigninConfig:
    raw = dict(raw or {})
    raw["admitted_logins"] = [str(x).strip() for x in (raw.get("admitted_logins") or []) if str(x).strip()]
    return GoogleSigninConfig(**raw)


def _canary_fixture_problem(raw: dict) -> str:
    """Why an enabled canary can't run (missing suite file, or web tasks without a fixture manifest); "" if fine."""
    suite = raw.get("suite", CanaryConfig.suite)
    if not (raw.get("enabled") and suite):
        return ""
    if not (ROOT / suite).is_file():
        return f"canary.suite {suite!r} is not a file"
    try:
        web = (yaml.safe_load((ROOT / suite).read_text(encoding="utf-8")) or {}).get("web")
    except (OSError, yaml.YAMLError, AttributeError) as e:
        return f"canary.suite {suite!r} can't be read: {e}"
    fixture = Path(raw.get("fixture_dir", CanaryConfig.fixture_dir))
    if web and not (fixture / "manifest.json").is_file():  # every web task would fail against an empty replay
        return f"canary.fixture_dir {str(fixture)!r} has no manifest.json (the suite has web tasks)"
    return ""


def _load_canary(raw) -> CanaryConfig:
    """`at` comes back as "HH:MM"; anything the nightly can't use is a config error here, not a dead loop at night."""
    from .canary import parse_at
    raw = dict(raw or {})
    at = raw.get("at", CanaryConfig.at)
    if isinstance(at, int) and not isinstance(at, bool):  # YAML 1.1 reads an unquoted 3:05 as 3 * 60 + 5
        at = f"{at // 60}:{at % 60}" if 0 <= at < 24 * 60 else str(at)
    hour, minute = parse_at(at)
    raw["at"] = f"{hour:02d}:{minute:02d}"
    for key, kind, low in (("repeats", int, 1), ("total_cap_seconds", int, 1), ("start_wait_seconds", int, 0),
                           ("baseline_runs", int, 1), ("min_prior_runs", int, 1), ("drop_points", float, 1),
                           ("metrics_limit", int, 1)):
        value = raw.get(key, getattr(CanaryConfig, key))
        try:
            if isinstance(value, bool):
                raise ValueError
            raw[key] = kind(value)
        except (TypeError, ValueError):
            raise ValueError(f"canary.{key} must be a number, got {value!r}") from None
        if raw[key] < low:
            raise ValueError(f"canary.{key} must be at least {low}, got {value!r}")
    if raw["min_prior_runs"] > raw["baseline_runs"]:
        raise ValueError("canary.min_prior_runs can't be more than canary.baseline_runs (no run would ever be judged)")
    problem = _canary_fixture_problem(raw)
    if problem:  # a misconfigured canary must never stop the daemon: log it, turn only the canary off
        logging.getLogger(__name__).error("canary disabled: %s", problem)
        raw["enabled"], raw["disabled_reason"] = False, problem
    return CanaryConfig(**raw)


def _load_guests(raw) -> list[GuestAccess]:
    guests = []
    for item in raw or []:
        if isinstance(item, str) and item.strip():
            guests.append(GuestAccess(login=item.strip()))
        elif isinstance(item, dict) and str(item.get("login") or "").strip():
            guests.append(GuestAccess(login=str(item["login"]).strip(), until=str(item.get("until") or "").strip()))
    return guests


def resolve_images_models_dir(cfg=None) -> Path:
    """Single models root used by installer, daemon, doctor, and image components."""
    configured = getattr(cfg, "models_dir", None) if cfg is not None else None
    return Path(configured or DEFAULT_IMAGES_MODELS_DIR)


def images_models_dir_matches(left: str | Path, right: str | Path) -> bool:
    return Path(left).as_posix().rstrip("/").casefold() == Path(right).as_posix().rstrip("/").casefold()


def _read_yaml(path: Path) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def _apply_profile_overlay(raw: dict, profile_raw: dict) -> None:
    """Fold install.ps1's profile.yaml into `raw` in place; existing machine settings always win."""
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


def _read_raw_config(config_dir: Path) -> dict:
    """harness.yaml, then the untracked harness.local.yaml, then install.ps1's profile.yaml."""
    raw = yaml.safe_load((config_dir / "harness.yaml").read_text(encoding="utf-8")) or {}
    # Machine-specific values (tailnet URL, logins) live in an untracked harness.local.yaml; its top-level
    # sections are merged over harness.yaml's (one level deep).
    for key, value in _read_yaml(config_dir / "harness.local.yaml").items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key] = {**raw[key], **value}
        else:
            raw[key] = value
    # install.ps1 writes this small overlay independently of harness.yaml so switching between the full and service
    # profiles never destroys an existing machine's paths, secrets, or integration settings.
    profile_file = config_dir / "profile.yaml"
    if profile_file.exists():
        _apply_profile_overlay(raw, yaml.safe_load(profile_file.read_text(encoding="utf-8")) or {})
    return raw


def _resolve_profile(raw: dict) -> tuple[str, dict, ModulesConfig]:
    """The validated profile name, its raw module mapping, and the modules it selects."""
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
    selected = ModulesConfig(**{
        name: bool(raw_modules.get(name, False if name in OPT_IN_MODULES else module_defaults))
        for name in MODULE_NAMES
    })
    if selected.image_edit:
        selected.images = True
        selected.local_model = True
    return profile, raw_modules, selected


def _gate_project(project: Project, selected: ModulesConfig) -> Project | None:
    """Mask a project's optional features by the selected modules; None if its runner target is unavailable."""
    if project.target != "tower" and not selected.runners:
        return None
    project.homelab = project.homelab and selected.homelab
    project.memory_library = project.memory_library and selected.memory_library
    project.web = project.web and selected.web
    project.images = project.images and selected.images
    return project


def _load_projects(raw_projects: dict, raw_overlay: dict, selected: ModulesConfig) -> dict[str, Project]:
    projects: dict[str, Project] = {}
    for name, spec in (raw_projects.get("projects") or {}).items():
        project = _gate_project(_project_from_spec(name, spec or {}), selected)
        if project is not None:
            projects[name] = project
    if not projects:
        projects["scratch"] = Project(name="scratch", description="Empty workspace for each session.")
    # The checked-in catalog wins if an owner later defines the same name there. The generated overlay is private
    # daemon state and is deliberately never written back to config/projects.yaml.
    for name, spec in (raw_overlay.get("projects") or {}).items():
        if name in projects:
            continue
        project = _gate_project(_project_from_spec(name, spec, managed=True), selected)
        if project is not None:
            projects[name] = project
    return projects


def _load_provider_secret_files(raw: dict) -> dict:
    provider_secret_files = raw.get("provider_secret_files") or {}
    if not isinstance(provider_secret_files, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                          for k, v in provider_secret_files.items()):
        raise ValueError("provider_secret_files must map opaque names to file paths")
    return provider_secret_files


def _load_homelab(raw: dict, selected) -> HomelabConfig:
    raw_homelab = dict(raw.get("homelab") or {})
    services = {
        name: HomelabService(name=name, **(spec or {})) for name, spec in (raw_homelab.pop("services", None) or {}).items()
    }
    return HomelabConfig(**raw_homelab, services=services if selected.homelab else {})


def _load_runners(raw: dict, selected) -> dict:
    if not selected.runners:
        return {}
    return {name: RunnerConfig(name=name, **(spec or {})) for name, spec in (raw.get("runners") or {}).items()}


def _module_packages(raw) -> tuple[str, ...] | None:
    """``module_packages``: the add-on packages to load, in order (absent: the harness_modules namespace)."""
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(name, str) and name.strip() for name in raw):
        raise ValueError("module_packages must be a list of Python package names")
    return tuple(name.strip() for name in raw)


def _load_images(raw: dict, selected) -> ImagesConfig:
    raw_images = raw.get("images") or {}
    images = ImagesConfig(**raw_images)
    # The switch is tri-state in configuration: absent follows the install/profile
    # choice, while explicit YAML true/false remains authoritative. Managed Settings
    # is applied below and provides the same explicit override without rewriting YAML.
    if "edit_enabled" not in raw_images:
        images.edit_enabled = selected.image_edit
    return images


def _module_enabled(profile: str, raw_modules: dict, selected, name: str, configured: bool) -> bool:
    # In the service profile, an explicit module opt-in is the enable switch. Full-profile settings keep their
    # historical two-level behavior: a module must be selected and enabled in its own config section.
    return bool(raw_modules.get(name)) if profile == "service" else configured and getattr(selected, name)


def _load_module_sections(raw: dict, profile: str, raw_modules: dict, selected) -> dict:
    """Build the per-module config sections, apply module enablement, and derive ModulesConfig."""
    notify = NotifyConfig(**(raw.get("notify") or {}))
    gpu_guard = GpuGuardConfig(**(raw.get("gpu_guard") or {}))
    backup = BackupConfig(**(raw.get("backup") or {}))
    memory_library = MemoryLibraryConfig(**(raw.get("memory_library") or {}))
    web = WebConfig(**(raw.get("web") or {}))
    endpoint = EndpointConfig(**(raw.get("endpoint") or {}))
    images = _load_images(raw, selected)
    search = SearchConfig(**(raw.get("search") or {}))
    jobs = JobsConfig(**(raw.get("jobs") or {}))
    skills = SkillsConfig(**(raw.get("skills") or {}))
    remote_control = RemoteControlConfig(**(raw.get("remote_control") or {}))
    def module_enabled(name: str, configured: bool) -> bool:
        return _module_enabled(profile, raw_modules, selected, name, configured)

    notify.enabled = module_enabled("notifications", notify.enabled)
    gpu_guard.enabled = module_enabled("gpu_guard", gpu_guard.enabled)
    backup.enabled = module_enabled("backup", backup.enabled)
    memory_library.enabled = module_enabled("memory_library", memory_library.enabled)
    web.enabled = module_enabled("web", web.enabled)
    endpoint.enabled = module_enabled("endpoint", endpoint.enabled)
    images.enabled = module_enabled("images", images.enabled) or selected.image_edit
    search.enabled = module_enabled("search", search.enabled)
    jobs.enabled = module_enabled("jobs", jobs.enabled)
    skills.enabled = module_enabled("skills", skills.enabled)
    remote_control.enabled = module_enabled("remote_control", remote_control.enabled)
    modules = ModulesConfig(
        local_model=selected.local_model,
        homelab=selected.homelab,
        memory_library=memory_library.enabled,
        images=images.enabled,
        image_edit=selected.image_edit and images.edit_enabled,
        jobs=jobs.enabled,
        gpu_guard=gpu_guard.enabled,
        runners=selected.runners,
        remote_control=remote_control.enabled,
        web=web.enabled,
        search=search.enabled,
        endpoint=endpoint.enabled,
        notifications=notify.enabled,
        backup=backup.enabled,
        skills=skills.enabled,
    )
    return {
        "notify": notify, "gpu_guard": gpu_guard, "backup": backup, "memory_library": memory_library,
        "web": web, "endpoint": endpoint, "images": images, "search": search, "jobs": jobs,
        "skills": skills, "remote_control": remote_control, "modules": modules,
    }


def load(config_dir: Path | None = None, data_dir: Path | None = None) -> Config:
    from .smart_approvals import load_smart_config
    config_dir = Path(config_dir or os.environ.get("HARNESS_CONFIG_DIR") or ROOT / "config")
    raw = _read_raw_config(config_dir)
    profile, raw_modules, selected = _resolve_profile(raw)
    provider_secret_files = _load_provider_secret_files(raw)
    resolved_data_dir = Path(data_dir or os.environ.get("HARNESS_DATA_DIR") or raw.get("data_dir", ROOT / "data"))

    models = {
        name: ModelConfig(name=name, **spec) for name, spec in (raw.get("models") or {}).items()
    }
    projects = _load_projects(_read_yaml(config_dir / PROJECTS_FILE),
                              _read_yaml(resolved_data_dir / PROJECTS_FILE), selected)

    homelab = _load_homelab(raw, selected)
    runners = _load_runners(raw, selected)
    backends = {name: BackendConfig(**(spec or {})) for name, spec in (raw.get("backends") or {}).items()}
    listen = raw.get("listen") or {}
    budgets = raw.get("budgets") or {}
    compaction = raw.get("compaction") or {}
    sections = _load_module_sections(raw, profile, raw_modules, selected)
    notify, gpu_guard, backup, memory_library = (sections[k] for k in ("notify", "gpu_guard", "backup", "memory_library"))
    web, endpoint, images, search = (sections[k] for k in ("web", "endpoint", "images", "search"))
    jobs, skills, remote_control = (sections[k] for k in ("jobs", "skills", "remote_control"))
    modules = sections["modules"]
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
        module_packages=_module_packages(raw.get("module_packages")),
        profile=profile,
        modules=modules,
        installed=selected,
        config_dir=config_dir,
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
        telemetry=TelemetryConfig(**(raw.get("telemetry") or {})),
        memory_library=memory_library,
        web=web,
        github=GitHubConfig(**(raw.get("github") or {})),
        github_member_auth=GitHubMemberAuthConfig(**(raw.get("github_member_auth") or {})),
        google_signin=_load_google_signin(raw.get("google_signin")),
        endpoint=endpoint,
        images=images,
        search=search,
        repo_map=RepoMapConfig(**(raw.get("repo_map") or {})),
        canary=_load_canary(raw.get("canary")),
        jobs=jobs,
        skills=skills,
        remote_control=remote_control,
        smart_approvals=load_smart_config(raw.get("smart_approvals")),
        max_turns=int(budgets.get("max_turns", 80)),
        max_completion_tokens=int(budgets.get("max_completion_tokens", 200000)),
        elide_at=float(compaction.get("elide_at", 0.55)),
        summarize_at=float(compaction.get("summarize_at", 0.65)),
        keep_recent=float(compaction.get("keep_recent", 0.20)),
        reset_at=_reset_at(compaction.get("reset_at", 0.60),
                           float(compaction.get("elide_at", 0.55)),
                           float(compaction.get("summarize_at", 0.65))),
        mask_min_chars=_mask_min_chars(compaction.get("mask_min_chars", 2000)),
        state_max_chars=_state_max_chars(compaction.get("state_max_chars", 8000)),
        tool_output=_tool_output(raw.get("tool_output")),
    )
    _validate_loaded(cfg)
    from .settings_keys import build_registry
    registry = build_registry(cfg)
    cfg._inherited = {spec.key: spec.getter(cfg) for spec in registry.writable_admin()}
    _apply_managed_overlay(cfg)
    return cfg


TOOL_OUTPUT_DEFAULTS = ToolOutputConfig()
_TOOL_OUTPUT_BOUNDS = {
    "read_file_lines": (1, 100_000),
    "read_file_lines_max": (1, 100_000),
    "search_matches": (1, 100_000),
    "search_matches_max": (1, 100_000),
    "run_shell_chars": (1, 1_000_000),
    "run_shell_chars_max": (1, 1_000_000),
    "verify_summary_chars": (1, 100_000),
}


def _bounded_int(value, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if lo <= parsed <= hi else default


def _tool_output(raw, base: ToolOutputConfig = TOOL_OUTPUT_DEFAULTS) -> ToolOutputConfig:
    """Validate a tool_output: block. Missing or invalid keys fall back to `base` (the package
    defaults globally, the resolved global config for a project overlay)."""
    spec = raw if isinstance(raw, dict) else {}
    values = {}
    for key, (lo, hi) in _TOOL_OUTPUT_BOUNDS.items():
        values[key] = _bounded_int(spec.get(key, getattr(base, key)), getattr(base, key), lo, hi)
    if values["read_file_lines"] > values["read_file_lines_max"]:
        values["read_file_lines"] = values["read_file_lines_max"]
    if values["search_matches"] > values["search_matches_max"]:
        values["search_matches"] = values["search_matches_max"]
    if values["run_shell_chars"] > values["run_shell_chars_max"]:
        values["run_shell_chars"] = values["run_shell_chars_max"]
    return ToolOutputConfig(**values)


def resolve_tool_output(cfg: Config, project: Project | None = None) -> ToolOutputConfig:
    """Global tool_output, overlaid by a project's tool_output: block when present."""
    if project is None or not project.tool_output:
        return cfg.tool_output
    return _tool_output(project.tool_output, base=cfg.tool_output)


def clamp_tool_limit(requested, default: int, maximum: int) -> int:
    """Per-call raise: omitted/invalid → default; otherwise clamp to 1..maximum."""
    if requested is None or isinstance(requested, bool):
        return default
    try:
        parsed = int(requested)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(1, min(parsed, maximum))


def _verify_checks(value) -> list[VerifyCheck]:
    if not isinstance(value, list):
        return []
    checks: list[VerifyCheck] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        command = str(item.get("command") or "").strip()
        if not name or not command:
            continue
        parser = str(item.get("parser") or "").strip().lower()
        if parser not in ("", "pytest", "generic"):
            parser = ""
        checks.append(VerifyCheck(
            name=name, command=command,
            timeout=clamp_tool_limit(item.get("timeout"), 120, 1800),
            parser=parser,
        ))
    return checks


def _mask_min_chars(value) -> int:
    if isinstance(value, bool):
        return 2000
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 2000
    return parsed if 1 <= parsed <= 10_000_000 else 2000


def _reset_at(value, elide: float = 0.55, summarize: float = 0.65) -> float:
    """Bounds 0.15–0.95; omitted/invalid use 0.60. If that is not strictly between elide and summarize, use the midpoint."""
    default = 0.60
    if isinstance(value, bool):
        parsed = default
    else:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            parsed = default
        if not 0.15 <= parsed <= 0.95:
            parsed = default
    if elide < parsed < summarize:
        return parsed
    mid = (float(elide) + float(summarize)) / 2
    if 0.15 <= mid <= 0.95 and elide < mid < summarize:
        return mid
    return default


def _state_max_chars(value) -> int:
    if isinstance(value, bool):
        return 8000
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 8000
    return parsed if 256 <= parsed <= 100_000 else 8000


def _validate_loaded(cfg: Config) -> None:
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
        _validate_project_target(cfg, project)


def _validate_project_target(cfg: Config, project) -> None:
    if project.target == "tower":
        return
    if project.target not in cfg.runners:
        raise ValueError(f"project {project.name}: target {project.target!r} is not in runners")
    if project.homelab:
        raise ValueError(f"project {project.name}: homelab tools only run on the tower")


def _apply_managed_overlay(cfg: Config) -> None:
    """Apply registered admin keys from data_dir/managed-config.json after YAML loading.

    An invalid or unconfirmed managed candidate restores the last known good overlay.
    If that overlay is also unusable, both files are quarantined and YAML defaults
    remain in effect. Invalid base/local/profile YAML is never masked by this fallback.
    """
    from .settings_service import SettingsService
    SettingsService(cfg).apply_overlay()
