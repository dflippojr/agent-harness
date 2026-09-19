"""Pure-logic and safety contracts for the automated review runner."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "ops" / "review" / "run-review.ps1"
WORKFLOW = ROOT / ".github" / "workflows" / "review.yml"
CI_DOCS = ROOT / "docs" / "CI-CD.md"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


def run_powershell(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is not installed")
    wrapper = tmp_path / "review-test.ps1"
    script_path = str(SCRIPT).replace("'", "''")
    wrapper.write_text(
        f". '{script_path}'\n{body}\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def test_backend_selection_priority_and_validation(tmp_path):
    result = run_powershell(
        tmp_path,
        r"""
$value = [ordered]@{
    default = @((Resolve-ReviewBackends -RequestedBackend '' -ConfiguredBackends ''))
    configured = @((Resolve-ReviewBackends -RequestedBackend 'auto' -ConfiguredBackends ' cursor, codex,CLAUDE,codex '))
    explicit = @((Resolve-ReviewBackends -RequestedBackend 'claude' -ConfiguredBackends 'cursor,codex'))
}
$value | ConvertTo-Json -Compress
""",
    )
    assert result.returncode == 0, output(result)
    value = json.loads(result.stdout.strip())
    assert value == {
        "default": ["codex", "claude", "cursor"],
        "configured": ["cursor", "codex", "claude"],
        "explicit": ["claude"],
    }

    invalid = run_powershell(
        tmp_path,
        "Resolve-ReviewBackends -RequestedBackend auto -ConfiguredBackends 'codex,unknown'",
    )
    assert invalid.returncode != 0
    assert "unsupported review backend 'unknown'" in output(invalid)


@pytest.mark.parametrize(
    "message",
    [
        "RESOURCE_EXHAUSTED",
        "rate limit reached",
        "quota has been exceeded",
        "HTTP 429: too many requests",
        "usage limit reached",
        "credit balance is too low",
    ],
)
def test_rate_limit_signatures(tmp_path, message):
    escaped = message.replace("'", "''")
    result = run_powershell(tmp_path, f"Test-ReviewRateLimit -Text '{escaped}'")
    assert result.returncode == 0, output(result)
    assert result.stdout.strip() == "True"


def test_backend_commands_enforce_read_only_review_access(tmp_path):
    cursor_base = tmp_path / "cursor-agent"
    version = cursor_base / "versions" / "2026.09.18"
    version.mkdir(parents=True)
    (version / "node.exe").touch()
    (version / "index.js").touch()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    workspace = tmp_path / "checkout"
    workspace.mkdir()

    result = run_powershell(
        tmp_path,
        f"""
$commands = @(
    Get-ReviewBackendCommand -Backend cursor -Workspace '{workspace}' -Prompt prompt -ScratchDirectory '{scratch}' -CursorBase '{cursor_base}' -WindowsPlatform $true
    Get-ReviewBackendCommand -Backend codex -Workspace '{workspace}' -Prompt prompt -ScratchDirectory '{scratch}'
    Get-ReviewBackendCommand -Backend claude -Workspace '{workspace}' -Prompt prompt -ScratchDirectory '{scratch}'
)
$commands | Select-Object Backend,FilePath,Arguments,InputText,ResultPath | ConvertTo-Json -Depth 4 -Compress
""",
    )
    assert result.returncode == 0, output(result)
    commands = {item["Backend"]: item for item in json.loads(result.stdout.strip())}

    cursor_args = commands["cursor"]["Arguments"]
    assert cursor_args == [
        str(version / "index.js"),
        "-p",
        "--output-format",
        "text",
        "--mode",
        "ask",
        "--workspace",
        str(workspace),
        "prompt",
    ]

    codex_args = commands["codex"]["Arguments"]
    result_path = str(scratch / "codex-review-output.md")
    assert codex_args == [
        "exec",
        "--sandbox",
        "read-only",
        "--cd",
        str(workspace),
        "--ephemeral",
        "--ignore-user-config",
        "--color",
        "never",
        "--output-last-message",
        result_path,
        "-",
    ]
    assert commands["codex"]["ResultPath"] == result_path

    claude_args = commands["claude"]["Arguments"]
    assert "Read,Grep,Glob,Bash" in claude_args
    assert "Read,Grep,Glob,Bash(gh pr diff:*)" in claude_args
    assert not {"Edit", "Write", "NotebookEdit"}.intersection(claude_args)
    assert "--strict-mcp-config" in claude_args


def test_cursor_enables_sandbox_off_windows(tmp_path):
    cursor_base = tmp_path / "cursor-agent"
    version = cursor_base / "versions" / "2026.09.18"
    version.mkdir(parents=True)
    (version / "node.exe").touch()
    (version / "index.js").touch()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    workspace = tmp_path / "checkout"
    workspace.mkdir()

    result = run_powershell(
        tmp_path,
        f"""
