# Synthetic provider output only; no models, credentials, or production services.
Describe 'Review publication safety' {
    BeforeEach {
        . (Join-Path $PSScriptRoot '../ops/review/run-review.ps1')
        $script:reviewOutputPath = Join-Path $TestDrive 'review-output.md'
    }

    It 'rejects unsafe backend output without a posted body' -TestCases @(
        @{ Payload = 'sk-synthetic12345678' }
        @{ Payload = 'ghp_synthetic12345678' }
        @{ Payload = ('T' * 48) }
        @{ Payload = 'Bearer synthetic-value' }
        @{ Payload = 'password=synthetic-value' }
        @{ Payload = 'C:\Users\reviewer\.claude\credentials.json' }
        @{ Payload = 'C:/Users/reviewer/.codex/auth.json' }
        @{ Payload = '/Users/reviewer/.claude/credentials.json' }
        @{ Payload = '/home/reviewer/.codex/auth.json' }
    ) {
        param($Payload)
        $fakeRunner = {
            param($command)
            [pscustomobject]@{
                ExitCode = 0
                Stdout = "- app.py:1: exposed $Payload`nREVIEW_VERDICT: FINDINGS 1`nREVIEW_STATUS: COMPLETE"
                Stderr = ''
                Model = $null
            }
        }.GetNewClosure()
        $result = Invoke-ReviewFallback -Backends @('claude') -Workspace $TestDrive -Prompt 'synthetic review' -ScratchDirectory $TestDrive -Runner $fakeRunner
        'stale body' | Set-Content -LiteralPath $script:reviewOutputPath
        $failure = ''
        try { Write-ReviewResult -Result $result -OutputPath $script:reviewOutputPath }
        catch { $failure = $_.Exception.Message }
        if ($failure -notlike 'Review did not complete*') { throw 'Expected a closed publication failure' }
        if (Test-Path -LiteralPath $script:reviewOutputPath) { throw 'Unsafe or stale body was retained' }
    }

    It 'publishes a safe review and its validated coverage marker' {
        $result = [pscustomobject]@{ Backend = 'fake'; Model = ''; Output = 'No significant findings.' }
        Write-ReviewResult -Result $result -OutputPath $script:reviewOutputPath -HeadSha ('a' * 40)
        $body = Get-Content -Raw -LiteralPath $script:reviewOutputPath
        if ($body -notmatch 'No significant findings\.') { throw 'Safe review was not published' }
        if ($body -notmatch '<!-- agent-review: sha=a{40} mode=full -->') { throw 'Validated marker is missing' }
    }

    It 'rejects a secret-shaped base ref before adding the marker' {
        $result = [pscustomobject]@{ Backend = 'fake'; Model = ''; Output = 'No significant findings.' }
        $failure = ''
        try { Write-ReviewResult -Result $result -OutputPath $script:reviewOutputPath -HeadSha ('a' * 40) -BaseRef 'ghp_synthetic12345678' }
        catch { $failure = $_.Exception.Message }
        if ($failure -notlike 'Review did not complete*') { throw 'Base ref bypassed the publication gate' }
        if (Test-Path -LiteralPath $script:reviewOutputPath) { throw 'Unsafe base ref was published' }
    }
}
