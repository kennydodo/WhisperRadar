@echo off
rem WhisperRadar SECOND dashboard: same data, another port, so you can work on another
rem production at the same time. The first server (start_dashboard.cmd) keeps the scheduler
rem and the start-up resume; this one only serves what you click. Never run the same
rem production from both servers at once. Close this window to stop this server.

set "PORT=8541"
set "URL=http://127.0.0.1:%PORT%"
cd /d "%~dp0"

rem pull the latest WR_* user environment variables (keys set with setx
rem after this window's parent process started would otherwise be missed)
for /f "tokens=1,2,*" %%a in ('reg query HKCU\Environment 2^>nul ^| findstr /i "WR_"') do set "%%a=%%c"

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

"%PY%" -m whisperradar serve --port %PORT% --secondary