$command = Get-ReviewBackendCommand -Backend cursor -Workspace '{workspace}' -Prompt prompt -ScratchDirectory '{scratch}' -CursorBase '{cursor_base}' -WindowsPlatform $false
$command.Arguments | ConvertTo-Json -Compress
""",
    )
    assert result.returncode == 0, output(result)
    assert json.loads(result.stdout.strip()) == [
        str(version / "index.js"),
        "-p",
        "--output-format",
        "text",
        "--mode",
        "ask",
        "--sandbox",
        "enabled",
        "--workspace",
        str(workspace),
        "prompt",
    ]


def test_fallback_uses_claude_after_codex_failure_and_footer_names_it(tmp_path):
    output_path = tmp_path / "review.md"
    result = run_powershell(
        tmp_path,
        f"""
$script:calls = New-Object System.Collections.Generic.List[string]
$runner = {{
    param($command)
    $script:calls.Add($command.Backend)
    if ($command.Backend -eq 'codex') {{
        return [pscustomobject]@{{ ExitCode = 17; Stdout = ''; Stderr = 'RESOURCE_EXHAUSTED'; Model = $null }}
    }}
    return [pscustomobject]@{{ ExitCode = 0; Stdout = '- src/app.py:12: real bug'; Stderr = ''; Model = $null }}
}}
$backends = @(Resolve-ReviewBackends -RequestedBackend auto -ConfiguredBackends 'codex,claude,cursor')
$result = Invoke-ReviewFallback -Backends $backends -Workspace '{tmp_path}' -Prompt prompt -ScratchDirectory '{tmp_path}' -Runner $runner
Write-ReviewResult -Result $result -OutputPath '{output_path}'
[ordered]@{{ backend = $result.Backend; calls = @($script:calls); body = [string](Get-Content -Raw -LiteralPath '{output_path}') }} | ConvertTo-Json -Compress
""",
    )
    assert result.returncode == 0, output(result)
    value = json.loads(result.stdout.strip().splitlines()[-1])
    assert value["backend"] == "claude"
    assert value["calls"] == ["codex", "claude"]
    assert "Automated review backend: **claude**." in value["body"]


def test_process_launcher_captures_stdout_stderr_and_exit_code(tmp_path):
    fake_backend = tmp_path / "fake-backend.ps1"
    fake_backend.write_text(
        '[Console]::Out.Write("review")\n[Console]::Error.Write("diagnostic")\nexit 7\n',
        encoding="utf-8",
    )
    result = run_powershell(
        tmp_path,
        f"""
$command = [pscustomobject]@{{
    Backend = 'fake'
    FilePath = (Get-Command powershell.exe).Source
    Arguments = @('-NoLogo', '-NoProfile', '-File', '{fake_backend}')
    InputText = $null
    WorkingDirectory = '{tmp_path}'
    ResultPath = $null
    Model = $null
}}
Invoke-ReviewBackendProcess -Command $command -ScratchDirectory '{tmp_path}' | ConvertTo-Json -Compress
""",
    )
    assert result.returncode == 0, output(result)
    value = json.loads(result.stdout.strip())
    assert value["ExitCode"] == 7
    assert value["Stdout"].strip() == "review"
    assert "diagnostic" in value["Stderr"]


def test_shared_prompt_points_read_only_backends_at_prefetched_diff(tmp_path):
    diff_path = tmp_path / ".automated-review-diff-42.patch"
    result = run_powershell(
        tmp_path,
        f"Add-ReviewDiffContext -Prompt 'original shared prompt' -DiffPath '{diff_path}'",
    )
    assert result.returncode == 0, output(result)
    assert result.stdout.startswith("original shared prompt")
    assert "exact gh pr diff output" in result.stdout
    assert diff_path.name in result.stdout
    assert str(tmp_path) not in result.stdout


def test_empty_and_rate_limited_successes_fall_through(tmp_path):
    version = tmp_path / "versions" / "2026.09.18"
    version.mkdir(parents=True)
    (version / "node.exe").touch()
    (version / "index.js").touch()
    result = run_powershell(
        tmp_path,
        f"""
