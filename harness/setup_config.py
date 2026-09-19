"""Write a starter configuration for a new install (used by the platform installers).

    python -m harness.setup_config --config-dir C:/.../config --data-dir C:/.../data --model qwen --port 8100

Writes harness.yaml and projects.yaml (never overwrites existing files unless --force), plus the selected
profile.yaml overlay on every run. Tower-specific pieces of the author's own config (homelab services, ntfy,
SearXNG, ComfyUI, the memory library, the MacBook runner) are left out; docs/INSTALL.md explains how to turn them on.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from .config import MODULE_NAMES

PRESETS = {
    # tested on an RTX 4070 Ti Super 16 GB with 32 GB RAM (docs/phase0-results.md)
    "qwen": {"name": "qwen3.6-35b-a3b", "context_tokens": 65536, "max_tokens": 8192,
             "sampling": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0}},
    "gpt-oss": {"name": "gpt-oss-20b", "context_tokens": 32768, "max_tokens": 8192,
                "sampling": {"temperature": 1.0, "top_p": 1.0}},
}

SERVICE_BACKENDS = {
    "claude": {"enabled": True, "model": "claude-opus-5", "effort": "high", "permission_mode": "default",
               "proxy": "http://harness-egress-claude:8888", "volume": "harness-auth-claude",
               "network": "harness-cli-claude"},
    "codex": {"enabled": True, "model": "gpt-5.6-sol", "effort": "high", "permission_mode": "on-request",
              "proxy": "http://harness-egress-codex:8888", "volume": "harness-auth-codex",
              "network": "harness-cli-codex"},
    "cursor": {"enabled": True, "model": "cursor-grok-4.6-high", "effort": "high", "permission_mode": "force",
               "proxy": "http://harness-egress-cursor:8888", "volume": "harness-auth-cursor",
               "network": "harness-cli-cursor"},
}

PROJECTS = """# Projects a session can run under. See the repository's config/projects.yaml for every field
# (git repos with review branches, homelab tools, MacBook targets, approval rules).
projects:
  scratch:
    description: Empty workspace for each session.

  # my-repo:
  #   description: A local git repository; each session works on its own branch
  #   repo: C:/Users/you/Projects/my-repo
"""


def build(args) -> dict:
    preset = PRESETS[args.model]
    data = Path(args.data_dir).as_posix()
    enabled = set(args.enable_module)
    local_model = args.profile == "full" or "local_model" in enabled
    result = {
        "listen": {"host": "127.0.0.1", "port": args.port},
        "public_url": args.public_url,
        "allowed_logins": [args.login] if args.login else [],
        "data_dir": data,
        "repos_dir": f"{data}/repos",
        "default_model": preset["name"] if local_model else "",
        "models": ({preset["name"]: {"base_url": args.llama_url, "context_tokens": preset["context_tokens"],
                                      "max_tokens": preset["max_tokens"], "sampling": preset["sampling"]}}
                   if local_model else {}),
        "notify": {"enabled": False},
        "sandbox": {"image": "agent-harness-sandbox:py312", "memory": "2g", "cpus": "2", "pids": 512,
                    "network": "harness-sandbox", "egress_network": "harness-egress"},
        "homelab": {"docker_root": f"{data}/docker", "services": {}},
        "cleanup": {"interval_minutes": 60, "container_idle_hours": 24, "workspace_retention_days": 14,
                    "workspace_quota_mb": 5000, "min_free_gb": 20},
        "budgets": {"max_turns": 80, "max_completion_tokens": 200000},
        "compaction": {"elide_at": 0.55, "summarize_at": 0.65, "keep_recent": 0.20},
        "gpu_guard": {"enabled": args.gpu_guard and (args.profile == "full" or "gpu_guard" in enabled),
                      "pause_flag": args.pause_flag},
        "backup": {"enabled": args.profile == "full" or "backup" in enabled,
                   "dir": f"{data}/backups", "at": "03:30", "keep_days": 14,
                   "image_archive_keep_days": 0, "image_archive_min_free_gb": 1},
        "endpoint": {"enabled": args.profile == "full" or "endpoint" in enabled},
        "web": {"enabled": "web" in enabled},
        "images": {"enabled": "images" in enabled or "image_edit" in enabled},
        "memory_library": {"enabled": "memory_library" in enabled},
        "search": {"enabled": "search" in enabled},
        "jobs": {"enabled": "jobs" in enabled},
        "skills": {"enabled": args.profile == "full" or "skills" in enabled},
        "remote_control": {"enabled": "remote_control" in enabled},
    }
    if args.profile == "service":
        result["backends"] = SERVICE_BACKENDS
    return result


def profile_overlay(args) -> dict:
    enabled = dict.fromkeys(args.enable_module, True) if args.profile == "service" else {}
    if "image_edit" in args.enable_module:
        enabled["image_edit"] = True
    overlay = {"profile": args.profile, "modules": enabled,
               **({"backends": SERVICE_BACKENDS} if args.profile == "service" else {})}
    if args.profile == "full" or "local_model" in enabled:
        preset = PRESETS[args.model]
        overlay.update({
            "default_model": preset["name"],
            "models": {preset["name"]: {"base_url": args.llama_url, "context_tokens": preset["context_tokens"],
                                                "max_tokens": preset["max_tokens"],
                                                "sampling": preset["sampling"]}},
        })
    return overlay


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config-dir", required=True)
    p.add_argument("--data-dir", required=True)
    p.add_argument("--model", choices=sorted(PRESETS), default="qwen")
    p.add_argument("--profile", choices=("full", "service"), default="full")
    p.add_argument("--enable-module", action="append", default=[], choices=MODULE_NAMES)
    p.add_argument("--port", type=int, default=8100)
    p.add_argument("--llama-url", default="http://127.0.0.1:8090")
    p.add_argument("--pause-flag", required=True)
    p.add_argument("--gpu-guard", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--public-url", default="")
    p.add_argument("--login", default="")
    p.add_argument("--force", action="store_true")
    args = p.parse_args(argv)
    enabled = set(args.enable_module)
    local_dependents = enabled & {"endpoint", "images", "image_edit", "gpu_guard"}
    if local_dependents:
        enabled.add("local_model")
    if "image_edit" in enabled:
        enabled.add("images")
    args.enable_module = sorted(enabled)

    config_dir = Path(args.config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    Path(args.data_dir).mkdir(parents=True, exist_ok=True)
    header = ("# Written by the agent-harness installer (harness/setup_config.py). Edit freely; it won't overwrite\n"
              "# it unless run with -Force. Every section is documented in the repository's config/harness.yaml.\n")
    for name, text in (("harness.yaml", header + yaml.safe_dump(build(args), sort_keys=False)),
                       ("projects.yaml", PROJECTS)):
        target = config_dir / name
        if target.exists() and not args.force:
            print(f"kept existing {target}")
            continue
        target.write_text(text, encoding="utf-8")
        print(f"wrote {target}")
    profile_path = config_dir / "profile.yaml"
    profile_header = ("# Written by the agent-harness installer (harness/setup_config.py). It updates this\n"
                      "# reversible profile overlay on every run; put machine-specific settings in harness.local.yaml.\n")
    profile_path.write_text(profile_header + yaml.safe_dump(profile_overlay(args), sort_keys=False), encoding="utf-8")
    print(f"wrote {profile_path}")
    from . import config
    cfg = config.load(config_dir)  # fail now rather than at daemon start
    model = (f"model {cfg.default_model} at {cfg.models[cfg.default_model].base_url}"
             if cfg.modules.local_model else "hosted providers only")
    print(f"config OK: {cfg.profile} profile, {model}, daemon port {cfg.port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
