[CmdletBinding()]
param(
    [string]$Backend = $env:REVIEW_BACKEND,
    [string]$ConfiguredBackends = $env:REVIEW_BACKENDS,
    [string]$Workspace = $env:GITHUB_WORKSPACE,
    [string]$PrNumber = $env:PR_NUMBER,
    [string]$Prompt = $env:REVIEW_PROMPT,
    [string]$OutputPath = '',
    [string]$ScratchDirectory = $env:RUNNER_TEMP
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:KnownReviewBackends = @('cursor', 'codex', 'claude')
$script:DefaultReviewBackends = @('codex', 'claude', 'cursor')

function Resolve-ReviewBackends {
    [CmdletBinding()]
    param(
        [AllowEmptyString()]
        [string]$RequestedBackend,
        [AllowEmptyString()]
        [string]$ConfiguredBackends
    )

    $requested = $RequestedBackend.Trim().ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($requested)) { $requested = 'auto' }
    if ($requested -ne 'auto') {
        if ($script:KnownReviewBackends -notcontains $requested) {
            throw "unsupported review backend '$RequestedBackend'"
        }
        return @($requested)
    }

    if ([string]::IsNullOrWhiteSpace($ConfiguredBackends)) {
        return @($script:DefaultReviewBackends)
    }

    $resolved = New-Object System.Collections.Generic.List[string]
    foreach ($item in $ConfiguredBackends.Split(',')) {
        $name = $item.Trim().ToLowerInvariant()
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        if ($script:KnownReviewBackends -notcontains $name) {
            throw "unsupported review backend '$name' in REVIEW_BACKENDS"
        }
        if (-not $resolved.Contains($name)) { $resolved.Add($name) }
    }
    if ($resolved.Count -eq 0) {
        throw 'REVIEW_BACKENDS does not contain a supported backend'
    }
    return @($resolved.ToArray())
}

function Test-ReviewRateLimit {
    [CmdletBinding()]
    param([AllowEmptyString()][string]$Text)

    if ([string]::IsNullOrWhiteSpace($Text)) { return $false }
    return $Text -match '(?i)(resource[_ -]?exhausted|rate[_ -]?limit(?:ed)?|quota(?:\s+(?:has\s+been\s+)?exceeded)?|too\s+many\s+requests|usage\s+limit|credit\s+balance)'
}

function Get-CursorAgentEntrypoint {
    [CmdletBinding()]
    param([string]$CursorBase = (Join-Path $env:LOCALAPPDATA 'cursor-agent'))

    $versions = Join-Path $CursorBase 'versions'
    $latest = Get-ChildItem -LiteralPath $versions -Directory -ErrorAction Stop |
        Sort-Object Name -Descending |
        Select-Object -First 1
    if (-not $latest) { throw "no cursor-agent version under $versions" }
    $node = Join-Path $latest.FullName 'node.exe'
    $index = Join-Path $latest.FullName 'index.js'
    if (-not (Test-Path -LiteralPath $node) -or -not (Test-Path -LiteralPath $index)) {
        throw "cursor-agent node.exe/index.js missing in $($latest.FullName)"
    }
    return [pscustomobject]@{ Node = $node; Index = $index }
}

function Get-ReviewBackendCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Backend,
        [Parameter(Mandatory = $true)][string]$Workspace,
        [Parameter(Mandatory = $true)][string]$Prompt,
        [Parameter(Mandatory = $true)][string]$ScratchDirectory,
        [string]$CursorBase = ''
    )

    $name = $Backend.Trim().ToLowerInvariant()
    switch ($name) {
        'cursor' {
            if ([string]::IsNullOrWhiteSpace($CursorBase)) {
                $entrypoint = Get-CursorAgentEntrypoint
            } else {
                $entrypoint = Get-CursorAgentEntrypoint -CursorBase $CursorBase
            }
            return [pscustomobject]@{
                Backend = $name
                FilePath = $entrypoint.Node
                Arguments = @($entrypoint.Index, '-p', '--output-format', 'text', '--mode', 'ask', '--sandbox', 'enabled', '--workspace', $Workspace, $Prompt)
                InputText = $null
                WorkingDirectory = $Workspace
                ResultPath = $null
                Model = $null
            }
        }
        'codex' {
            $resultPath = Join-Path $ScratchDirectory 'codex-review-output.md'
            return [pscustomobject]@{
                Backend = $name
                FilePath = 'codex'
                Arguments = @('exec', '--sandbox', 'read-only', '--ask-for-approval', 'never', '--cd', $Workspace, '--ephemeral', '--ignore-user-config', '--color', 'never', '--output-last-message', $resultPath, '-')
                InputText = $Prompt
                WorkingDirectory = $Workspace
                ResultPath = $resultPath
                Model = $null
            }
        }
        'claude' {
            return [pscustomobject]@{
                Backend = $name
                FilePath = 'claude'
                Arguments = @('-p', '--output-format', 'text', '--permission-mode', 'manual', '--tools', 'Read,Grep,Glob,Bash', '--allowedTools', 'Read,Grep,Glob,Bash(gh pr diff:*)', '--strict-mcp-config', '--disable-slash-commands', $Prompt)
                InputText = $null
                WorkingDirectory = $Workspace
                ResultPath = $null
                Model = $null
            }
        }
        default { throw "unsupported review backend '$Backend'" }
    }
}

