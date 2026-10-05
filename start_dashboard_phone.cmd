@echo off
rem WhisperRadar dashboard launcher WITH PHONE ACCESS (Tailscale).
rem Same as start_dashboard.cmd, but the server also listens on this PC's
rem Tailscale address so your phone can open it. Needs:
rem   1. Tailscale installed and signed in on this PC and on the phone
rem   2. a password:  setx WR_PASSWORD "your-password"   (then open a new window)
rem No auto-reload in this mode: restart this window after code changes.
rem Close this window to stop the server.

set "PORT=8540"
set "URL=http://127.0.0.1:%PORT%"
cd /d "%~dp0"

rem pull the latest WR_* user environment variables (WR_PASSWORD, API keys)
for /f "tokens=1,2,*" %%a in ('reg query HKCU\Environment 2^>nul ^| findstr /i "WR_"') do set "%%a=%%c"

if not defined WR_PASSWORD (
    echo.
    echo  Phone access needs a password. Run this once, then start again:
    echo      setx WR_PASSWORD "choose-a-password"
    echo.
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")

netstat -ano | findstr ":%PORT% " | findstr LISTENING >nul
if %errorlevel%==0 (
    echo A dashboard is already running on port %PORT%. Close it first (or this
    echo window cannot add phone access), then start this launcher again.
    pause
    exit /b 1
)

"%PY%" -m whisperradar serve --remote --port %PORT%
pause
