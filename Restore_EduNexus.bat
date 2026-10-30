@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title EduNexus - restore

echo ==============================================
echo  EduNexus - restore a backup
echo ==============================================
echo.
echo Restoring REPLACES the data on this laptop with the data in a backup.
echo Before anything is replaced, your current data is backed up automatically.
echo.
echo EduNexus must be CLOSED for a restore. Close its window, or run Stop_EduNexus.bat.
echo.

if not exist "backend\.venv\Scripts\python.exe" (
  echo EduNexus has not been started on this laptop yet, so there is nothing to restore into.
  goto :end
)
"backend\.venv\Scripts\python.exe" "backend\scripts\backup_restore.py" restore
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo Nothing was lost. Read the message above and try again.

:end
if not "%EDUNEXUS_NO_PAUSE%"=="1" pause
