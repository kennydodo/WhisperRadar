@echo off
rem WhisperRadar - install everything in one go.
rem Installs missing system tools (Python, ffmpeg, Node.js, Chrome) with winget,
rem then the Python packages, FlowBatch, and the optional Remotion tool.
rem Safe to run again. If a system tool gets installed, close this window,
rem open a new one and run this file again so PATH picks it up.
setlocal enabledelayedexpansion
cd /d "%~dp0"
set NEEDRESTART=0

where winget >nul 2>&1
if errorlevel 1 (
    echo [X] winget not found - install "App Installer" from the Microsoft Store,
    echo     or install Python 3.11+, ffmpeg, Node 20+ and Chrome by hand.
    set HAVEWINGET=0
) else (
    set HAVEWINGET=1
)

echo.
echo === System tools ===
where python >nul 2>&1
if errorlevel 1 (
    if "%HAVEWINGET%"=="1" (
        echo Installing Python 3.12 ...
        winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
        set NEEDRESTART=1
    ) else (
        echo [X] Python missing. Install Python 3.11+ from python.org ^(tick "Add to PATH"^).
        pause & exit /b 1
    )
) else (
    echo Python OK
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
    if "%HAVEWINGET%"=="1" (
        echo Installing ffmpeg ...
        winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
        set NEEDRESTART=1
    ) else (
        echo [X] ffmpeg missing - install from ffmpeg.org
    )
) else (
    echo ffmpeg OK
)

where node >nul 2>&1
if errorlevel 1 (
    if "%HAVEWINGET%"=="1" (
        echo Installing Node.js LTS ...
        winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements
        set NEEDRESTART=1
    ) else (
        echo [X] Node.js 20+ missing - install from nodejs.org
    )
) else (
    echo Node OK
)

set CHROME=0
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set CHROME=1
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set CHROME=1
if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set CHROME=1
if "%CHROME%"=="0" (
    if "%HAVEWINGET%"=="1" (
        echo Installing Google Chrome ...
        winget install --id Google.Chrome -e --accept-source-agreements --accept-package-agreements
    ) else (
        echo [X] Google Chrome missing - install from google.com/chrome
    )
) else (
    echo Chrome OK
)

if "%NEEDRESTART%"=="1" (
    echo.
    echo [X] New tools were installed. Close this window, open a new one and run
    echo     install_all.cmd again to finish the package installs.
    pause & exit /b 0
)

echo.
echo === Python packages ===
if exist ".venv" (
    set VENVOK=
    for /f "delims=" %%v in ('".venv\Scripts\python.exe" -c "print(12345)" 2^>nul') do set VENVOK=%%v
    if not "!VENVOK!"=="12345" (
        echo Existing .venv is broken ^(its Python was removed^) - replacing it ...
        if exist ".venv_old" rmdir /s /q ".venv_old" 2>nul
        if exist ".venv_old" (
            echo [X] Old .venv_old folder is locked. Close WhisperRadar windows, a running render or the dashboard, then run this again.
            pause
            exit /b 1
        )
        ren ".venv" ".venv_old" 2>nul
        if exist ".venv" (
            echo [X] .venv is in use by a running program ^(dashboard, a job or a render^).
            echo     Nothing was deleted. Let any render finish, close the dashboard and
            echo     other WhisperRadar windows ^(or restart the PC^), then run this again.
            pause
            exit /b 1
        )
    )
)
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    python -m venv .venv || (echo [X] venv creation failed & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo [X] pip install failed & pause & exit /b 1)
".venv\Scripts\python.exe" -m pip install -r engines\renderly-api\requirements.txt || echo [X] renderly-api requirements failed
echo Installing Playwright browser ...
".venv\Scripts\python.exe" -m playwright install chromium || echo [X] playwright browser install failed

if not exist data mkdir data

echo.
echo === FlowBatch (Node) ===
pushd engines\flowbatch
call npm install || echo [X] npm install failed in engines\flowbatch
popd

echo.
echo === Remotion (optional, Node) ===
pushd tools\remotion
if exist package-lock.json (
    call npm ci || echo [X] npm ci failed in tools\remotion
) else (
    call npm install || echo [X] npm install failed in tools\remotion
)
popd

echo.
if exist ".venv_old" rmdir /s /q ".venv_old" 2>nul
if exist ".venv_old" echo [X] Could not remove .venv_old - you can delete that folder by hand.
echo === Check ===
".venv\Scripts\python.exe" wr.py --help >nul && echo Install complete. Double-click start_dashboard.cmd to launch.
echo.
echo Still to do by hand, once:
echo   - Google Flow login:  cd engines\flowbatch ^&^& npm run login
echo   - GEMINI_API_KEY in engines\renderly-api\.env  ^(only if you use PL/PR via Renderly^)
echo   - LLM / TTS API keys in the dashboard under Settings ^> LLM
echo   - Sign in once to z.ai / DeepSeek for the browser-chat stages
echo   - GPU Whisper needs an NVIDIA GPU with current drivers; otherwise it runs on CPU
pause