function Invoke-ReviewBackendProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Command,
        [Parameter(Mandatory = $true)][string]$ScratchDirectory
    )

    New-Item -ItemType Directory -Path $ScratchDirectory -Force | Out-Null
    $stdoutPath = Join-Path $ScratchDirectory ("{0}-stdout.txt" -f $Command.Backend)
    $stderrPath = Join-Path $ScratchDirectory ("{0}-stderr.txt" -f $Command.Backend)
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    if ($Command.ResultPath) {
        Remove-Item -LiteralPath $Command.ResultPath -Force -ErrorAction SilentlyContinue
    }

    $exitCode = -1
    try {
        Push-Location -LiteralPath $Command.WorkingDirectory
        try {
            $arguments = @($Command.Arguments)
            $previousErrorActionPreference = $ErrorActionPreference
            try {
                # Windows PowerShell promotes native stderr to error records. Keep
                # those records redirected without aborting before LASTEXITCODE is read.
                $ErrorActionPreference = 'Continue'
                if ($null -ne $Command.InputText) {
                    $Command.InputText | & $Command.FilePath @arguments 1> $stdoutPath 2> $stderrPath
                } else {
                    & $Command.FilePath @arguments 1> $stdoutPath 2> $stderrPath
                }
            } finally {
                $ErrorActionPreference = $previousErrorActionPreference
            }
            $exitCode = $LASTEXITCODE
            if ($null -eq $exitCode) { $exitCode = 0 }
        } finally {
            Pop-Location
        }
    } catch {
        $_ | Out-String | Out-File -LiteralPath $stderrPath -Append -Encoding utf8
    }

    $stdout = ''
    $stderr = ''
    if (Test-Path -LiteralPath $stdoutPath) { $stdout = [string](Get-Content -Raw -LiteralPath $stdoutPath) }
    if (Test-Path -LiteralPath $stderrPath) { $stderr = [string](Get-Content -Raw -LiteralPath $stderrPath) }
    if ($Command.ResultPath -and (Test-Path -LiteralPath $Command.ResultPath)) {
        $stdout = [string](Get-Content -Raw -LiteralPath $Command.ResultPath)
    }
    return [pscustomobject]@{
        ExitCode = [int]$exitCode
        Stdout = $stdout
        Stderr = $stderr
        Model = $Command.Model
    }
}

function Invoke-ReviewFallback {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string[]]$Backends,
        [Parameter(Mandatory = $true)][string]$Workspace,
        [Parameter(Mandatory = $true)][string]$Prompt,
        [Parameter(Mandatory = $true)][string]$ScratchDirectory,
        [Parameter(Mandatory = $true)][scriptblock]$Runner,
        [string]$CursorBase = ''
    )

    $failures = New-Object System.Collections.Generic.List[string]
    foreach ($backend in $Backends) {
        try {
            $commandArgs = @{
                Backend = $backend
                Workspace = $Workspace
                Prompt = $Prompt
                ScratchDirectory = $ScratchDirectory
            }
            if (-not [string]::IsNullOrWhiteSpace($CursorBase)) { $commandArgs.CursorBase = $CursorBase }
            $command = Get-ReviewBackendCommand @commandArgs
            $attempt = & $Runner $command
            $combined = "{0}`n{1}" -f $attempt.Stdout, $attempt.Stderr
            $reason = $null
            if ([int]$attempt.ExitCode -ne 0) {
                $reason = "exit code $($attempt.ExitCode)"
            } elseif ([string]::IsNullOrWhiteSpace([string]$attempt.Stdout)) {
                $reason = 'empty output'
            } elseif (Test-ReviewRateLimit -Text $combined) {
                $reason = 'rate limit or quota response'
            }
            if ($reason) {
                $failures.Add("$backend`: $reason")
                Write-Warning "Review backend $backend failed ($reason); trying the next backend."
                continue
            }
            return [pscustomobject]@{
                Backend = $backend
                Model = $attempt.Model
                Output = ([string]$attempt.Stdout).Trim()
            }
        } catch {
            $failures.Add("$backend`: $($_.Exception.Message)")
            Write-Warning "Review backend $backend could not run; trying the next backend. $($_.Exception.Message)"
        }
    }
    throw "all review backends failed: $($failures -join '; ')"
}

