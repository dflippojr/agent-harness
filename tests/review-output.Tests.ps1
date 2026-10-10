# Synthetic provider output only; no models, credentials, or production services.
Describe 'Review publication safety' {
    BeforeEach {
        . (Join-Path $PSScriptRoot '../ops/review/run-review.ps1')
        $script:reviewOutputPath = Join-Path $TestDrive 'review-output.md'
    }

    It 'rejects unsafe backend output without a posted body' -TestCases @(
        # Credentials and long tokens.
        @{ Payload = 'sk-synthetic12345678' }
        @{ Payload = 'ghp_synthetic12345678' }
        @{ Payload = ('T' * 48) }
        @{ Payload = (('A' * 38) + '==') }
        @{ Payload = 'gh**p_**synthetic12345678' }
        @{ Payload = 'gh<b>p</b>_synthetic12345678' }
        @{ Payload = 'gh`p`_synthetic12345678' }
        @{ Payload = '[gh](https://example.invalid)p_synthetic12345678' }
        @{ Payload = '[/ho][ref]me/reviewer/auth.json' }
        @{ Payload = "[gh]p_synthetic12345678`n`n[gh]: https://example.invalid" }
        @{ Payload = 'Bearer synthetic-value' }
        @{ Payload = 'Bearer followed by a value' }
        @{ Payload = 'password=synthetic-value' }
        @{ Payload = ('{"api_key":"' + ('A' * 32) + '"}') }
        @{ Payload = '{"password":"short"}' }
        @{ Payload = "'password'='short'" }
        @{ Payload = ('https://example.invalid/download?sig=' + ('A' * 48)) }
        @{ Payload = ('https://example.invalid/blob?sv=2024&sig=' + ('A' * 24) + '/' + ('B' * 24)) }
        @{ Payload = ('https://example.invalid/blob?sv=2024&sig=' + ('A' * 24) + '%2F' + ('B' * 24)) }
        # Any Windows absolute root, whatever follows it: no path is parsed or canonicalized.
        @{ Payload = 'C:/public/config.py' }
        @{ Payload = 'C:\Users\reviewer\.claude\credentials.json' }
        @{ Payload = 'C:/temp/../Users/reviewer/.codex/auth.json' }
        @{ Payload = '"C:\Program Files\..\Users\reviewer\.codex\auth.json"' }
        @{ Payload = 'C:\temp,dir\..\Users\reviewer\file.py' }
        @{ Payload = 'profile:C:/temp/../Users/reviewer/.codex/auth.json' }
        @{ Payload = '\\server\share\folder\config.py' }
        @{ Payload = '//server/share/folder/config.py' }
        @{ Payload = 'see (//server/share/folder/config.py)' }
        @{ Payload = '/\server\share\folder\config.py' }
        @{ Payload = '\\?\C:\Users\reviewer\.codex\auth.json' }
        @{ Payload = '\\.\C:\Users\reviewer\.codex\auth.json' }
        @{ Payload = '\\?\Volume{12345678-1234-1234-1234-123456789abc}\config.py' }
        @{ Payload = '\??\C:\Users\reviewer\.codex\auth.json' }
        @{ Payload = 'file:///tmp/config.py' }
        @{ Payload = '[profile](file:///home/reviewer/.codex/auth.json)' }
        @{ Payload = 'file://server/share/Users/reviewer/.codex/auth.json' }
        # Any separator followed by a profile directory name outside a known repository path.
        @{ Payload = '/home/reviewer/.codex/auth.json' }
        @{ Payload = '/Users/reviewer/.claude/credentials.json' }
        @{ Payload = '/root' }
        @{ Payload = '/root/../public/config.py' }
        @{ Payload = '/tmp/../home/reviewer/.codex/auth.json' }
        @{ Payload = '"/var/data folder/../../home/reviewer/.codex/auth.json"' }
        @{ Payload = '/tmp<dir>/../home/reviewer/file.py' }
        @{ Payload = '\Device\HarddiskVolume1\Users\reviewer\.codex\auth.json' }
        @{ Payload = '\Device\HarddiskVolume1\Users.\reviewer\auth.json' }
        @{ Payload = '\Device\HarddiskVolume1\USERS~1\reviewer\auth.json' }
        @{ Payload = '\Documents and Settings\reviewer\auth.json' }
        @{ Payload = 'app/[locale]/home/components/Nav.tsx' }
        # Encodings a reader would see decoded.
        @{ Payload = '/h%6fme/reviewer/.codex/auth.json' }
        @{ Payload = '&#47;home&#47;reviewer&#47;auth.json' }
        @{ Payload = 'C&#58;&#92;Users&#92;reviewer' }
        @{ Payload = '\/home\/reviewer\/auth.json' }
        @{ Payload = 'C\:\\Users\\reviewer' }
        @{ Payload = ([string][char]0xFF0F + 'home' + [char]0xFF0F + 'reviewer') }
        @{ Payload = ('/ho' + [char]0x200B + 'me/reviewer') }
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

    It 'publishes ordinary review text' -TestCases @(
        @{ Payload = 'ops/review/run-review.ps1:340: the regex `[\\/]` misses `"\\n"`.' }
        @{ Payload = 'See https://learn.microsoft.com/dotnet/standard/io/file-path-formats#trim-characters.' }
        @{ Payload = 'docs/users.md, docs/root-ca.md and src/rooted/homepage.py' }
        @{ Payload = 'The old side is /dev/null; the shebang is /usr/bin/env.' }
        @{ Payload = '`$env:USERPROFILE` and `~/.claude/settings.json` are inherited.' }
        @{ Payload = 'Call 127.0.0.1:8100/gpu or localhost:8100/gpu.' }
        @{ Payload = 'See https://github.com/dflippojr/agent-harness/pull/549 and // a code comment.' }
        @{ Payload = '**Structure** `harness/__init__.py`: keep the `<n>` placeholder.' }
    ) {
        param($Payload)
        $result = [pscustomobject]@{ Backend = 'fake'; Model = ''; Output = $Payload }
        Write-ReviewResult -Result $result -OutputPath $script:reviewOutputPath
        $body = Get-Content -Raw -LiteralPath $script:reviewOutputPath
        if (-not $body.Contains($Payload)) { throw 'Safe review text was not published unchanged' }
    }

    It 'publishes a profile-named directory only as a known repository path' {
        $known = 'app/[locale]/home/components/Nav.tsx'
        $result = [pscustomobject]@{ Backend = 'fake'; Model = ''; Output = "$known`:3: finding" }
        Write-ReviewResult -Result $result -OutputPath $script:reviewOutputPath -DiffPaths @($known)
        if (-not (Get-Content -Raw -LiteralPath $script:reviewOutputPath).Contains($known)) { throw 'Known path was not published' }
        foreach ($prefix in @('/home/reviewer/', 'C:/Users/reviewer/', '/tmp/../home/reviewer/')) {
            $result.Output = "$prefix$known`:3: finding"
            $failure = ''
            try { Write-ReviewResult -Result $result -OutputPath $script:reviewOutputPath -DiffPaths @($known) }
            catch { $failure = $_.Exception.Message }
            if ($failure -notlike 'Review did not complete*') { throw "A known path exempted the profile prefix $prefix" }
        }
    }

    It 'publishes known citations in code spans, before a period and with underscores' {
        $known = @('src/__tests__/home/nav.test.ts', 'harness_modules/remote_control/folder_discovery.py')
        foreach ($text in @("See ``$($known[0])``.", "Fix ``$($known[1])``.", "Fix $($known[1]).")) {
            $result = [pscustomobject]@{ Backend = 'fake'; Model = ''; Output = $text }
            Write-ReviewResult -Result $result -OutputPath $script:reviewOutputPath -DiffPaths $known
            if (-not (Get-Content -Raw -LiteralPath $script:reviewOutputPath).Contains($text)) { throw "Known citation was rejected: $text" }
        }
    }

    It 'keeps composed and decomposed diff names distinct' {
        $composed = 'home/caf' + [char]0xe9 + '.py'
        $decomposed = 'home/cafe' + [char]0x301 + '.py'
        $diff = "diff --git a/$composed b/$composed`ndeleted file mode 100644`n-old`ndiff --git a/$decomposed b/$decomposed`ndeleted file mode 100644`n-old"
        $embedding = Get-ReviewDiffEmbedding -Diff $diff
        if (@($embedding.FilePaths).Count -ne 2) { throw 'Canonically equivalent diff names were merged' }
    }

    It 'does not let a name that normalizes to an absolute path exempt a profile path' {
        $slash = [string][char]0xFF0F
        $disguised = $slash + 'home' + $slash + 'reviewer/auth.json'
        foreach ($text in @('/home/reviewer/auth.json', $disguised)) {
            $result = [pscustomobject]@{ Backend = 'fake'; Model = ''; Output = $text }
            $failure = ''
            try { Write-ReviewResult -Result $result -OutputPath $script:reviewOutputPath -DiffPaths @($disguised) }
            catch { $failure = $_.Exception.Message }
            if ($failure -notlike 'Review did not complete*') { throw 'A normalized diff name exempted a profile path' }
        }
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

    It 'keeps the diagnostic message before a path and redacts the rest of the line' {
        $tail = Get-ReviewDiagnosticTail -Stderr "error: open C:\Users\reviewer\auth.json failed`nplain line`nsee /home/reviewer/.codex`nprofile file:///h%6fme/reviewer`ntoken=synthetic-value"
        $lines = @($tail -split "`r?`n")
        if ($tail -like '*reviewer*' -or $tail -like '*synthetic-value*') { throw 'Diagnostic tail leaked a path or token' }
        if ($lines[0] -ne 'error: open [REDACTED PATH]' -or $lines[1] -ne 'plain line') { throw "Unexpected redaction: $tail" }
        if ($lines[3] -ne 'profile [REDACTED PATH]') { throw "Unexpected URL redaction: $tail" }
        if ((Get-ReviewDiagnosticTail -Stderr '&#47;home&#47;reviewer') -ne '[REDACTED LINE]') { throw 'Encoded profile path survived' }
        if ((Get-ReviewDiagnosticTail -Stderr 'open %2Fhome%2Freviewer then C:\temp\x') -ne '[REDACTED LINE]') { throw 'Encoded prefix survived truncation' }
        if ((Get-ReviewDiagnosticTail -Stderr 'value ghp%5Fsynthetic12345678') -ne '[REDACTED LINE]') { throw 'Encoded token survived' }
        if ((Get-ReviewDiagnosticTail -Stderr 'token=synthetic-value') -ne 'token=[REDACTED]') { throw 'Redaction marker was re-redacted' }
        $multiline = Get-ReviewDiagnosticTail -Stderr "password=`"synthetic-first`nsynthetic-rest`" done"
        if ($multiline -like '*synthetic*') { throw "Multiline quoted credential survived: $multiline" }
    }
}
