@echo off
rem WhisperRadar one-time local setup: creates .venv, installs dependencies,
rem checks ffmpeg and creates the data folder. Safe to run again.
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [!] Python not found on PATH. Install Python 3.11+ from python.org
    echo     ^(tick "Add python.exe to PATH"^), then run this again.
    pause & exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    python -m venv .venv || (echo [!] venv creation failed & pause & exit /b 1)
)

echo Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo [!] pip install failed & pause & exit /b 1)

if not exist data mkdir data

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo.
    echo [!] ffmpeg not found. Installing with winget ...
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    echo     Close and reopen this window afterwards so PATH picks up ffmpeg.
) else (
    echo ffmpeg OK
)

echo.
echo Checking the install ...
".venv\Scripts\python.exe" wr.py --help >nul && echo Setup complete. Double-click start_dashboard.cmd to launch.
pause
