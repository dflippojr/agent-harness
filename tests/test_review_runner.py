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
    ]
    assert commands["cursor"]["InputText"] == "prompt"

    codex_args = commands["codex"]["Arguments"]
    result_path = str(scratch / "codex-review-output.md")
    assert codex_args == [
        "exec",
        "--sandbox",
        "read-only",
        "--cd",
        str(workspace),
        "--ephemeral",
        "--color",
        "never",
        "--output-last-message",
        result_path,
        "-",
    ]
    assert "--ignore-user-config" not in codex_args
    assert commands["codex"]["ResultPath"] == result_path
    assert commands["codex"]["InputText"] == "prompt"

    claude_args = commands["claude"]["Arguments"]
    assert "Read,Grep,Glob" in claude_args
    assert all("Bash" not in arg for arg in claude_args)
    assert not {"Edit", "Write", "NotebookEdit"}.intersection(claude_args)
    assert "--strict-mcp-config" in claude_args
    assert commands["claude"]["InputText"] == "prompt"


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
    return [pscustomobject]@{{ ExitCode = 0; Stdout = "- src/app.py:12: real bug`nREVIEW_STATUS: COMPLETE"; Stderr = ''; Model = $null }}
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
    assert "REVIEW_STATUS: COMPLETE" not in value["body"]


def test_missing_completion_marker_triggers_fallback(tmp_path):
    result = run_powershell(
        tmp_path,
        f"""
$script:calls = New-Object System.Collections.Generic.List[string]
$runner = {{
    param($command)
    $script:calls.Add($command.Backend)
    if ($command.Backend -eq 'codex') {{
        return [pscustomobject]@{{ ExitCode = 0; Stdout = 'plausible but unverified review'; Stderr = ''; Model = $null }}
    }}
    return [pscustomobject]@{{ ExitCode = 0; Stdout = "No significant findings.`nREVIEW_STATUS: COMPLETE"; Stderr = ''; Model = $null }}
}}
$result = Invoke-ReviewFallback -Backends @('codex','claude') -Workspace '{tmp_path}' -Prompt prompt -ScratchDirectory '{tmp_path}' -Runner $runner
[ordered]@{{ backend = $result.Backend; calls = @($script:calls); body = $result.Output }} | ConvertTo-Json -Compress
""",
    )
    assert result.returncode == 0, output(result)
    value = json.loads(result.stdout.strip().splitlines()[-1])
    assert value == {
        "backend": "claude",
        "calls": ["codex", "claude"],
        "body": "No significant findings.",
    }
    assert "missing completion marker" in result.stdout


def test_unable_to_review_response_without_marker_triggers_fallback(tmp_path):
    result = run_powershell(
        tmp_path,
        f"""
$script:calls = New-Object System.Collections.Generic.List[string]
$runner = {{
    param($command)
    $script:calls.Add($command.Backend)
    if ($command.Backend -eq 'codex') {{
        return [pscustomobject]@{{ ExitCode = 0; Stdout = 'Unable to review: environment policy blocked the diff.'; Stderr = ''; Model = $null }}
    }}
    return [pscustomobject]@{{ ExitCode = 0; Stdout = "No significant findings.`nREVIEW_STATUS: COMPLETE"; Stderr = ''; Model = $null }}
}}
$result = Invoke-ReviewFallback -Backends @('codex','claude') -Workspace '{tmp_path}' -Prompt prompt -ScratchDirectory '{tmp_path}' -Runner $runner
[ordered]@{{ backend = $result.Backend; calls = @($script:calls); body = $result.Output }} | ConvertTo-Json -Compress
""",
    )
    assert result.returncode == 0, output(result)
    value = json.loads(result.stdout.strip().splitlines()[-1])
    assert value["backend"] == "claude"
    assert value["calls"] == ["codex", "claude"]
    assert "Unable to review" not in value["body"]
    assert "missing completion marker" in result.stdout.lower()


def test_all_backends_without_completion_marker_fail(tmp_path):
    result = run_powershell(
        tmp_path,
        f"""
$runner = {{
    param($command)
    return [pscustomobject]@{{ ExitCode = 0; Stdout = 'review without proof of completion'; Stderr = ''; Model = $null }}
}}
Invoke-ReviewFallback -Backends @('codex','claude') -Workspace '{tmp_path}' -Prompt prompt -ScratchDirectory '{tmp_path}' -Runner $runner
""",
    )
    assert result.returncode != 0
    assert "all review backends failed" in output(result)
    assert "codex: missing completion marker" in output(result)
    assert "claude: missing completion marker" in output(result)


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


