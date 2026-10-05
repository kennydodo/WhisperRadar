@echo off
rem WhisperRadar dashboard launcher WITH PHONE ACCESS - Tailscale.
rem Same as start_dashboard.cmd, but the server also listens on this PC's
rem Tailscale address so your phone can open it. Needs:
rem   1. Tailscale installed and signed in on this PC and on the phone
rem   2. a password, set once:  setx WR_PASSWORD "your-password"
rem Optional: give the PC's Tailscale address as an argument if it is not found.
rem No auto-reload in this mode: restart this window after code changes.
rem Close this window to stop the server.
rem NOTE: no round brackets inside echo lines - a closing bracket there ends
rem the surrounding if-block early and cmd exits at once with a syntax error.

set "PORT=8540"
cd /d "%~dp0"

rem pull the latest WR_* user environment variables - WR_PASSWORD, API keys
for /f "tokens=1,2,*" %%a in ('reg query HKCU\Environment 2^>nul ^| findstr /i "WR_"') do set "%%a=%%c"

if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")

if not defined WR_PASSWORD goto :nopassword

netstat -ano | findstr ":%PORT% " | findstr LISTENING >nul
if %errorlevel%==0 goto :alreadyrunning

echo Starting WhisperRadar with phone access on port %PORT% ...
set "HOSTARG="
if not "%~1"=="" set "HOSTARG=--host %~1"
"%PY%" -m whisperradar serve --remote --port %PORT% %HOSTARG%
echo.
echo The server stopped. Read the lines above for the reason.
pause
exit /b 0

:nopassword
echo.
echo  Phone access needs a password. Run this once in a terminal:
echo      setx WR_PASSWORD "choose-a-password"
echo  then open a NEW window and start this file again.
echo.
pause
exit /b 1

:alreadyrunning
echo.
echo  A dashboard is already running on port %PORT%.
echo  Close its window first, then start this file again.
echo  Phone access needs this launcher to be the one that starts the server.
echo.
pause
exit /b 1
