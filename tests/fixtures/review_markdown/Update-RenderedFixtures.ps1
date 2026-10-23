# Regenerates rendered.json: GitHub's POST /markdown HTML for the publication-scan payloads in
# tests/review-output.Tests.ps1, so the tests can stub the renderer with real answers and make no
# network calls. Run it by hand (gh must be signed in) after adding payloads there:
#   powershell -NoProfile -File tests/fixtures/review_markdown/Update-RenderedFixtures.ps1
# Every payload is synthetic, so nothing sensitive is sent.
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '../../..')
. (Join-Path $root 'ops/review/run-review.ps1')

$tests = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $root 'tests/review-output.Tests.ps1'), [ref]$null, [ref]$null)
$cases = {
    param([string]$Name)
    $it = $tests.Find({ param($node) $node -is [System.Management.Automation.Language.CommandAst] -and $node.GetCommandName() -eq 'It' -and $node.CommandElements[1].Value -eq $Name }, $true)
    $index = [Array]::FindIndex([object[]]$it.CommandElements, [Predicate[object]]{ param($element) $element -is [System.Management.Automation.Language.CommandParameterAst] -and $element.ParameterName -eq 'TestCases' })
    @(Invoke-Expression $it.CommandElements[$index + 1].Extent.Text | ForEach-Object { $_.Payload })
}
# The rejected payloads as the backend reports them; the published ones as they are.
$sets = [ordered]@{
    rejected = @(& $cases 'rejects unsafe backend output without a posted body' | ForEach-Object { "- app.py:1: exposed $_" })
    published = @(& $cases 'publishes ordinary review text')
}
$lines = foreach ($set in $sets.Keys) {
    foreach ($text in $sets[$set]) {
        $html = Invoke-ReviewMarkdownRender -Text $text -Repository 'dflippojr/agent-harness'
        [pscustomobject]@{ set = $set; text = $text; html = $html } | ConvertTo-Json -Compress
    }
}
# One row per line, ASCII only, so invisible and bidirectional characters stay visible as escapes.
$json = "[`n" + ($lines -join ",`n") + "`n]`n"
$json = [regex]::Replace($json, '\\u00(3c|3e|26|27)', { param($match) [string][char][Convert]::ToInt32($match.Groups[1].Value, 16) })
$json = [regex]::Replace($json, '[^\x00-\x7F]', { param($match) '\u{0:x4}' -f [int][char]$match.Value })
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'rendered.json'), $json, (New-Object System.Text.UTF8Encoding($false)))
"Wrote $(@($lines).Count) rendered payloads."
