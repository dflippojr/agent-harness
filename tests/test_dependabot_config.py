"""Scheduled updates cover the repository's external dependency manifests."""

from pathlib import Path

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
        "pip": {"/", "/tools/openapi"},
        "docker": {"/sandbox", "/ops/egress", "/reference/hermes", "/reference/openclaw",
                   "/reference/opencode", "/reference/openhands"},
        "docker-compose": {"/ops/observability"},
        "github-actions": {"/"},
    }
    for directory in directories["docker"]:
        assert (ROOT / directory.lstrip("/") / "Dockerfile").is_file()
    for name in ("requirements.txt", "requirements-repomap.txt", "requirements-telemetry.txt",
                 "tools/openapi/requirements.txt"):
        assert (ROOT / name).is_file()


def test_fastapi_bump_moves_the_server_floor_and_the_exporter_pin_together():
    # One PR across both directories, or the exporter's exact pin rejects the raised floor (PR #820).
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    pip = next(update for update in config["updates"] if update["package-ecosystem"] == "pip")
    assert [group.get("group-by") for group in pip["groups"].values()] == ["dependency-name"]

    def requirement(path):
        lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
        return next(line for line in lines if line.startswith("fastapi"))

    floor = requirement("requirements.txt").removeprefix("fastapi>=")
    assert requirement("tools/openapi/requirements.txt") == f"fastapi=={floor}"


def test_nonstandard_container_manifest_names_are_discoverable():
    # Discovery must not require duplicate manifests: the real, nonstandard filenames have to exist.
    assert (ROOT / "sandbox/cli.Dockerfile").is_file()
    compose = yaml.safe_load((ROOT / "ops/observability/docker-compose.tempo.yml").read_text(encoding="utf-8"))
    assert compose["services"]["tempo"]["image"].startswith("grafana/tempo:")
