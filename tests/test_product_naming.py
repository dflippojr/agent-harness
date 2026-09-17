"""Issue #91: canonical product names stay consistent across active surfaces."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_web_metadata_and_compatibility_identifiers():
    web = ROOT / "harness" / "web"
    manifest = json.loads((web / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["name"] == "Agent Harness Web"
    assert manifest["short_name"] == "Harness"

    index = (web / "index.html").read_text(encoding="utf-8")
    assert "<title>Agent Harness Web</title>" in index
    assert 'apple-mobile-web-app-title" content="Harness"' in index

    transport = (web / "client.mjs").read_text(encoding="utf-8")
    assert '"harness.daemonUrl"' in transport
    assert '"harness.ownerToken"' in transport
    assert "Agent Harness Server URL" in transport


def test_readme_glossary_defines_every_first_party_surface():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in (
        "Agent Harness Server",
        "Agent Harness Web",
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

    assert guide.startswith("# Agent Harness Web\n")
    assert "Agent Harness Server URL" in guide
    assert "Web connections" in guide
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
