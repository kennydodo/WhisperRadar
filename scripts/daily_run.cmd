@echo off
rem WhisperRadar daily run - invoked by the Windows Task Scheduler
cd /d "%~dp0.."
if not exist data mkdir data
set "PYTHONUTF8=1"
rem pull the latest WR_* user environment variables (API keys)
for /f "tokens=1,2,*" %%a in ('reg query HKCU\Environment 2^>nul ^| findstr /i "WR_"') do set "%%a=%%c"
if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")
"%PY%" -m whisperradar run >> "%~dp0..\data\run.log" 2>&1
