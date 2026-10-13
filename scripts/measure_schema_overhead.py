"""Measure the per-turn tool-schema overhead of native sessions for each toolkit set (#645).

Builds the schema lists the runner sends from the checked-in config (no daemon, no model, no network) and prints a
Markdown table of schema count, characters and estimated tokens, as a share of each configured model's window. Tokens
use the runner's own estimate (`efficiency.schema_overhead`, characters / chars_per_token, 3.0 until a session
calibrates it). Run from the repository root: python scripts/measure_schema_overhead.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import config as config_mod
from harness import delegate_edit, efficiency
from harness.mcp_server import SPLIT_TOOLS
from harness.runner import READ_ARTIFACT_SCHEMA
from harness.tools import tool_schemas
from harness.web_tools import schemas as web_schemas
from harness_modules.homelab.service import schemas as homelab_schemas
from harness_modules.images.service import schemas as image_schemas
from harness_modules.memory_library.service import schemas as memory_schemas
from harness_modules.remote_control.service import RemoteControl
from harness_modules.search.service import schemas as search_schemas
from harness_modules.skills.service import schemas as skill_schemas


class _Projects:
    """Stand-in for RemoteControl: the schema only needs the eligible project names."""

    def eligible(self) -> list[str]:
        return ["agent-harness", "agent-loop", "agent-memory-library"]


def kits(cfg) -> dict[str, list[dict]]:
    limits = cfg.tool_output
    core = tool_schemas(limits.read_file_lines, search_matches=limits.search_matches,
                        read_file_lines_max=limits.read_file_lines_max,
                        search_matches_max=limits.search_matches_max,
                        run_shell_chars=limits.run_shell_chars, run_shell_chars_max=limits.run_shell_chars_max)
    memory_cfg = config_mod.MemoryLibraryConfig(**{**vars(cfg.memory_library), "writes": True})
    return {
        "core": core,
        "split": [s for s in core if s["function"]["name"] in SPLIT_TOOLS],
        "read_artifact": [READ_ARTIFACT_SCHEMA],
        "delegate": [delegate_edit.PROPOSE_SCHEMA, delegate_edit.APPLY_SCHEMA],
        "web": web_schemas(cfg.web),
        "memory": memory_schemas(memory_cfg),
        "session_search": search_schemas(),
        "homelab": homelab_schemas(cfg.homelab),
        "images": image_schemas(),
        "skills": skill_schemas(),
        "remote_control": RemoteControl.schemas(cast(RemoteControl, _Projects())),
    }


PROFILES = [
    ("Chat session (web)", ["web"]),
    ("Hosted split mode, harness MCP tools only (+ web, session search)", ["split", "web", "session_search"]),
    ("Core tools only", ["core"]),
    ("Default tower agent (core, read_artifact, delegate pair)", ["core", "read_artifact", "delegate"]),
    ("+ web", ["core", "read_artifact", "delegate", "web"]),
    ("+ web, session search", ["core", "read_artifact", "delegate", "web", "session_search"]),
    ("+ web, session search, memory (writes on)",
     ["core", "read_artifact", "delegate", "web", "session_search", "memory"]),
    ("Every built-in toolkit", ["core", "read_artifact", "delegate", "web", "session_search", "memory", "homelab",
                                "images", "skills", "remote_control"]),
]


def main() -> None:
    cfg = config_mod.load()
    windows = sorted({m.context_tokens for m in cfg.models.values()})
    by_kit = kits(cfg)
    print("| Toolkit | Schemas | Characters | Est. tokens |")
    print("| --- | ---: | ---: | ---: |")
    for name, schemas in by_kit.items():
        cost = efficiency.schema_overhead(schemas, 3.0)
        print(f"| {name} | {cost['schema_count']} | {len(json.dumps(schemas))} | {cost['schema_tokens']} |")
    print()
    header = " | ".join(f"% of {w // 1024}K" for w in windows)
    print(f"| Profile | Schemas | Est. tokens | {header} |")
    print("| --- | ---: | ---: |" + " ---: |" * len(windows))
    for label, names in PROFILES:
        schemas = [s for name in names for s in by_kit[name]]
        cost = efficiency.schema_overhead(schemas, 3.0)
        shares = " | ".join(f"{100 * cost['schema_tokens'] / w:.1f}%" for w in windows)
        print(f"| {label} | {cost['schema_count']} | {cost['schema_tokens']} | {shares} |")


if __name__ == "__main__":
    main()
