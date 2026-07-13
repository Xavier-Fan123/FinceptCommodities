param(
    [ValidateSet('asia','overnight','all','news')]
    [string]$Scope = 'all',
    [int]$TimeoutSec = 240
)

$ErrorActionPreference = 'Stop'
$Root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$PreferredPython = Join-Path $env:LOCALAPPDATA 'com.fincept.terminal\venv-numpy2\Scripts\python.exe'
$Python = if (Test-Path -LiteralPath $PreferredPython) { $PreferredPython } else { 'python.exe' }
Push-Location $Root
try {
    & $Python -m lpg.cli refresh --scope $Scope --timeout $TimeoutSec
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
