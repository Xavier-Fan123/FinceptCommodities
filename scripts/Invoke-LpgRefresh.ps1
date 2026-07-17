param(
    [ValidateSet('asia','overnight','all','news')]
    [string]$Scope = 'all',
    [int]$TimeoutSec = 240,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$Root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$PreferredPython = Join-Path $env:LOCALAPPDATA 'com.fincept.terminal\venv-numpy2\Scripts\python.exe'
$Python = if (Test-Path -LiteralPath $PreferredPython) { $PreferredPython } else { 'python.exe' }
$SchedulerDir = Join-Path $Root 'data\private\platts\scheduler'
$MarkerPath = Join-Path $SchedulerDir ($Scope + '-success.json')
$LocalDate = (Get-Date).ToString('yyyy-MM-dd')

# This script is the daily scheduler entry point. Eight explicit triggers
# provide the two-hour "Excel was busy" retry window.
# Once today's scheduled refresh succeeds (or produces a usable partial
# snapshot), later triggers must not reopen Excel and ask the user to sign in.
if (-not $Force -and (Test-Path -LiteralPath $MarkerPath)) {
    try {
        $Marker = Get-Content -LiteralPath $MarkerPath -Raw | ConvertFrom-Json
        if ([string]$Marker.local_date -eq $LocalDate -and
            [string]$Marker.state -in @('succeeded', 'partial')) {
            Write-Output ("SKIPPED_ALREADY_SUCCEEDED scope=$Scope local_date=$LocalDate")
            exit 0
        }
    } catch {
        Write-Output ("SCHEDULER_MARKER_WARNING " + $_.Exception.Message)
    }
}

Push-Location $Root
try {
    $OutputLines = @(
        & $Python -m lpg.cli refresh --scope $Scope --timeout $TimeoutSec 2>&1 |
            ForEach-Object { [string]$_ }
    )
    $ExitCode = $LASTEXITCODE
    $OutputLines | ForEach-Object { Write-Output $_ }

    if ($ExitCode -eq 0) {
        try {
            $Payload = ($OutputLines -join [Environment]::NewLine) | ConvertFrom-Json
            $State = [string]$Payload.state
            if ($State -in @('succeeded', 'partial')) {
                New-Item -ItemType Directory -Path $SchedulerDir -Force | Out-Null
                [ordered]@{
                    scope = $Scope
                    local_date = $LocalDate
                    state = $State
                    completed_at = [DateTimeOffset]::Now.ToString('o')
                } | ConvertTo-Json | Set-Content -LiteralPath $MarkerPath -Encoding utf8
                Write-Output ("SCHEDULER_SUCCESS_MARKED scope=$Scope local_date=$LocalDate state=$State")
            }
        } catch {
            Write-Output ("SCHEDULER_MARKER_WARNING " + $_.Exception.Message)
        }
    }
    exit $ExitCode
} finally {
    Pop-Location
}
