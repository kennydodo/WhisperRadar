@echo off
rem WhisperRadar dashboard launcher: starts the server and opens the browser.
rem Close this window to stop the server.

set "PORT=8540"
set "URL=http://127.0.0.1:%PORT%"
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")

rem If the dashboard is already running, just open the browser
netstat -ano | findstr ":%PORT% " | findstr LISTENING >nul
if %errorlevel%==0 (
    echo Dashboard already running at %URL% - opening browser...
    start "" "%URL%"
    exit /b 0
)

echo Starting WhisperRadar dashboard at %URL% ...
rem open the browser once the server has had a moment to boot
start "" /min cmd /c "timeout /t 2 /nobreak >nul & start "" "%URL%""

"%PY%" -m whisperradar serve --port %PORT%
