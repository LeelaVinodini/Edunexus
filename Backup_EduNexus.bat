@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title EduNexus - backup

echo ==============================================
echo  EduNexus - backup of your data
echo ==============================================
echo.
echo This copies your database and uploaded files into a new dated folder
echo inside the "backups" folder. Old backups are never overwritten.
echo It is safe to run while EduNexus is open.
echo.

if not exist "backend\.venv\Scripts\python.exe" (
  echo EduNexus has not been started on this laptop yet, so there is no data to back up.
  goto :end
)
"backend\.venv\Scripts\python.exe" "backend\scripts\backup_restore.py" backup
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" (
  echo Backup finished. Copy that folder to a USB drive or another safe place.
) else (
  echo The backup did not finish. Your data was not changed. Read the message above.
)

:end
if not "%EDUNEXUS_NO_PAUSE%"=="1" pause
