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
$script:ReviewCompletionMarker = 'REVIEW_STATUS: COMPLETE'

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

function Test-ReviewAttemptRateLimit {
    [CmdletBinding()]
    param(
        [AllowEmptyString()][string]$Stdout,
        [AllowEmptyString()][string]$Stderr,
        [int]$ShortStdoutThreshold = 600
    )

    if (Test-ReviewRateLimit -Text $Stderr) { return $true }
    if ($Stdout.Length -lt $ShortStdoutThreshold) {
        return Test-ReviewRateLimit -Text $Stdout
    }
    return $false
}

function Get-ReviewDiagnosticTail {
    [CmdletBinding()]
    param(
        [AllowEmptyString()][string]$Stderr,
        [int]$MaxLines = 20,
        [int]$MaxCharacters = 2048
    )

    if ([string]::IsNullOrWhiteSpace($Stderr)) { return '' }
    $lines = @($Stderr -split "`r?`n")
    $redacted = (($lines | Select-Object -Last $MaxLines) -join [Environment]::NewLine)
    $redacted = $redacted -replace '(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]'
    $redacted = $redacted -replace '(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password)(\s*[:=]\s*)("[^"]*"|''[^'']*''|[^\s,;]+)', '$1$2[REDACTED]'
    $redacted = $redacted -replace '(?i)\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}|xox[baprs]-[A-Za-z0-9-]{8,})\b', '[REDACTED]'
    $redacted = $redacted -replace '\b[A-Za-z0-9+/=_-]{40,}\b', '[REDACTED]'
    if ($redacted.Length -gt $MaxCharacters) {
        $redacted = $redacted.Substring($redacted.Length - $MaxCharacters)
    }
    return $redacted
}

