@echo off
REM start_syndicate.bat — Launch The Syndicate engine + TC intelligence gate
REM Run from the syndicate\ directory.

cd /d "%~dp0"

echo Starting The Syndicate...

REM Window 1: Python engine (main.py)
start "Syndicate Engine" cmd /k "python main.py"

REM Window 2: TC intelligence gate (PowerShell watcher)
start "Syndicate TC Gate" powershell -ExecutionPolicy Bypass -NoExit -File "intelligence\wake_syndicate.ps1"

echo Both windows launched.
echo   Syndicate Engine  — runs main.py
echo   Syndicate TC Gate — watches triggers/ for TC signals
