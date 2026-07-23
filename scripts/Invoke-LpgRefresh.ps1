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

# This script is the daily market and derived-curve pipeline entry point.
# Eight explicit triggers provide the two-hour "Excel was busy" retry window.
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
                $CurvePipelineState = $null
                $CurveLatestCommonDate = $null
                $CurveMissingLegs = @()
                $CurveDuplicateRecords = 0
                $CurveAnomalyCount = 0
                $CurveReasons = @()
                if ($null -ne $Payload.curve_pipeline) {
                    $CurvePipelineState = [string]$Payload.curve_pipeline.state
                    $CurveLatestCommonDate = [string]$Payload.curve_pipeline.latest_common_date
                    $CurveMissingLegs = @($Payload.curve_pipeline.missing_latest_legs)
                    $CurveDuplicateRecords = [int]$Payload.curve_pipeline.duplicate_record_count
                    $CurveAnomalyCount = [int]$Payload.curve_pipeline.anomaly_count
                    $CurveReasons = @($Payload.curve_pipeline.reason_codes)
                }
                New-Item -ItemType Directory -Path $SchedulerDir -Force | Out-Null
                [ordered]@{
                    scope = $Scope
                    local_date = $LocalDate
                    state = $State
                    completed_at = [DateTimeOffset]::Now.ToString('o')
                    curve_pipeline_state = $CurvePipelineState
                    curve_latest_common_date = $CurveLatestCommonDate
                    curve_missing_legs = $CurveMissingLegs
                    curve_duplicate_records = $CurveDuplicateRecords
                    curve_anomaly_count = $CurveAnomalyCount
                    curve_reason_codes = $CurveReasons
                } | ConvertTo-Json | Set-Content -LiteralPath $MarkerPath -Encoding utf8
                Write-Output (
                    "SCHEDULER_SUCCESS_MARKED scope=$Scope local_date=$LocalDate state=$State" +
                    " curve_pipeline=$CurvePipelineState curve_as_of=$CurveLatestCommonDate" +
                    " missing_legs=$($CurveMissingLegs.Count) duplicates=$CurveDuplicateRecords" +
                    " anomalies=$CurveAnomalyCount"
                )
            }
        } catch {
            Write-Output ("SCHEDULER_MARKER_WARNING " + $_.Exception.Message)
        }
    }
    exit $ExitCode
} finally {
    Pop-Location
}