function New-ReviewDiffFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$PrNumber,
        [Parameter(Mandatory = $true)][string]$Workspace,
        [Parameter(Mandatory = $true)][string]$ScratchDirectory
    )

    if ($PrNumber -notmatch '^\d+$') { throw "invalid PR number '$PrNumber'" }
    $command = [pscustomobject]@{
        Backend = 'github-diff'
        FilePath = 'gh'
        Arguments = @('pr', 'diff', $PrNumber)
        InputText = $null
        WorkingDirectory = $Workspace
        ResultPath = $null
        Model = $null
    }
    $attempt = Invoke-ReviewBackendProcess -Command $command -ScratchDirectory $ScratchDirectory
    if ($attempt.ExitCode -ne 0) {
        throw "gh pr diff $PrNumber exited $($attempt.ExitCode): $($attempt.Stderr.Trim())"
    }
    if ([string]::IsNullOrWhiteSpace($attempt.Stdout)) {
        throw "gh pr diff $PrNumber produced no diff"
    }
    $diffPath = Join-Path $Workspace ('.automated-review-diff-{0}.patch' -f $PID)
    $attempt.Stdout | Out-File -LiteralPath $diffPath -Encoding utf8
    return $diffPath
}

function Add-ReviewDiffContext {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Prompt,
        [Parameter(Mandatory = $true)][string]$DiffPath
    )

    $leaf = Split-Path -Leaf $DiffPath
    return "$Prompt`r`n`r`nThe workflow has also saved the exact gh pr diff output to '$leaf'. Read that file if gh is unavailable in the read-only environment; do not treat the scratch file itself as a proposed change."
}

function Write-ReviewResult {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Result,
        [Parameter(Mandatory = $true)][string]$OutputPath
    )

    $label = $Result.Backend
    if (-not [string]::IsNullOrWhiteSpace([string]$Result.Model)) {
        $label = "$label ($($Result.Model))"
    }
    $body = "{0}`r`n`r`n---`r`nAutomated review backend: **{1}**." -f $Result.Output.Trim(), $label
    $body | Out-File -LiteralPath $OutputPath -Encoding utf8
}

function Invoke-ReviewMain {
    [CmdletBinding()]
    param(
        [string]$Backend,
        [string]$ConfiguredBackends,
        [string]$Workspace,
        [string]$PrNumber,
        [string]$Prompt,
        [string]$OutputPath,
        [string]$ScratchDirectory
    )

    if ([string]::IsNullOrWhiteSpace($Workspace)) { $Workspace = (Get-Location).Path }
    if ([string]::IsNullOrWhiteSpace($PrNumber)) { throw 'PR_NUMBER is required' }
    if ([string]::IsNullOrWhiteSpace($Prompt)) { throw 'REVIEW_PROMPT is required' }
    if ([string]::IsNullOrWhiteSpace($ScratchDirectory)) { $ScratchDirectory = $env:TEMP }
    if ([string]::IsNullOrWhiteSpace($ScratchDirectory)) { throw 'RUNNER_TEMP or TEMP is required' }
    if ([string]::IsNullOrWhiteSpace($OutputPath)) { $OutputPath = Join-Path $Workspace 'review-output.md' }

    $diffPath = $null
    try {
        $diffPath = New-ReviewDiffFile -PrNumber $PrNumber -Workspace $Workspace -ScratchDirectory $ScratchDirectory
        $effectivePrompt = Add-ReviewDiffContext -Prompt $Prompt -DiffPath $diffPath
        $backends = @(Resolve-ReviewBackends -RequestedBackend $Backend -ConfiguredBackends $ConfiguredBackends)
        $runner = { param($command) Invoke-ReviewBackendProcess -Command $command -ScratchDirectory $ScratchDirectory }
        $result = Invoke-ReviewFallback -Backends $backends -Workspace $Workspace -Prompt $effectivePrompt -ScratchDirectory $ScratchDirectory -Runner $runner
        Write-ReviewResult -Result $result -OutputPath $OutputPath
    } finally {
        if ($diffPath) { Remove-Item -LiteralPath $diffPath -Force -ErrorAction SilentlyContinue }
    }

    if (-not [string]::IsNullOrWhiteSpace($env:GITHUB_OUTPUT)) {
        "backend=$($result.Backend)" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
        if (-not [string]::IsNullOrWhiteSpace([string]$result.Model)) {
            "model=$($result.Model)" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
        }
    }
    Write-Host "Review completed with backend: $($result.Backend)"
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-ReviewMain -Backend $Backend -ConfiguredBackends $ConfiguredBackends -Workspace $Workspace -PrNumber $PrNumber -Prompt $Prompt -OutputPath $OutputPath -ScratchDirectory $ScratchDirectory
}