$script:index = 0
$runner = {{
    param($command)
    $script:index++
    if ($script:index -eq 1) {{ return [pscustomobject]@{{ ExitCode = 0; Stdout = '   '; Stderr = ''; Model = $null }} }}
    if ($script:index -eq 2) {{ return [pscustomobject]@{{ ExitCode = 0; Stdout = 'quota exceeded'; Stderr = ''; Model = $null }} }}
    return [pscustomobject]@{{ ExitCode = 0; Stdout = 'clean review'; Stderr = ''; Model = $null }}
}}
$result = Invoke-ReviewFallback -Backends @('codex','claude','cursor') -Workspace '{tmp_path}' -Prompt prompt -ScratchDirectory '{tmp_path}' -Runner $runner -CursorBase '{tmp_path}'
$result | ConvertTo-Json -Compress
""",
    )
    assert result.returncode == 0, output(result)
    value = json.loads(result.stdout.strip().splitlines()[-1])
    assert value["Backend"] == "cursor"
    assert value["Output"] == "clean review"
    assert "empty output" in result.stdout
    assert "rate limit or quota response" in result.stdout


def test_long_successful_review_can_discuss_rate_limits_and_quotas(tmp_path):
    review = (
        "This review explains why matching rate limit text such as quota exceeded "
        "inside a pull request is not evidence of a provider failure. "
    ) * 8
    escaped_review = review.replace("'", "''")
    result = run_powershell(
        tmp_path,
        f"""
$script:calls = New-Object System.Collections.Generic.List[string]
$runner = {{
    param($command)
    $script:calls.Add($command.Backend)
    return [pscustomobject]@{{ ExitCode = 0; Stdout = '{escaped_review}'; Stderr = ''; Model = $null }}
}}
$result = Invoke-ReviewFallback -Backends @('codex','claude') -Workspace '{tmp_path}' -Prompt prompt -ScratchDirectory '{tmp_path}' -Runner $runner
[ordered]@{{ backend = $result.Backend; calls = @($script:calls); length = $result.Output.Length }} | ConvertTo-Json -Compress
""",
    )
    assert result.returncode == 0, output(result)
    value = json.loads(result.stdout.strip().splitlines()[-1])
    assert value["backend"] == "codex"
    assert value["calls"] == ["codex"]
    assert value["length"] >= 600


@pytest.mark.parametrize(
    ("first_stdout", "first_stderr"),
    [
        ("rate limit exceeded", ""),
        ("brief provider response", "RESOURCE_EXHAUSTED"),
    ],
)
def test_short_rate_limit_response_triggers_fallback(tmp_path, first_stdout, first_stderr):
    result = run_powershell(
        tmp_path,
        f"""
$script:index = 0
$runner = {{
    param($command)
    $script:index++
    if ($script:index -eq 1) {{
        return [pscustomobject]@{{ ExitCode = 0; Stdout = '{first_stdout}'; Stderr = '{first_stderr}'; Model = $null }}
    }}
    return [pscustomobject]@{{ ExitCode = 0; Stdout = 'clean review'; Stderr = ''; Model = $null }}
}}
$result = Invoke-ReviewFallback -Backends @('codex','claude') -Workspace '{tmp_path}' -Prompt prompt -ScratchDirectory '{tmp_path}' -Runner $runner
$result | ConvertTo-Json -Compress
""",
    )
    assert result.returncode == 0, output(result)
    value = json.loads(result.stdout.strip().splitlines()[-1])
    assert value["Backend"] == "claude"
    assert value["Output"] == "clean review"
    assert "rate limit or quota response" in result.stdout


def test_workflow_exposes_backend_input_and_delegates_to_runner():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    dispatch = workflow.split("  workflow_dispatch:", 1)[1].split("\npermissions:", 1)[0]
    assert "      backend:" in dispatch
    assert "        default: auto" in dispatch
    assert all(f"          - {name}" in dispatch for name in ("auto", "cursor", "codex", "claude"))
    assert "${{ vars.REVIEW_BACKENDS }}" in workflow
    assert ".\\ops\\review\\run-review.ps1" in workflow
    assert "steps.agent.outputs.backend" in workflow
    assert "Cursor Agent is reviewing" not in workflow
    assert "--force" not in workflow


def test_workflow_keeps_review_security_and_scheduling_contracts():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "pull-requests: write" in workflow
    assert "contents: read" in workflow
    assert "checks: write" in workflow
    assert "runs-on: [self-hosted, Windows, X64, agent-harness-review]" in workflow
    assert "group: review-${{ github.event.pull_request.number || github.event.inputs.pr_number }}" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "types: [opened]" in workflow


def test_ci_docs_explain_backend_configuration_and_manual_verification():
    docs = CI_DOCS.read_text(encoding="utf-8")
    assert "`REVIEW_BACKENDS`" in docs
    assert "`codex,claude,cursor`" in docs
    assert "`backend` dispatch input" in docs
    for backend in ("cursor", "codex", "claude"):
        assert f"gh workflow run review.yml -f pr_number=N -f backend={backend}" in docs
    assert "runner service user" in docs
