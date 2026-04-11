@echo off
REM stop_syndicate.bat — Kill all Syndicate windows

echo Stopping The Syndicate...

taskkill /f /fi "WINDOWTITLE eq Syndicate Engine*" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq Syndicate TC Gate*" >nul 2>&1

echo Done.
