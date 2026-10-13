"""Per-session tool profiles for hosted CLI backends (#638).

`default` is the tool set each backend has always had. `bash-only` leaves the model a shell and nothing else for
workspace work, after arXiv 2609.20804 Finding 5: strong bash-capable models did as well or better at lower cost, and
weaker ones lost ground (docs/harness-design-gap-report.md, Section 4.4). It is for strong hosted models only, so it is
never offered for native local sessions or for a `kind: cli` backend, which runs the local model.

- Claude Code, builtin mode: `--tools Bash`.
- Claude Code and Codex, split mode: the harness MCP server serves `run_shell` alone of `SPLIT_TOOLS`; sandbox, policy,
  grants and approvals are unchanged.
- Codex builtin mode, Cursor, local and `kind: cli` backends: unsupported, and asking for it is an error.
"""

from __future__ import annotations

from .config import BackendConfig

DEFAULT = "default"
BASH_ONLY = "bash-only"
TOOL_PROFILES = (DEFAULT, BASH_ONLY)
UNSUPPORTED = "tool_profile_unsupported"
# What split mode serves under bash-only: the shell, and none of the file tools.
BASH_ONLY_SPLIT_TOOLS = frozenset({"run_shell"})


def supported(name: str, backend: BackendConfig | None) -> tuple[str, ...]:
    """The profiles a backend can run, in display order."""
    if backend is None or backend.is_cli or name not in ("claude", "codex"):
        return (DEFAULT,)
    if name == "codex" and backend.tool_mode != "split":
        return (DEFAULT,)  # environments: [] would take the shell away along with apply_patch
    return TOOL_PROFILES


def refusal(name: str, backend: BackendConfig | None, profile: str, kind: str = "agent") -> str:
    """Why `profile` can't run on this backend and session kind, or "" when it can."""
    if profile not in TOOL_PROFILES:
        return f"tool_profile must be one of {', '.join(TOOL_PROFILES)}"
    if profile == DEFAULT:
        return ""
    if kind != "agent":
        return f"tool_profile {profile} is for agent sessions only"
    if profile in supported(name, backend):
        return ""
    if name == "local" or (backend is not None and backend.is_cli):
        return (f"tool_profile {profile} is for strong hosted models only; backend {name!r} runs the local model, "
                "where the predefined tools do better")
    if name == "codex":
        return (f"tool_profile {profile} needs Codex in split mode (backends.codex.tool_mode: split); builtin Codex "
                "can't keep its shell without its editor")
    return f"backend {name!r} can't restrict its tools, so it can't run tool_profile {profile}"
