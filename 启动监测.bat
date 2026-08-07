@echo off
setlocal
cd /d "%~dp0"
title Job Monitor CLI
set "JOB_PROFILE="
echo =========================================
echo Job Monitor - Command Line Mode
echo Press Ctrl+C to stop monitoring.
echo =========================================
python main.py
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" echo Monitor exited with code %APP_EXIT_CODE%.
pause
exit /b %APP_EXIT_CODE%
