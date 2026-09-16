@echo off
rem WhisperRadar daily run - invoked by the Windows Task Scheduler
cd /d "%~dp0.."
if not exist data mkdir data
set "PYTHONUTF8=1"
if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")
"%PY%" -m whisperradar run >> "%~dp0..\data\run.log" 2>&1
