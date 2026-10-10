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
        @{ Payload = 'Bearer followed by a value' }
        @{ Payload = 'password=synthetic-value' }
        @{ Payload = ('{"api_key":"' + ('A' * 32) + '"}') }
        @{ Payload = '{"password":"short"}' }
        @{ Payload = "'password'='short'" }
        @{ Payload = 'C:\Users\reviewer\.claude\credentials.json' }
        @{ Payload = 'C:/Users/reviewer/.codex/auth.json' }
        @{ Payload = 'C:/./Users/reviewer/.codex/auth.json' }
        @{ Payload = 'C:/temp/../Users/reviewer/.codex/auth.json' }
        @{ Payload = 'C:/temp/../Users/reviewer/.codex/auth.json followed by ../../../../../public/config.py' }
        @{ Payload = '"file at C:/temp/../Users/reviewer/.codex/auth.json followed by ../../../../../public/config.py"' }
        @{ Payload = 'C:\Users.\reviewer\.codex\auth.json' }
        @{ Payload = 'C:\Users...\reviewer\.codex\auth.json' }
        @{ Payload = '"C:\Users \reviewer\.codex\auth.json"' }
        @{ Payload = '"C:\Program Files\..\Users\reviewer\.codex\auth.json"' }
        @{ Payload = 'C:\Program Files\..\Users\reviewer\.codex\auth.json' }
        @{ Payload = '"file at C:\Program Files\..\Users\reviewer\.codex\auth.json"' }
        @{ Payload = "'C:\Program Files\..\Users\reviewer\.codex\auth.json'" }
        @{ Payload = '`C:\Program Files\..\Users\reviewer\.codex\auth.json`' }
        @{ Payload = '"\\?\C:\Program Files\..\Users\reviewer\.codex\auth.json"' }
        @{ Payload = '"/var/data folder/../../home/reviewer/.codex/auth.json"' }
        @{ Payload = '"\\server\share\folder name\..\Users\reviewer\.codex\auth.json"' }
        @{ Payload = 'file:///C:/Program%20Files/%2e%2e/Users/reviewer/.codex/auth.json' }
        @{ Payload = 'C:/Users//reviewer/.codex/auth.json' }
        @{ Payload = '/home//reviewer/.codex/auth.json' }
        @{ Payload = '/tmp/../home/reviewer/.codex/auth.json' }
        @{ Payload = '//tmp/../home/reviewer/.codex/auth.json' }
        @{ Payload = '///home///reviewer/.codex/auth.json' }
        @{ Payload = '\\?\C:\temp\..\Users\reviewer\.codex\auth.json' }
        @{ Payload = '\\server\share\temp\..\Users\reviewer\.codex\auth.json' }
        @{ Payload = 'file:///home//reviewer/.codex/auth.json' }
        @{ Payload = '/Users/reviewer/.claude/credentials.json' }
        @{ Payload = '/home/reviewer/.codex/auth.json' }
        @{ Payload = '/root/.codex/auth.json' }
        @{ Payload = '/root' }
        @{ Payload = '/root/../public/config.py' }
        @{ Payload = '[profile](file:///home/reviewer/.codex/auth.json)' }
        @{ Payload = 'file:///h%6fme/reviewer/.codex/auth.json' }
        @{ Payload = 'C:/public/config.py followed by file:///h%6fme/reviewer/.codex/auth.json' }
        @{ Payload = 'C:/public/config.py followed by file:///C:/%55sers/reviewer/.codex/auth.json' }
        @{ Payload = 'C:/public/config.py followed by C:/temp/../../Users/reviewer/.codex/auth.json' }
        @{ Payload = 'C:/public/config.py followed by /tmp/../../home/reviewer/.codex/auth.json' }
        @{ Payload = 'profile:C:/temp/../Users/reviewer/.codex/auth.json' }
        @{ Payload = 'profile:/tmp/../home/reviewer/.codex/auth.json' }
        @{ Payload = 'C:\temp,dir\..\Users\reviewer\file.py' }
        @{ Payload = 'C:\temp;dir\..\Users\reviewer\file.py' }
        @{ Payload = 'C:\temp(dir)\..\Users\reviewer\file.py' }
        @{ Payload = 'C:\temp`dir\..\Users\reviewer\file.py' }
        @{ Payload = "C:\temp'dir\..\Users\reviewer\file.py" }
        @{ Payload = '/tmp<dir>/../home/reviewer/file.py' }
        @{ Payload = 'file:///C:/Users/reviewer/.codex/auth.json' }
        @{ Payload = '\\?\C:\Users\reviewer\.codex\auth.json' }
        @{ Payload = '\\.\C:\Users\reviewer\.codex\auth.json' }
        @{ Payload = '\??\C:\Users\reviewer\.codex\auth.json' }
        @{ Payload = '\\?\UNC\server\share\Users\reviewer\.codex\auth.json' }
        @{ Payload = '\\server\share\Users\reviewer\.codex\auth.json' }
        @{ Payload = 'file://server/share/Users/reviewer/.codex/auth.json' }
        @{ Payload = '\\?\Volume{12345678-1234-1234-1234-123456789abc}\Users\reviewer\.codex\auth.json' }
        @{ Payload = '\Device\HarddiskVolume1\Users\reviewer\.codex\auth.json' }
        @{ Payload = '\\?\GLOBALROOT\Device\HarddiskVolume1\Users\reviewer\.codex\auth.json' }
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
