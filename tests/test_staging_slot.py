"""Issue #129: the trusted-code staging smoke slot must never be able to reach production.

Three groups of contracts: which refs may be deployed at all, which locations and names the tower scripts are
allowed to touch, and what a reset deletes versus preserves.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
from scripts import resolve_staging_ref  # noqa: E402
from scripts.resolve_staging_ref import RefRejected, resolve  # noqa: E402

ROOT = Path(__file__).parents[1]
OPS = ROOT / "ops" / "harness"
WORKFLOW_TEXT = (ROOT / ".github" / "workflows" / "staging.yml").read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(WORKFLOW_TEXT)
# YAML 1.1 reads the `on:` key as the boolean True.
TRIGGERS = WORKFLOW["on"] if "on" in WORKFLOW else WORKFLOW[True]
DEPLOY = (OPS / "deploy-staging.ps1").read_text(encoding="utf-8")
RESET = (OPS / "reset-staging.ps1").read_text(encoding="utf-8")
RESTART = (OPS / "restart-daemon-staging.ps1").read_text(encoding="utf-8")
COMMON = (OPS / "staging-common.ps1").read_text(encoding="utf-8")
SUPERVISOR = (OPS / "run-daemon-staging.ps1").read_text(encoding="utf-8")
LOCAL_TEMPLATE = yaml.safe_load((OPS / "staging-harness.local.yaml").read_text(encoding="utf-8"))
PROFILE_OVERLAY = yaml.safe_load((OPS / "staging-profile.yaml").read_text(encoding="utf-8"))
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


def code_only(script: str) -> str:
    """The script without its help block and comment lines, for assertions about what it actually runs."""
    body = re.sub(r"<#.*?#>", "", script, flags=re.DOTALL)
    return "\n".join(line for line in body.splitlines() if not line.strip().startswith("#"))


SHA = "a" * 40
OTHER_SHA = "b" * 40


# --- allowed refs -----------------------------------------------------------------------------------------------


def api_for(responses: dict) -> object:
    def api(path: str) -> dict:
        if path not in responses:
            raise RefRejected(f"gh api {path} failed: Not Found")
        return responses[path]
    return api


def test_branch_resolves_to_an_immutable_commit():
    api = api_for({"repos/dflippojr/agent-harness/git/ref/heads/feat/84-chat-home":
                   {"object": {"sha": SHA, "type": "commit"}}})
    assert resolve(branch="feat/84-chat-home", api=api) == {
        "sha": SHA, "ref_label": "branch feat/84-chat-home", "reset_only": False}


def test_same_repository_pull_request_resolves_to_its_head_commit():
    api = api_for({"repos/dflippojr/agent-harness/pulls/131":
                   {"head": {"sha": SHA, "repo": {"full_name": "dflippojr/agent-harness"}}}})
    assert resolve(pr_number="131", api=api) == {
        "sha": SHA, "ref_label": "pull request #131", "reset_only": False}


def test_fork_pull_request_is_rejected_before_any_checkout():
    api = api_for({"repos/dflippojr/agent-harness/pulls/9":
                   {"head": {"sha": SHA, "repo": {"full_name": "someone/agent-harness"}}}})
    with pytest.raises(RefRejected, match="someone/agent-harness"):
        resolve(pr_number="9", api=api)


def test_pull_request_from_a_deleted_fork_is_rejected():
    api = api_for({"repos/dflippojr/agent-harness/pulls/9": {"head": {"sha": SHA, "repo": None}}})
    with pytest.raises(RefRejected, match="unknown"):
        resolve(pr_number="9", api=api)


def test_missing_branch_and_invalid_inputs_fail_closed():
    api = api_for({})
    with pytest.raises(RefRejected, match="Not Found"):
        resolve(branch="feat/never-pushed", api=api)
    with pytest.raises(RefRejected, match="exactly one"):
        resolve(api=api)
    with pytest.raises(RefRejected, match="exactly one"):
        resolve(branch="main", pr_number="131", api=api)
    with pytest.raises(RefRejected, match="must be a number"):
        resolve(pr_number="131; rm -rf /", api=api)
    with pytest.raises(RefRejected, match="usable ref name"):
        resolve(branch="--upload-pack=evil", api=api)
    with pytest.raises(RefRejected, match="usable ref name"):
        resolve(branch="feat/a..b", api=api)


def _bomb_api(path: str) -> dict:
    raise AssertionError(f"gh api must not be called for a rejected dispatch: {path}")


def test_repository_and_ref_payloads_fail_closed_before_any_gh_call():
    for repository in ("--help", "dflippojr/agent-harness/../../etc", "other/repo",
                       "/dflippojr/agent-harness", r"dflippojr\agent-harness", ""):
        with pytest.raises(RefRejected, match="allowed staging repository"):
            resolve(branch="main", repository=repository, api=_bomb_api)
    with pytest.raises(RefRejected, match="usable ref name"):
        resolve(branch=r"feat\escape", api=_bomb_api)
    with pytest.raises(RefRejected, match="usable ref name"):
        resolve(branch="/etc/passwd", api=_bomb_api)
    assert resolve_staging_ref.main(["--branch", "main", "--repository=--upload-pack=evil"]) == 1
    assert resolve_staging_ref.main(["--branch", "main", "--repository", "dflippojr/agent-harness/../other"]) == 1


def test_gh_api_rejects_unsafe_paths_before_subprocess(monkeypatch):
    called: list[object] = []

    def fake_run(*args, **kwargs):
        called.append(args)
        raise AssertionError(f"subprocess.run must not run: {args}")

    monkeypatch.setattr(resolve_staging_ref.subprocess, "run", fake_run)
    for path in ("--help", "-H", "repos/../etc/passwd", "/repos/dflippojr/agent-harness/pulls/1",
                 "repos/dflippojr/agent-harness/git/ref/heads/feat/a..b",
                 "repos/other/repo/pulls/1", "repos/dflippojr/agent-harness/pulls/1;id"):
        with pytest.raises(RefRejected, match="allowed staging lookup"):
            resolve_staging_ref.gh_api(path)
    assert called == []


def test_gh_api_passes_a_validated_path_after_a_double_dash(monkeypatch):
    def fake_run(argv, **kwargs):
        assert argv[:3] == ["gh", "api", "--"]
        assert argv[3] == "repos/dflippojr/agent-harness/git/ref/heads/main"
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(resolve_staging_ref.subprocess, "run", fake_run)
    assert resolve_staging_ref.gh_api("repos/dflippojr/agent-harness/git/ref/heads/main") == {"ok": True}


def test_branch_pointing_at_a_tag_object_or_nothing_is_rejected():
    with pytest.raises(RefRejected, match="commit SHA"):
        resolve(branch="main", api=api_for({"repos/dflippojr/agent-harness/git/ref/heads/main": {"object": {}}}))
    with pytest.raises(RefRejected, match="does not point at a commit"):
        resolve(branch="main", api=api_for({"repos/dflippojr/agent-harness/git/ref/heads/main":
                                           {"object": {"sha": SHA, "type": "tag"}}}))


def test_reset_is_dispatched_on_its_own():
    assert resolve(reset=True, api=api_for({})) == {"sha": "", "ref_label": "reset", "reset_only": True}
    with pytest.raises(RefRejected, match="on its own"):
        resolve(branch="main", reset=True, api=api_for({}))


def test_resolver_exits_nonzero_and_writes_outputs(tmp_path, monkeypatch, capsys):
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(resolve_staging_ref, "gh_api", api_for(
        {"repos/dflippojr/agent-harness/pulls/131":
         {"head": {"sha": SHA, "repo": {"full_name": "dflippojr/agent-harness"}}}}))
    assert resolve_staging_ref.main(["--pr-number", "131"]) == 0
    written = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert written == {"sha": SHA, "ref_label": "pull request #131", "reset_only": "false"}

    assert resolve_staging_ref.main(["--branch", "main", "--pr-number", "131"]) == 1
    assert "rejected" in capsys.readouterr().err


# --- workflow contracts -----------------------------------------------------------------------------------------


def test_dispatch_is_manual_only_with_the_three_documented_inputs():
    assert list(TRIGGERS) == ["workflow_dispatch"]
    assert sorted(TRIGGERS["workflow_dispatch"]["inputs"]) == ["branch", "pr_number", "reset"]


def test_staging_never_runs_on_the_production_or_ci_runners():
    stage = WORKFLOW["jobs"]["stage"]
    assert stage["runs-on"] == ["self-hosted", "Windows", "X64", "agent-harness-staging"]
    effective = "\n".join(line for line in WORKFLOW_TEXT.splitlines() if not line.strip().startswith("#"))
    assert "agent-harness-tower" not in effective
    assert "agent-harness-ci" not in effective
    assert stage["environment"] == "tower-staging"
    assert WORKFLOW["permissions"] == {"contents": "read"}


def test_resolve_job_can_read_pull_requests_without_write_scopes():
    assert WORKFLOW["permissions"] == {"contents": "read"}
    resolve_perms = WORKFLOW["jobs"]["resolve"]["permissions"]
    assert resolve_perms["pull-requests"] == "read"
    assert resolve_perms["contents"] == "read"
    for job in WORKFLOW["jobs"].values():
        for access in (job.get("permissions") or {}).values():
            assert "write" not in str(access)


def test_production_never_queues_behind_staging():
    production = yaml.safe_load((ROOT / ".github" / "workflows" / "ci-cd.yml").read_text(encoding="utf-8"))
    assert WORKFLOW["concurrency"]["group"] != production["concurrency"]["group"]
    assert WORKFLOW["concurrency"]["cancel-in-progress"] is False


def test_deploy_and_reset_abort_after_thirty_minutes():
    assert WORKFLOW["jobs"]["stage"]["timeout-minutes"] == 30
    assert WORKFLOW["jobs"]["resolve"]["timeout-minutes"] <= 30


def test_validation_happens_off_tower_before_the_candidate_is_checked_out():
    resolve_job, stage = WORKFLOW["jobs"]["resolve"], WORKFLOW["jobs"]["stage"]
    assert resolve_job["runs-on"] == "ubuntu-latest"
    assert stage["needs"] == "resolve"
    assert "refs/heads/main" in WORKFLOW_TEXT  # the control plane must be the trusted main workflow file
    checkouts = [step for step in stage["steps"] if "checkout" in str(step.get("uses", ""))]
    assert [step["with"]["ref"] for step in checkouts] == ["main"]
    # The deployer is always main's copy; the candidate's deploy scripts are never executed as the deployer.
    assert "deploy-ci.ps1" not in WORKFLOW_TEXT
    assert all("deploy-staging.ps1" in step.get("run", "") or "report-staging.ps1" in step.get("run", "")
               for step in stage["steps"] if "run" in step)


def test_one_slot_is_replaced_rather_than_a_second_slot_added():
    runs = [step.get("run", "") for step in WORKFLOW["jobs"]["stage"]["steps"]]
    deploys = [run for run in runs if "deploy-staging.ps1" in run]
    assert len(deploys) == 2  # exactly one reset path and one deploy path, mutually exclusive
    conditions = [step.get("if", "") for step in WORKFLOW["jobs"]["stage"]["steps"] if "deploy-staging.ps1" in
                  step.get("run", "")]
    assert "reset_only == 'true'" in conditions[0] and "reset_only != 'true'" in conditions[1]


# --- path, name, and tag guards ---------------------------------------------------------------------------------


def test_staging_locations_are_literal_and_distinct_from_production():
    for literal in ("D:\\Projects\\agent-harness-staging", "D:\\Agents\\harness-staging",
                    "D:\\Agents\\harness-staging\\venv", "D:\\Agents\\harness-staging\\logs",
                    "AgentHarness-Daemon-Staging", "8101", "8444"):
        assert literal in COMMON
    # No runtime derivation of a staging path from a production one.
    assert "-staging'" not in COMMON.split("$ProductionCheckout", 1)[1]
    assert "$StagingCheckout = 'D:\\Projects\\agent-harness-staging'" in COMMON


def test_every_staging_entry_point_asserts_it_is_not_production():
    for script in (DEPLOY, RESET, SUPERVISOR):
        assert "Assert-StagingTarget" in script
    for script in (DEPLOY, RESTART):
        assert "Assert-StagingTask" in script and "Assert-StagingPort" in script


def test_copy_item_passes_destination_on_the_same_line():
    # Sonar S8429 does not follow PowerShell backtick continuations; Destination must sit on the Copy-Item line.
    for line in code_only(DEPLOY).splitlines():
        if "Write-Host" in line:
            continue
        if re.search(r"\bCopy-Item\b", line):
            assert "-Destination" in line, line


def test_staging_scripts_never_name_production_locations_as_targets():
    for name, script in (("deploy", DEPLOY), ("reset", RESET), ("restart", RESTART), ("supervisor", SUPERVISOR)):
        assert "'D:\\Projects\\agent-harness'" not in script, name
        assert "'D:\\Agents\\harness'" not in script, name
        assert "AgentHarness-Daemon'" not in script, name
        assert "8100/health" not in script, name


def test_staging_never_touches_production_docker_tags_or_the_production_route():
    for script in (DEPLOY, RESET, RESTART, SUPERVISOR):
        code = code_only(script)
        # No Docker command at all in v1: nothing to pull, retag, or remove, so the stable tags cannot move.
        for command in ("docker", "127.0.0.1:8100", "https=443", "agent-harness-sandbox:py312",
                        "agent-harness-cli:1"):
            assert command not in code
    assert "agent-harness-sandbox:staging" in COMMON
    serve_staging = (ROOT / "ops" / "tailscale" / "serve-staging.ps1").read_text(encoding="utf-8")
    assert "--https=8444 http://127.0.0.1:8101" in serve_staging
    serve_code = code_only(serve_staging)
    assert "--https=443" not in serve_code and "8100" not in serve_code
    assert serve_code.count("$ts serve") == 2  # the staging mapping and a status print, nothing else


def test_stop_matchers_of_production_and_staging_are_disjoint():
    production = (OPS / "restart-daemon.ps1").read_text(encoding="utf-8")
    assert "CommandLine -notmatch 'harness-staging'" in production
    assert "AgentHarness-Daemon-Staging" not in production
    staging_matcher = RESTART.split("function Get-StagingDaemonProcesses", 1)[1].split("}", 1)[0]
    assert "harness-staging" in staging_matcher
    # Staging stop is the staging task and the staging port only, never "any python on 8100".
    assert "$StagingTaskName" in RESTART and "AgentHarness-Daemon'" not in RESTART
    assert f"127.0.0.1:$StagingPort/health" in RESTART


def test_supervisor_keeps_the_virtual_environment_pointer_inside_the_staging_data_root():
    assert "Test-PathInside $venv $StagingDataRoot" in SUPERVISOR
    assert "AgentHarnessDaemonStaging" in SUPERVISOR  # its own mutex, never production's
    assert "HARNESS_DATA_DIR=$StagingDataRoot" in SUPERVISOR
    assert "HARNESS_BUILD_COMMIT" in SUPERVISOR


# --- forced-off capabilities ------------------------------------------------------------------------------------


def test_trusted_profile_overlay_forces_every_optional_module_off():
    from harness.config import MODULE_NAMES

    # `service` would demand an enabled hosted backend; the smoke slot has none, so it is `full` with nothing on.
    assert PROFILE_OVERLAY["profile"] == "full"
    assert sorted(PROFILE_OVERLAY["modules"]) == sorted(MODULE_NAMES)
    assert not any(PROFILE_OVERLAY["modules"].values())


def test_candidate_yaml_alone_cannot_switch_a_forced_off_module_back_on(tmp_path):
    from harness import config

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    candidate = yaml.safe_load((ROOT / "config" / "harness.yaml").read_text(encoding="utf-8"))
    candidate["profile"] = "service"
    candidate["modules"] = {name: True for name in config.MODULE_NAMES if name != "image_edit"}
    (config_dir / "harness.yaml").write_text(yaml.safe_dump(candidate), encoding="utf-8")
    shutil.copy(OPS / "staging-harness.local.yaml", config_dir / "harness.local.yaml")
    shutil.copy(OPS / "staging-profile.yaml", config_dir / "profile.yaml")

    cfg = config.load(config_dir, tmp_path / "data")
    assert cfg.profile == "full"
    assert not any(cfg.module_effective(name) for name in config.MODULE_NAMES)
    assert [name for name, backend in cfg.backends.items() if backend.enabled] == []
    assert cfg.port == 8101 and cfg.host == "127.0.0.1"
    assert cfg.capabilities()["hosted_backends"] == []


def test_owner_overlay_template_admits_only_the_owner_and_holds_no_production_state():
    assert LOCAL_TEMPLATE["listen"] == {"host": "127.0.0.1", "port": 8101}
    assert LOCAL_TEMPLATE["allowed_logins"] == ["REPLACE_WITH_OWNER_TAILSCALE_LOGIN"]
    assert LOCAL_TEMPLATE["guests"] == []
    assert LOCAL_TEMPLATE["data_dir"] == "D:/Agents/harness-staging"
    assert LOCAL_TEMPLATE["repos_dir"].startswith("D:/Agents/harness-staging")
    assert LOCAL_TEMPLATE["sandbox"]["image"] == "agent-harness-sandbox:staging"
    for backend in LOCAL_TEMPLATE["backends"].values():
        assert backend == {"enabled": False, "volume": "", "api_key_file": ""}
    # The placeholder must fail the deploy closed rather than admitting every tailnet login.
    assert "REPLACE_WITH_OWNER_TAILSCALE_LOGIN" in DEPLOY


def test_health_reports_the_commit_the_process_is_running(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from harness import config, setup_config
    from harness.api import create_app
    from harness.manager import Manager

    assert setup_config.main(["--config-dir", str(tmp_path / "cfg"), "--data-dir", str(tmp_path / "data"),
                              "--profile", "service", "--model", "gpt-oss",
                              "--pause-flag", str(tmp_path / "paused")]) == 0
    monkeypatch.setenv("HARNESS_BUILD_COMMIT", SHA)
    with TestClient(create_app(Manager(config.load(tmp_path / "cfg", tmp_path / "data")))) as client:
        assert client.get("/health").json()["build"]["commit"] == SHA


# --- documentation ----------------------------------------------------------------------------------------------


def test_docs_describe_the_slot_the_scripts_actually_build():
    docs = (ROOT / "docs" / "CI-CD.md").read_text(encoding="utf-8")
    staging = docs.split("## Staging smoke slot", 1)[1].split("\n## Failure behavior", 1)[0]
    for fact in ("D:\\Projects\\agent-harness-staging", "D:\\Agents\\harness-staging", "127.0.0.1:8101",
                 ":8444", "AgentHarness-Daemon-Staging", "agent-harness-staging", "tower-staging",
                 "gh workflow run staging.yml -f pr_number=N", "gh workflow run staging.yml -f reset=true",
                 "30 minutes", "owner-token.txt", "allowed_logins", "Real-tower checklist"):
        assert fact in staging, fact
    assert "install-runner.ps1 -Token $token -Labels agent-harness-staging" in staging
    assert "serve-staging.ps1" in (ROOT / "ops" / "tailscale" / "serve.ps1").read_text(encoding="utf-8")


# --- token bootstrap and rotation -------------------------------------------------------------------------------


def test_staging_token_is_minted_in_staging_only_and_rotates(tmp_path):
    from scripts.staging_owner_token import mint

    data_dir, token_file = tmp_path / "harness-staging", tmp_path / "harness-staging" / "owner-token.txt"
    first_prefix = mint(data_dir, token_file)
    first = token_file.read_text(encoding="utf-8").strip()
    assert first.startswith("ho-") and first.startswith(first_prefix)

    second_prefix = mint(data_dir, token_file)
    second = token_file.read_text(encoding="utf-8").strip()
    assert second != first and second_prefix != first_prefix

    from harness.db import Database

    db = Database(data_dir / "harness.sqlite3")
    try:
        keys = db.list_api_keys()
    finally:
        db.close()
    assert [bool(key["revoked_at"]) for key in keys] == [True, False]
    assert all(key["kind"] == "owner" and key["scopes"] == "admin" for key in keys)


def test_token_minting_refuses_the_production_data_root():
    from scripts.staging_owner_token import mint

    with pytest.raises(SystemExit, match="production data root"):
        mint(Path("D:/Agents/harness"), Path("D:/Agents/harness/owner-token.txt"))
    with pytest.raises(SystemExit, match="production data root"):
        mint(Path("D:/Agents/harness/sub"), Path("D:/Agents/harness/sub/owner-token.txt"))


def test_token_minting_rejects_escaped_paths_before_any_write(tmp_path):
    from scripts.staging_owner_token import mint

    data_dir = tmp_path / "harness-staging"
    data_dir.mkdir()
    escaped = tmp_path / "escaped.txt"
    with pytest.raises(SystemExit, match="outside the staging data dir"):
        mint(data_dir, escaped)
    assert not escaped.exists()
    assert not (data_dir / "harness.sqlite3").exists()

    with pytest.raises(SystemExit, match=r"\.\."):
        mint(data_dir, data_dir / ".." / "escaped.txt")
    with pytest.raises(SystemExit, match=r"\.\."):
        mint(data_dir / ".." / "harness", data_dir / "owner-token.txt")
    with pytest.raises(SystemExit, match="starts with '-'"):
        mint(data_dir, Path("-owner-token.txt"))
    with pytest.raises(SystemExit, match="production data root"):
        mint(data_dir, Path("D:/Agents/harness/owner-token.txt"))
    with pytest.raises(SystemExit, match="starts with '-'"):
        mint(data_dir, data_dir / "owner-token.txt", Path("-rf"))
    with pytest.raises(SystemExit, match="outside the staging data dir"):
        mint(data_dir, data_dir / "other.txt")
    with pytest.raises(SystemExit, match="outside the staging data dir"):
        mint(data_dir, data_dir / "subdir" / "owner-token.txt")
    with pytest.raises(SystemExit, match="safe absolute path"):
        mint(data_dir / "not a dir", (data_dir / "not a dir") / "owner-token.txt")
    assert not escaped.exists()
    assert not (data_dir / "harness.sqlite3").exists()
    assert not (data_dir / "other.txt").exists()
    assert not Path("-owner-token.txt").exists()


# --- reset boundaries -------------------------------------------------------------------------------------------


def test_reset_preserves_exactly_the_overlay_logs_and_virtual_environment():
    preserved = RESET.split("$StagingPreservedEntries = @(", 1)[1].split(")", 1)[0]
    assert sorted(entry.strip().strip("'") for entry in preserved.split(",")) == [
        "harness.local.yaml", "logs", "venv"]
    assert "Remove-Item" in RESET and "docker rmi" not in RESET
    assert "seed" in RESET.lower()  # the documented no-seed promise stays next to the code


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell is not installed")
def test_reset_dry_run_deletes_candidate_state_and_keeps_the_owner_overlay(tmp_path):
    data_root = tmp_path / "harness-staging"
    (data_root / "logs").mkdir(parents=True)
    (data_root / "venv" / "Scripts").mkdir(parents=True)
    (data_root / "workspaces").mkdir()
    for name in ("harness.sqlite3", "harness.sqlite3-wal", "harness.local.yaml", "projects.yaml",
                 "deployed-sha.txt", "owner-token.txt"):
        (data_root / name).write_text("x", encoding="utf-8")

    result = run_powershell([
        "-Command",
        f". '{OPS / 'reset-staging.ps1'}'; $StagingDataRoot = '{data_root}'; Reset-StagingData",
    ])
    assert result.returncode == 0, output(result)
    assert sorted(p.name for p in data_root.iterdir()) == ["harness.local.yaml", "logs", "venv"]
    assert "staging tokens rotated" in result.stdout


# --- deploy plan ------------------------------------------------------------------------------------------------


def run_powershell(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           *arguments], cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)


def output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def run_deploy(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    data_root = tmp_path / "harness-staging"
    data_root.mkdir(parents=True, exist_ok=True)
    local = data_root / "harness.local.yaml"
    if not local.exists():
        text = (OPS / "staging-harness.local.yaml").read_text(encoding="utf-8")
        local.write_text(text.replace("REPLACE_WITH_OWNER_TAILSCALE_LOGIN", "owner@example.com"), encoding="utf-8")
    return run_powershell(["-File", str(OPS / "deploy-staging.ps1"), "-DryRun",
                           "-StagingDir", str(tmp_path / "agent-harness-staging"),
                           "-StagingDataDir", str(data_root), *extra])


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell is not installed")
def test_deploy_dry_run_plans_stop_clean_checkout_overlay_start(tmp_path):
    result = run_deploy(tmp_path, "-Commit", SHA)
    assert result.returncode == 0, output(result)
    plan = result.stdout
    order = [plan.index(marker) for marker in (
        "[staging] guards passed", "[staging] slot stopped", "[staging] slot clean",
        f"[staging] checkout at {SHA}", "[staging] dependencies installed",
        "[staging] overlay applied", "[staging] daemon started and healthy", f"Staged {SHA}")]
    assert order == sorted(order)
    assert "profile.yaml" in plan and "harness.local.yaml" in plan
    assert "127.0.0.1:8101" in plan and "tailnet :8444" in plan
    assert "docker" not in plan.lower()


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell is not installed")
def test_deploy_refuses_production_locations_and_bad_dispatches(tmp_path):
    checkout, data_root = str(tmp_path / "agent-harness-staging"), str(tmp_path / "harness-staging")
    for arguments, expected in (
        (("-Commit", SHA, "-Reset", "-StagingDir", checkout, "-StagingDataDir", data_root), "not both"),
        (("-StagingDir", checkout, "-StagingDataDir", data_root), "-Commit <40-hex sha> or -Reset"),
        (("-Commit", "main", "-StagingDir", checkout, "-StagingDataDir", data_root), "does not match"),
        (("-Commit", SHA, "-StagingDir", checkout, "-StagingDataDir", "D:\\Agents\\harness"),
         "production location"),
        (("-Commit", SHA, "-StagingDir", "D:\\Projects\\agent-harness", "-StagingDataDir", data_root),
         "production location"),
        (("-Commit", SHA, "-StagingDir", "agent-harness-staging", "-StagingDataDir", data_root), "absolute path"),
    ):
        result = run_powershell(["-File", str(OPS / "deploy-staging.ps1"), "-DryRun", *arguments])
        assert result.returncode != 0, (arguments, output(result))
        assert expected in output(result), (arguments, output(result))


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell is not installed")
def test_deploy_reset_stops_the_slot_and_deploys_nothing(tmp_path):
    result = run_deploy(tmp_path, "-Reset")
    assert result.returncode == 0, output(result)
    assert "[staging] reset complete" in result.stdout
    assert "checkout at" not in result.stdout and "daemon started" not in result.stdout
    assert "Production checkout" in result.stdout


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell is not installed")
def test_staging_overlay_referencing_production_fails_the_deploy_closed(tmp_path):
    data_root = tmp_path / "harness-staging"
    data_root.mkdir(parents=True)
    (data_root / "harness.local.yaml").write_text(
        "listen:\n  port: 8101\nallowed_logins: [owner@example.com]\ndata_dir: D:/Agents/harness/\n",
        encoding="utf-8")
    result = run_powershell(["-File", str(OPS / "deploy-staging.ps1"), "-DryRun", "-Commit", SHA,
                             "-StagingDir", str(tmp_path / "agent-harness-staging"),
                             "-StagingDataDir", str(data_root)])
    assert result.returncode != 0, output(result)
    assert "references production" in output(result), output(result)


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell is not installed")
def test_report_names_the_running_commit_and_the_staging_url(tmp_path):
    data_root = tmp_path / "harness-staging"
    data_root.mkdir(parents=True)
    (data_root / "deployed-sha.txt").write_text(SHA, encoding="utf-8")
    (data_root / "harness.local.yaml").write_text(
        'public_url: "https://tower.example-tailnet.ts.net:8444"\n', encoding="utf-8")
    summary = tmp_path / "summary.md"
    env = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary)}
    result = subprocess.run(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(OPS / "report-staging.ps1"), "-RefLabel", "pull request #131", "-Commit", OTHER_SHA,
         "-StagingDataDir", str(data_root)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, output(result)
    assert SHA in result.stdout and "https://tower.example-tailnet.ts.net:8444/" in result.stdout
    assert OTHER_SHA in result.stdout  # a moved head is reported, not chased
    assert "8100" in summary.read_text(encoding="utf-8")
