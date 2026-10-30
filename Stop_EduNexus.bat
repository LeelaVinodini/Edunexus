@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title EduNexus - stop

if not exist "backend\.venv\Scripts\python.exe" (
  echo EduNexus has not been set up on this laptop yet, so there is nothing to stop.
  goto :end
)
"backend\.venv\Scripts\python.exe" "backend\scripts\launcher.py" stop

:end
if not "%EDUNEXUS_NO_PAUSE%"=="1" pause
