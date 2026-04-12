@echo off
cd /d "%~dp0"
echo Starting The Syndicate (background)...

REM Engine: python main.py — hidden, logs go to logs\syndicate.log as always
powershell -Command "Start-Process 'C:\Python314\python.exe' -ArgumentList 'main.py' -WorkingDirectory '%~dp0' -WindowStyle Hidden"

REM TC Gate: wake_syndicate.ps1 — hidden, polls triggers\ every 500ms
powershell -Command "Start-Process powershell -ArgumentList '-ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden -File ""%~dp0intelligence\wake_syndicate.ps1""' -WorkingDirectory '%~dp0' -WindowStyle Hidden"

echo Syndicate running in background.
echo   Engine: python main.py
echo   Gate:   intelligence\wake_syndicate.ps1
echo   Logs:   logs\syndicate.log
echo.
echo Run stop_syndicate.bat to shut down.
