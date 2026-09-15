"""Write a starter configuration for a new install (used by install/install.ps1).

    python -m harness.setup_config --config-dir C:/.../config --data-dir C:/.../data --model qwen --port 8100

Writes harness.yaml and projects.yaml (never overwrites existing files unless --force). Tower-specific pieces of the
author's own config (homelab services, ntfy, SearXNG, ComfyUI, the memory library, the MacBook runner) are left out;
docs/INSTALL.md explains how to turn them on.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

PRESETS = {
    # tested on an RTX 4070 Ti Super 16 GB with 32 GB RAM (docs/phase0-results.md)
    "qwen": {"name": "qwen3.6-35b-a3b", "context_tokens": 65536, "max_tokens": 8192,
             "sampling": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0}},
    "gpt-oss": {"name": "gpt-oss-20b", "context_tokens": 32768, "max_tokens": 8192,
                "sampling": {"temperature": 1.0, "top_p": 1.0}},
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
    return {
        "listen": {"host": "127.0.0.1", "port": args.port},
        "public_url": args.public_url,
        "allowed_logins": [args.login] if args.login else [],
        "data_dir": data,
        "repos_dir": f"{data}/repos",
        "default_model": preset["name"],
        "models": {preset["name"]: {"base_url": args.llama_url, "context_tokens": preset["context_tokens"],
                                    "max_tokens": preset["max_tokens"], "sampling": preset["sampling"]}},
        "notify": {"enabled": False},
        "sandbox": {"image": "agent-harness-sandbox:py312", "memory": "2g", "cpus": "2", "pids": 512,
                    "network": "harness-sandbox", "egress_network": "harness-egress"},
        "homelab": {"docker_root": f"{data}/docker", "services": {}},
        "cleanup": {"interval_minutes": 60, "container_idle_hours": 24, "workspace_retention_days": 14,
                    "workspace_quota_mb": 5000, "min_free_gb": 20},
        "budgets": {"max_turns": 80, "max_completion_tokens": 200000},
        "compaction": {"elide_at": 0.55, "summarize_at": 0.65, "keep_recent": 0.20},
        "gpu_guard": {"enabled": args.gpu_guard, "pause_flag": args.pause_flag},
        "backup": {"enabled": True, "dir": f"{data}/backups", "at": "03:30", "keep_days": 14},
        "endpoint": {"enabled": True},
        "web": {"enabled": False},
        "images": {"enabled": False},
        "memory_library": {"enabled": False},
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config-dir", required=True)
    p.add_argument("--data-dir", required=True)
    p.add_argument("--model", choices=sorted(PRESETS), default="qwen")
    p.add_argument("--port", type=int, default=8100)
    p.add_argument("--llama-url", default="http://127.0.0.1:8090")
    p.add_argument("--pause-flag", required=True)
    p.add_argument("--gpu-guard", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--public-url", default="")
    p.add_argument("--login", default="")
    p.add_argument("--force", action="store_true")
    args = p.parse_args(argv)

    config_dir = Path(args.config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    Path(args.data_dir).mkdir(parents=True, exist_ok=True)
    header = ("# Written by install/install.ps1 (harness/setup_config.py). Edit freely; the installer won't overwrite\n"
              "# it unless run with -Force. Every section is documented in the repository's config/harness.yaml.\n")
    for name, text in (("harness.yaml", header + yaml.safe_dump(build(args), sort_keys=False)),
                       ("projects.yaml", PROJECTS)):
        target = config_dir / name
        if target.exists() and not args.force:
            print(f"kept existing {target}")
            continue
        target.write_text(text, encoding="utf-8")
        print(f"wrote {target}")
    from . import config
    cfg = config.load(config_dir)  # fail now rather than at daemon start
    print(f"config OK: model {cfg.default_model} at {cfg.models[cfg.default_model].base_url}, daemon port {cfg.port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
