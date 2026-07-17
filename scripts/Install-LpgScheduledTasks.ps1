param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$TaskPrefix = 'Fincept LPG'
$Root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$Runner = Join-Path $PSScriptRoot 'Invoke-LpgRefresh.ps1'
$Definitions = @(
    @{ Name = "$TaskPrefix Overnight"; Scope = 'overnight'; At = '08:00' },
    @{ Name = "$TaskPrefix Asia Close"; Scope = 'asia'; At = '17:30' }
)

if ($Uninstall) {
    foreach ($definition in $Definitions) {
        Unregister-ScheduledTask -TaskName $definition.Name -Confirm:$false -ErrorAction SilentlyContinue
    }
    Write-Output 'REMOVED Fincept LPG scheduled tasks'
    exit 0
}

if ($DryRun -or -not $Install) {
    [pscustomobject]@{
        mode = 'dry-run'
        timezone = [TimeZoneInfo]::Local.Id
        expected_timezone = 'Singapore Standard Time'
        interactive_user_only = $true
        repetition = 'every 15 minutes for 2 hours until the first usable daily refresh'
        tasks = $Definitions
        runner = $Runner
        root = $Root
    } | ConvertTo-Json -Depth 5
    exit 0
}

if ([TimeZoneInfo]::Local.Id -ne 'Singapore Standard Time') {
    throw "Windows timezone must be Singapore Standard Time before installation; current=$([TimeZoneInfo]::Local.Id)"
}
if (-not (Test-Path -LiteralPath $Runner)) { throw "Missing runner: $Runner" }

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

foreach ($definition in $Definitions) {
    $arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`" -Scope $($definition.Scope)"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $Root
    # Some Windows ScheduledTasks module versions expose no writable
    # Repetition property on a Daily trigger. Eight explicit daily triggers
    # are equivalent to a 15-minute retry window and work across versions.
    $baseTime = [DateTime]::Today.Add([TimeSpan]::Parse($definition.At))
    $triggers = @(
        for ($retry = 0; $retry -lt 8; $retry++) {
            New-ScheduledTaskTrigger -Daily -At $baseTime.AddMinutes(15 * $retry)
        }
    )
    Register-ScheduledTask -TaskName $definition.Name -Action $action -Trigger $triggers `
        -Principal $principal -Settings $settings -Description `
        'Refresh Fincept LPG data through the signed-in official S&P Global Energy Excel Add-in; later retry triggers skip after daily success.' `
        -Force | Out-Null
}
Write-Output 'INSTALLED Fincept LPG scheduled tasks (08:00 overnight, 17:30 Asia close)'
