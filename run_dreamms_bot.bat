@echo off
rem Run DreamMS helper from this folder
chcp 65001 >nul
pushd "%~dp0"

rem Find Python executable
where python >nul 2>&1
if errorlevel 1 (
  where py >nul 2>&1
  if errorlevel 1 (
    echo Python not found in PATH. Install Python or run this script with full path to python.
    pause
    popd
    exit /b 1
  ) else (
    set "PYEXEC=py"
  )
) else (
  set "PYEXEC=python"
)

"%PYEXEC%" "dreamms_bot.py"
set /p _=Press Enter to close...
popd
