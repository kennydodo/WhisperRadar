# Installs (or removes) a WhisperRadar dashboard auto-start entry that runs at
# Windows login. No admin rights needed.
#
# Install:  powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1
# Remove:   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1 -Remove

param([switch]$Remove)

$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup "WhisperRadar Dashboard.lnk"

if ($Remove) {
    if (Test-Path $lnk) {
        Remove-Item $lnk -Force
        Write-Host "Removed auto-start: $lnk"
    } else {
        Write-Host "No auto-start entry found."
    }
    exit 0
}

$proj = Split-Path -Parent $PSScriptRoot
$target = Join-Path $proj "start_dashboard.cmd"
if (-not (Test-Path $target)) {
    Write-Error "start_dashboard.cmd not found at $target"
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnk)
$sc.TargetPath = $target
$sc.WorkingDirectory = $proj
$sc.WindowStyle = 7   # minimized
$sc.Description = "WhisperRadar dashboard (starts server + opens browser)"
$sc.Save()

Write-Host "Installed auto-start: $lnk"
Write-Host "The dashboard will start (minimized) and open in your browser at login."
Write-Host "Remove anytime with:  scripts\autostart.ps1 -Remove"
