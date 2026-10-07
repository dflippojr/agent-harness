"""Scheduled updates cover the repository's external dependency manifests."""

from pathlib import Path
import re

import yaml


ROOT = Path(__file__).parents[1]


def test_scheduled_ecosystems_and_manifest_directories():
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    assert config["version"] == 2
    directories = {}
    for update in config["updates"]:
        assert update["schedule"]["interval"] == "weekly"
        ecosystem = update["package-ecosystem"]
        assert ecosystem not in directories
        directories[ecosystem] = set(update.get("directories", [update.get("directory")]))
    assert directories == {
        "pip": {"/"},
        "docker": {"/sandbox", "/ops/egress", "/reference/hermes", "/reference/openclaw",
                   "/reference/opencode", "/reference/openhands"},
        "docker-compose": {"/ops/observability"},
        "github-actions": {"/"},
    }
    for directory in directories["docker"]:
        assert (ROOT / directory.lstrip("/") / "Dockerfile").is_file()
    for name in ("requirements.txt", "requirements-repomap.txt", "requirements-telemetry.txt"):
        assert (ROOT / name).is_file()


def test_nonstandard_container_manifest_names_are_discoverable():
    # Filename patterns from dependabot-core's Docker and Docker Compose fetchers.
    # Keep the real filenames covered: discovery must not require duplicate manifests.
    assert re.search(r"dockerfile|containerfile", "cli.Dockerfile", re.I)
    assert (ROOT / "sandbox/cli.Dockerfile").is_file()
    assert re.search(r"(docker-)?compose(-[\w]+)?(?>\.[\w-]+)?\.ya?ml", "docker-compose.tempo.yml", re.I)
    compose = yaml.safe_load((ROOT / "ops/observability/docker-compose.tempo.yml").read_text(encoding="utf-8"))
    assert compose["services"]["tempo"]["image"].startswith("grafana/tempo:")