def test_process_launcher_round_trips_unicode_review_and_posts_it(tmp_path):
    fake_backend = tmp_path / "fake-backend.ps1"
    fake_backend.write_text(
        "$utf8 = New-Object System.Text.UTF8Encoding($false)\n"
        "[Console]::InputEncoding = $utf8\n"
        "[Console]::OutputEncoding = $utf8\n"
        "[Console]::Out.Write([Console]::In.ReadToEnd())\n",
        encoding="ascii",
    )
    prompt_path = tmp_path / "prompt.md"
    prompt = (
        "- src/caf\u00e9.py:7: \u6f22\u5b57 identifier changed from na\u00efve "
        "to \u0395\u03bb\u03bb\u03b7\u03bd\u03b9\u03ba\u03ac \u2014 regression.\nREVIEW_STATUS: COMPLETE"
    )
    prompt_path.write_text(prompt, encoding="utf-8")
    output_path = tmp_path / "posted-review.md"
    result = run_powershell(
        tmp_path,
        f"""
$prompt = Get-Content -Raw -LiteralPath '{prompt_path}' -Encoding utf8
$command = [pscustomobject]@{{
    Backend = 'fake'
    FilePath = (Get-Command powershell.exe).Source
    Arguments = @('-NoLogo', '-NoProfile', '-File', '{fake_backend}')
    InputText = $prompt
    WorkingDirectory = '{tmp_path}'
    ResultPath = $null
    Model = $null
}}
$attempt = Invoke-ReviewBackendProcess -Command $command -ScratchDirectory '{tmp_path}'
$runner = {{ param($ignored) $attempt }}
$review = Invoke-ReviewFallback -Backends @('codex') -Workspace '{tmp_path}' -Prompt ignored -ScratchDirectory '{tmp_path}' -Runner $runner
Write-ReviewResult -Result $review -OutputPath '{output_path}'
""",
    )
    assert result.returncode == 0, output(result)
    posted = output_path.read_text(encoding="utf-8-sig")
    assert posted.splitlines()[0] == prompt.splitlines()[0]
    assert "src/caf\u00e9.py" in posted
    assert "\u6f22\u5b57 identifier changed from na\u00efve to \u0395\u03bb\u03bb\u03b7\u03bd\u03b9\u03ba\u03ac \u2014 regression" in posted
    assert "Automated review backend: **codex**." in posted


def test_shared_prompt_contains_prefetched_diff(tmp_path):
    result = run_powershell(
        tmp_path,
        r"""
$diff = @'
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-old value
+new value
'@
Add-ReviewDiffContext -Prompt 'original shared prompt' -Diff $diff
""",
    )
    assert result.returncode == 0, output(result)
    assert result.stdout.startswith("original shared prompt")
    assert "+new value" in result.stdout
    assert "BEGIN PULL REQUEST DIFF" in result.stdout
    assert "gh pr diff" not in result.stdout


def test_oversize_diff_truncates_at_file_boundary_and_lists_omitted_files(tmp_path):
    result = run_powershell(
        tmp_path,
        r"""
$diff = @'
diff --git a/src/one.py b/src/one.py
--- a/src/one.py
+++ b/src/one.py
@@ -0,0 +1 @@
+AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
diff --git a/src/two.py b/src/two.py
--- a/src/two.py
+++ b/src/two.py
@@ -0,0 +1 @@
+BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB
diff --git a/src/three.py b/src/three.py
--- a/src/three.py
+++ b/src/three.py
@@ -0,0 +1 @@
+CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC
'@
Add-ReviewDiffContext -Prompt 'review prompt' -Diff $diff -MaxDiffBytes 160
""",
    )
    assert result.returncode == 0, output(result)
    assert "+AAAAAAAA" in result.stdout
    assert "+BBBBBBBB" not in result.stdout
    assert "+CCCCCCCC" not in result.stdout
    assert "OMITTED FILES (diff exceeded 160 bytes): src/two.py, src/three.py" in result.stdout


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
    return [pscustomobject]@{{ ExitCode = 0; Stdout = "clean review`nREVIEW_STATUS: COMPLETE"; Stderr = ''; Model = $null }}
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