function Get-CompletedReviewText {
    [CmdletBinding()]
    param([AllowEmptyString()][string]$Text)

    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $marker = [regex]::Escape($script:ReviewCompletionMarker)
    $match = [regex]::Match($Text, "(?:^|\r?\n)$marker(?:\r?\n)?\z")
    if (-not $match.Success) { return $null }
    $review = $Text.Substring(0, $match.Index).Trim()
    if ([string]::IsNullOrWhiteSpace($review)) { return $null }
    return $review
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
        [string]$CursorBase = '',
        [bool]$WindowsPlatform = ($env:OS -eq 'Windows_NT')
    )

    $name = $Backend.Trim().ToLowerInvariant()
    switch ($name) {
        'cursor' {
            if ([string]::IsNullOrWhiteSpace($CursorBase)) {
                $entrypoint = Get-CursorAgentEntrypoint
            } else {
                $entrypoint = Get-CursorAgentEntrypoint -CursorBase $CursorBase
            }
            $arguments = @($entrypoint.Index, '-p', '--output-format', 'text', '--mode', 'ask')
            if (-not $WindowsPlatform) {
                $arguments += @('--sandbox', 'enabled')
            }
            $arguments += @('--workspace', $Workspace)
            return [pscustomobject]@{
                Backend = $name
                FilePath = $entrypoint.Node
                Arguments = $arguments
                InputText = $Prompt
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
                Arguments = @('exec', '--sandbox', 'read-only', '--cd', $Workspace, '--ephemeral', '--color', 'never', '--output-last-message', $resultPath, '-')
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
                Arguments = @('-p', '--output-format', 'text', '--permission-mode', 'manual', '--tools', 'Read,Grep,Glob', '--allowedTools', 'Read,Grep,Glob', '--strict-mcp-config', '--disable-slash-commands')
                InputText = $Prompt
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
            # Native pipeline encoding is read from the global preference in
            # Windows PowerShell 5.1; a function-local assignment is ignored.
            $previousOutputEncoding = $global:OutputEncoding
            $previousConsoleOutputEncoding = [Console]::OutputEncoding
            $previousConsoleInputEncoding = [Console]::InputEncoding
            try {
                # Windows PowerShell promotes native stderr to error records. Keep
                # those records redirected without aborting before LASTEXITCODE is read.
                $ErrorActionPreference = 'Continue'
                # Windows PowerShell 5.1 otherwise encodes pipeline input as ASCII
                # and decodes native stdout with the active console code page.
                $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
                $global:OutputEncoding = $utf8NoBom
                [Console]::OutputEncoding = $utf8NoBom
                [Console]::InputEncoding = $utf8NoBom
                if ($null -ne $Command.InputText) {
                    $Command.InputText | & $Command.FilePath @arguments 1> $stdoutPath 2> $stderrPath
                } else {
                    & $Command.FilePath @arguments 1> $stdoutPath 2> $stderrPath
                }
            } finally {
                $ErrorActionPreference = $previousErrorActionPreference
                $global:OutputEncoding = $previousOutputEncoding
                [Console]::OutputEncoding = $previousConsoleOutputEncoding
                [Console]::InputEncoding = $previousConsoleInputEncoding
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
        $stdout = [string](Get-Content -Raw -LiteralPath $Command.ResultPath -Encoding utf8)
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
            $reason = $null
            $completedReview = $null
            if ([int]$attempt.ExitCode -eq 0) {
                $completedReview = Get-CompletedReviewText -Text ([string]$attempt.Stdout)
            }
            if ([string]::IsNullOrWhiteSpace([string]$completedReview)) {
                $rateLimited = Test-ReviewAttemptRateLimit -Stdout ([string]$attempt.Stdout) -Stderr ([string]$attempt.Stderr)
                if ($rateLimited) {
                    $reason = 'rate limit or quota response'
                } elseif ([int]$attempt.ExitCode -ne 0) {
                    $reason = "exit code $($attempt.ExitCode)"
                } elseif ([string]::IsNullOrWhiteSpace([string]$attempt.Stdout)) {
                    $reason = 'empty output'
                } else {
                    $reason = 'missing completion marker'
                }
            }
            if ($reason) {
                $failures.Add("$backend`: $reason")
                $warning = "Review backend $backend failed ($reason); trying the next backend."
                $diagnostic = Get-ReviewDiagnosticTail -Stderr ([string]$attempt.Stderr)
                if (-not [string]::IsNullOrWhiteSpace($diagnostic)) {
                    $warning = "$warning`nStderr tail (redacted, last 20 lines / 2 KB):`n$diagnostic"
                }
                Write-Warning $warning
                continue
            }
            return [pscustomobject]@{
                Backend = $backend
                Model = $attempt.Model
                Output = $completedReview
            }
        } catch {
            $failures.Add("$backend`: $($_.Exception.Message)")
            Write-Warning "Review backend $backend could not run; trying the next backend. $($_.Exception.Message)"
        }
    }
    throw "all review backends failed: $($failures -join '; ')"
}

function Get-ReviewDiff {
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
    return [string]$attempt.Stdout
}

function Add-ReviewDiffContext {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Prompt,
        [Parameter(Mandatory = $true)][string]$Diff,
        [int]$MaxDiffBytes = 204800
    )

    if ($MaxDiffBytes -le 0) { throw 'MaxDiffBytes must be positive' }
    if ([string]::IsNullOrWhiteSpace($Diff)) { throw 'pull request diff is empty' }

    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $embeddedDiff = $Diff
    $omittedFiles = New-Object System.Collections.Generic.List[string]
    if ($utf8.GetByteCount($Diff) -gt $MaxDiffBytes) {
        $fileStarts = @([regex]::Matches($Diff, '(?m)^diff --git .+$'))
        if ($fileStarts.Count -eq 0) {
            throw 'oversized pull request diff has no file boundaries'
        }

        $builder = New-Object System.Text.StringBuilder
        if ($fileStarts[0].Index -gt 0) {
            [void]$builder.Append($Diff.Substring(0, $fileStarts[0].Index))
        }
        $truncated = $false
        for ($index = 0; $index -lt $fileStarts.Count; $index++) {
            $start = $fileStarts[$index].Index
            $end = if ($index + 1 -lt $fileStarts.Count) { $fileStarts[$index + 1].Index } else { $Diff.Length }
            $section = $Diff.Substring($start, $end - $start)
            $header = $fileStarts[$index].Value
            $fileName = $header
            if ($header -match '^diff --git (?:"?a/.*?"?) (?:"?b/(.*)"?)$') {
                $fileName = $Matches[1].Trim().Trim('"')
            }

            if (-not $truncated -and $utf8.GetByteCount($builder.ToString() + $section) -le $MaxDiffBytes) {
                [void]$builder.Append($section)
            } else {
                $truncated = $true
                $omittedFiles.Add($fileName)
            }
        }
        $embeddedDiff = $builder.ToString()
    }

    $context = "$Prompt`r`n`r`nThe pull request diff is embedded below. Review it directly; do not fetch the diff with network tools. Repository files may be read for additional context.`r`n`r`nBEGIN PULL REQUEST DIFF`r`n$($embeddedDiff.TrimEnd())`r`nEND PULL REQUEST DIFF"
    if ($omittedFiles.Count -gt 0) {
        $context += "`r`nOMITTED FILES (diff exceeded $MaxDiffBytes bytes): $($omittedFiles -join ', ')"
    }
    return $context
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

    $diff = Get-ReviewDiff -PrNumber $PrNumber -Workspace $Workspace -ScratchDirectory $ScratchDirectory
    $effectivePrompt = Add-ReviewDiffContext -Prompt $Prompt -Diff $diff
    $backends = @(Resolve-ReviewBackends -RequestedBackend $Backend -ConfiguredBackends $ConfiguredBackends)
    $runner = { param($command) Invoke-ReviewBackendProcess -Command $command -ScratchDirectory $ScratchDirectory }
    $result = Invoke-ReviewFallback -Backends $backends -Workspace $Workspace -Prompt $effectivePrompt -ScratchDirectory $ScratchDirectory -Runner $runner
    Write-ReviewResult -Result $result -OutputPath $OutputPath

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
