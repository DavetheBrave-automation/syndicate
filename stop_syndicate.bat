@echo off
echo Stopping The Syndicate...

wmic process where "name='python.exe' and commandline like '%%syndicate%%main.py%%'" delete >nul 2>&1
wmic process where "name='python.exe' and commandline like '%%syndicate%%watchdog%%'" delete >nul 2>&1
wmic process where "name='powershell.exe' and commandline like '%%wake_syndicate%%'" delete >nul 2>&1

echo Done.