@pytest.mark.parametrize(
    "review",
    [
        "- src/api.py:9: the rate limit branch drops a completed review.",
        "- src/api.py:12: quota exceeded is ordinary finding text here.",
        "- src/auth.py:4: users cannot access the resource after this change.",
    ],
)
def test_completed_review_trigger_words_are_accepted_and_posted(tmp_path, review):
    escaped_review = review.replace("'", "''")
    output_path = tmp_path / "posted-review.md"
    result = run_powershell(
        tmp_path,
        f"""
$script:calls = New-Object System.Collections.Generic.List[string]
$runner = {{
    param($command)
    $script:calls.Add($command.Backend)
    return [pscustomobject]@{{ ExitCode = 0; Stdout = '{escaped_review}
REVIEW_STATUS: COMPLETE'; Stderr = ''; Model = $null }}
}}
$result = Invoke-ReviewFallback -Backends @('codex','claude') -Workspace '{tmp_path}' -Prompt prompt -ScratchDirectory '{tmp_path}' -Runner $runner
Write-ReviewResult -Result $result -OutputPath '{output_path}'
[ordered]@{{ backend = $result.Backend; calls = @($script:calls); body = $result.Output }} | ConvertTo-Json -Compress
""",
    )
    assert result.returncode == 0, output(result)
    value = json.loads(result.stdout.strip().splitlines()[-1])
    assert value["backend"] == "codex"
    assert value["calls"] == ["codex"]
    assert value["body"] == review
    posted = output_path.read_text(encoding="utf-8-sig")
    assert review in posted
    assert "Automated review backend: **codex**." in posted


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
    return [pscustomobject]@{{ ExitCode = 0; Stdout = "clean review`nREVIEW_STATUS: COMPLETE"; Stderr = ''; Model = $null }}
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


def test_diagnostic_tail_is_bounded_and_redacts_tokens(tmp_path):
    result = run_powershell(
        tmp_path,
        r"""
$lines = @(1..25 | ForEach-Object { ('diagnostic line {0:D2} ' -f $_) + ('detail ' * 80) })
$lines += 'Authorization: Bearer bearer-secret-value-1234567890'
$lines += 'api_key=sk-supersecretvalue1234567890'
$tail = Get-ReviewDiagnosticTail -Stderr ($lines -join "`n")
[ordered]@{ tail = $tail; length = $tail.Length } | ConvertTo-Json -Compress
""",
    )
    assert result.returncode == 0, output(result)
    value = json.loads(result.stdout.strip())
    assert value["length"] <= 2048
    assert "diagnostic line 01" not in value["tail"]
    assert "bearer-secret-value" not in value["tail"]
    assert "supersecretvalue" not in value["tail"]
    assert value["tail"].count("[REDACTED]") == 2


def test_failed_backend_warning_includes_stderr_tail(tmp_path):
    result = run_powershell(
        tmp_path,
        f"""
$script:index = 0
$runner = {{
    param($command)
    $script:index++
    if ($script:index -eq 1) {{
        return [pscustomobject]@{{ ExitCode = 9; Stdout = ''; Stderr = 'backend diagnostic detail'; Model = $null }}
    }}
    return [pscustomobject]@{{ ExitCode = 0; Stdout = "clean review`nREVIEW_STATUS: COMPLETE"; Stderr = ''; Model = $null }}
}}
$result = Invoke-ReviewFallback -Backends @('codex','claude') -Workspace '{tmp_path}' -Prompt prompt -ScratchDirectory '{tmp_path}' -Runner $runner
$result | ConvertTo-Json -Compress
""",
    )
    assert result.returncode == 0, output(result)
    assert "Stderr tail (redacted, last 20 lines / 2 KB)" in result.stdout
    assert "backend diagnostic detail" in result.stdout


def test_workflow_exposes_backend_input_and_delegates_to_runner():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    dispatch = workflow.split("  workflow_dispatch:", 1)[1].split("\npermissions:", 1)[0]
    assert "      backend:" in dispatch
    assert "        default: auto" in dispatch
    assert all(f"          - {name}" in dispatch for name in ("auto", "cursor", "codex", "claude"))
    assert "${{ vars.REVIEW_BACKENDS }}" in workflow
    assert ".\\ops\\review\\run-review.ps1" in workflow
    assert "steps.agent.outputs.backend" in workflow
    assert "diff embedded in this prompt" in workflow
    assert "Run 'gh pr diff" not in workflow
    assert "REVIEW_STATUS: COMPLETE" in workflow
    assert '$title = "Automated review $conclusion"' in workflow
    assert '$title = "Review by $backend"' in workflow
    assert '-f "output[title]=$title"' in workflow
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
