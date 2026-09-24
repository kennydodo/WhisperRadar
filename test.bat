@echo off
rem Runs the WhisperRadar test suite. Exit code 1 = something failed.
setlocal
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo [1/1] WhisperRadar unit tests...
"%PY%" -m unittest discover -s tests -v
if errorlevel 1 goto :fail

echo.
echo All tests passed.
endlocal & exit /b 0

:fail
echo.
echo TESTS FAILED
endlocal & exit /b 1
