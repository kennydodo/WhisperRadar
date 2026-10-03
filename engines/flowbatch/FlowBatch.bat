@echo off
setlocal
title FlowBatch
cd /d "%~dp0"

echo ==========================================================
echo   FlowBatch - batch image generation for Google Flow
echo ==========================================================
echo.

rem --- Node.js present and new enough? ----------------------
where node >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Node.js was not found on PATH.
    echo         Install Node.js 20 or newer, then run this file again.
    echo         https://nodejs.org
    echo.
    pause
    exit /b 1
)

node -e "process.exit(process.versions.node.split('.')[0] >= 20 ? 0 : 1)"
if errorlevel 1 (
    echo [ERROR] Node.js 20 or newer is required. This machine has:
    node --version
    echo.
    pause
    exit /b 1
)

rem --- dependencies ----------------------------------------
if not exist "node_modules\playwright" (
    echo Dependencies are missing. Running npm install...
    echo.
    call npm install
    if errorlevel 1 (
        echo.
        echo [ERROR] npm install failed. Fix the error above and try again.
        echo.
        pause
        exit /b 1
    )
    echo.
)

rem --- already running? ------------------------------------
netstat -an | findstr "LISTENING" | findstr ":8787" >nul 2>nul
if not errorlevel 1 (
    echo The UI already appears to be running on port 8787.
    echo Opening it in your browser.
    start "" http://127.0.0.1:8787
    echo.
    pause
    exit /b 0
)

rem --- go ---------------------------------------------------
echo Starting the UI. Your browser will open in a moment.
echo.
echo Leave this window open while you work.
echo Press Ctrl+C here to stop the tool.
echo.

node src\cli.js serve --open

echo.
echo The UI has stopped.
pause
