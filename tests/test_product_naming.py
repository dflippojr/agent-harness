"""Issues #91 and #334: canonical product names stay consistent across active surfaces."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_web_metadata_and_compatibility_identifiers():
    web = ROOT / "stables" / "public"
    manifest = json.loads((web / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["name"] == "Stables"
    assert manifest["short_name"] == "Stables"

    index = (web / "index.html").read_text(encoding="utf-8")
    assert "<title>Stables</title>" in index
    assert 'apple-mobile-web-app-title" content="Stables"' in index

    transport = (web / "client.mjs").read_text(encoding="utf-8")
    assert '"harness.daemonUrl"' in transport
    assert '"harness.ownerToken"' in transport
    assert "Agent Harness Server URL" in transport
    assert "export const ControlCenterClient = AgentHarnessWebClient;" in transport
    assert "export const controlCenter = agentHarnessWeb;" in transport


def test_readme_glossary_defines_every_first_party_surface():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in (
        "Agent Harness Server",
        "Stables",
        "Agent Harness CLI",
        "Agent Harness Runner",
        "Agent Harness SDK",
        "Agent Harness App",
        "Agent Harness for Mac",
    ):
        assert f"**{name}**" in readme
    assert "[`docs/web.md`](docs/web.md)" in readme
    assert "docs/control-center.md" not in readme


def test_web_guide_is_canonical_and_old_path_is_a_short_pointer():
    guide = (ROOT / "docs" / "web.md").read_text(encoding="utf-8")
    pointer = (ROOT / "docs" / "control-center.md").read_text(encoding="utf-8")

    assert guide.startswith("# Stables\n")
    assert "formerly Agent Harness Web" in guide
    assert "Agent Harness Server URL" in guide
    assert "harness keys create" in guide  # Web connection tokens come from the Hub or the CLI (#545)
    assert "harness.*" in guide
    assert "remain valid and visible" in guide

    assert "[`web.md`](web.md)" in pointer
    assert len(pointer.splitlines()) <= 8


def test_active_docs_link_to_the_canonical_web_guide():
    active = [
        ROOT / "README.md",
        ROOT / "docs" / "INSTALL.md",
        ROOT / "docs" / "admin-api.md",
        ROOT / "docs" / "app-api.md",
        ROOT / "docs" / "mac-client.md",
        ROOT / "docs" / "service-profile.md",
        ROOT / "docs" / "web.md",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in active)
    assert "(control-center.md)" not in combined
    assert "docs/control-center.md" not in combined
    assert "(web.md)" in combined


def test_server_and_living_docs_call_the_client_stables():
    """#334 stage (d): the Server, its config and living docs say Stables. History keeps the old name: study and
    results docs, API changelog rows, the rename notes, and the migration that renames the registry row."""
    import re
    import subprocess

    history = re.compile(r"docs/(.*-study|phase\w*-results|issue-\d+-\w*results|control-center)\.md$")
    allowed_files = {"harness/migrations/0063_stables_name.py"}
    allowed_lines = re.compile(r"^\| \d+\.\d+ \||formerly Agent Harness Web|from \"Agent Harness Web\" to \"Stables\"")
    tracked = subprocess.run(["git", "ls-files", "harness", "harness_modules", "sdk", "config", "ops", "install",
                              "macrunner", "stables", "tools", "docs", "README.md", "AGENTS.md"],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    stale = []
    for name in tracked:
        if name in allowed_files or history.search(name) or not name.endswith((".py", ".md", ".mjs", ".js", ".yaml",
                                                                                ".html", ".ps1", ".sh")):
            continue
        for number, line in enumerate((ROOT / name).read_text(encoding="utf-8").splitlines(), 1):
            if "Agent Harness Web" in line and not allowed_lines.search(line):
                stale.append(f"{name}:{number}")
    assert not stale, stale
