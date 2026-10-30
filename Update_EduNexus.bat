@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title EduNexus - update

echo ==============================================
echo  EduNexus - update the program files
echo ==============================================
echo.
echo This updates the EduNexus PROGRAM files from a newer package.
echo Your students, attendance, marks, quizzes, uploads and other records are kept.
echo A backup of your data is made first. EduNexus must be CLOSED.
echo.

set "NEW_PACKAGE=%~1"
if "%NEW_PACKAGE%"=="" (
  set /p NEW_PACKAGE=Paste the full path of the NEW EduNexus folder ^(the one you extracted^) and press Enter: 
)
if "%NEW_PACKAGE%"=="" (
  echo No folder was given. Nothing was changed.
  goto :end
)
if not exist "backend\.venv\Scripts\python.exe" (
  echo EduNexus has not been started on this laptop yet. Start it once with Start_EduNexus.bat first.
  goto :end
)
"backend\.venv\Scripts\python.exe" "backend\scripts\update_app.py" "%NEW_PACKAGE%"
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" (
  echo Now start EduNexus with Start_EduNexus.bat.
) else (
  echo The update was not completed. Read the message above. Your data was not lost.
)

:end
if not "%EDUNEXUS_NO_PAUSE%"=="1" pause
