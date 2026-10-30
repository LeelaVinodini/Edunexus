@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title EduNexus

echo ==============================================
echo  EduNexus - starting
echo ==============================================
echo.

if not exist "backend\app\main.py" (
  echo EduNexus program files were not found next to this file.
  echo Extract the whole ZIP into a normal folder, for example C:\EduNexus, and run this file again.
  goto :fail
)
if not exist "dist\index.html" (
  echo The EduNexus web pages are missing from this folder ^(dist\index.html^).
  echo Extract the whole ZIP again, or ask for a fresh copy.
  goto :fail
)

set "PYTHON_CMD="
call :find_python
if not defined PYTHON_CMD (
  echo Python 3.11 or newer is required but was not found.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  echo Then run this file again.
  goto :fail
)
echo Python found: %PYTHON_CMD%

if not exist "backend\.venv\Scripts\python.exe" (
  echo Creating this laptop's Python environment. This is only needed the first time.
  %PYTHON_CMD% -m venv "backend\.venv"
  if errorlevel 1 (
    echo Could not create the Python environment. Check that Python is installed correctly.
    goto :fail
  )
)

"backend\.venv\Scripts\python.exe" "backend\scripts\launcher.py" start
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" (
  echo EduNexus stopped with an error. See logs\backend.log and logs\install.log in this folder.
)
if not "%EDUNEXUS_NO_PAUSE%"=="1" pause
exit /b %RESULT%

:fail
if not "%EDUNEXUS_NO_PAUSE%"=="1" pause
exit /b 1

:find_python
where py >nul 2>&1 && py -3.11 -c "import sys" >nul 2>&1 && (set "PYTHON_CMD=py -3.11" & goto :eof)
where python >nul 2>&1 && python -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1 && (set "PYTHON_CMD=python" & goto :eof)
where py >nul 2>&1 && py -3 -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1 && (set "PYTHON_CMD=py -3" & goto :eof)
goto :eof
