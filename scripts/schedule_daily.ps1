# Registers a daily Windows scheduled task for WhisperRadar.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\schedule_daily.ps1 -Time 09:00

param(
    [string]$Time = "09:00",
    [string]$TaskName = "WhisperRadar Daily"
)

$proj = Split-Path -Parent $PSScriptRoot
$cmd = Join-Path $proj "scripts\daily_run.cmd"

if (-not (Test-Path $cmd)) {
    Write-Error "daily_run.cmd not found at $cmd"
    exit 1
}

schtasks /Create /F /SC DAILY /ST $Time /TN $TaskName /TR "`"$cmd`""
Write-Host ""
Write-Host "Scheduled '$TaskName' daily at $Time."
Write-Host "Output is logged to $proj\data\run.log"
