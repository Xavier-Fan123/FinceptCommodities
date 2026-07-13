param(
    [ValidateSet('asia','overnight','all','news')]
    [string]$Scope = 'all',
    [int]$TimeoutSec = 420,
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$RunId = 'manual'
)

$ErrorActionPreference = 'Stop'
$Root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$RunDir = Join-Path $Root 'data\private\platts\runlogs'
$OutputPath = Join-Path $RunDir ($RunId + '.output.log')
$StatePath = Join-Path $RunDir ($RunId + '.state.json')
$PreferredPython = Join-Path $env:LOCALAPPDATA 'com.fincept.terminal\venv-numpy2\Scripts\python.exe'
$Python = if (Test-Path -LiteralPath $PreferredPython) { $PreferredPython } else { 'python.exe' }
$StartedAt = [DateTimeOffset]::Now
$ExitCode = 1
$State = 'failed'
$ErrorMessage = $null

New-Item -ItemType Directory -Path $RunDir -Force | Out-Null
Remove-Item -LiteralPath $OutputPath, $StatePath -Force -ErrorAction SilentlyContinue

Push-Location $Root
try {
    $OutputLines = @(
        & $Python -m lpg.cli refresh --scope $Scope --timeout $TimeoutSec 2>&1 |
            ForEach-Object { [string]$_ }
    )
    $ExitCode = $LASTEXITCODE
    $OutputLines | Out-File -LiteralPath $OutputPath -Encoding utf8
    try {
        $Payload = ($OutputLines -join [Environment]::NewLine) | ConvertFrom-Json
        $State = [string]$Payload.state
    } catch {
        $State = if ($ExitCode -eq 0) { 'succeeded' } else { 'failed' }
    }
    if (-not $State) {
        $State = if ($ExitCode -eq 0) { 'succeeded' } else { 'failed' }
    }
} catch {
    $ErrorMessage = $_.Exception.Message
    $_ | Out-String | Out-File -LiteralPath $OutputPath -Encoding utf8 -Append
} finally {
    Pop-Location
    [ordered]@{
        run_id = $RunId
        scope = $Scope
        state = $State
        exit_code = $ExitCode
        started_at = $StartedAt.ToString('o')
        finished_at = [DateTimeOffset]::Now.ToString('o')
        output_path = $OutputPath
        error = $ErrorMessage
    } | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding utf8
}

exit $ExitCode
