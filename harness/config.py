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
class NotifyConfig:
    enabled: bool = False
    server: str = "http://127.0.0.1:8095"  # where the daemon publishes (local)
    topic: str = "agent-harness"
    token_file: str = ""                   # file holding an ntfy access token with write access to the topic


@dataclass
class Project:
    name: str
    description: str = ""
    instructions: str = ""
    rules: list[dict] = field(default_factory=list)
    sandbox: dict = field(default_factory=dict)


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
    public_url: str = ""               # how the phone reaches the daemon, e.g. https://host.tailnet.ts.net
    allowed_logins: list[str] = field(default_factory=list)  # Tailscale logins allowed through `tailscale serve`
    notify: NotifyConfig = field(default_factory=NotifyConfig)
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
        )
    if not projects:
        projects["scratch"] = Project(name="scratch", description="Empty workspace for each session.")

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
        public_url=(raw.get("public_url") or "").rstrip("/"),
        allowed_logins=list(raw.get("allowed_logins") or []),
        notify=NotifyConfig(**(raw.get("notify") or {})),
        max_turns=int(budgets.get("max_turns", 80)),
        max_completion_tokens=int(budgets.get("max_completion_tokens", 200000)),
        elide_at=float(compaction.get("elide_at", 0.55)),
        summarize_at=float(compaction.get("summarize_at", 0.65)),
        keep_recent=float(compaction.get("keep_recent", 0.20)),
    )
    if cfg.default_model not in cfg.models:
        raise ValueError(f"default_model {cfg.default_model!r} is not in models")
    return cfg
