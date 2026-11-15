@echo off
setlocal
cd /d "%~dp0"
set "JOB_PROFILE="
set "FLASK_PORT=5000"
set "JOB_USER=1"
if /i "%~1"=="user2" (
    set "JOB_PROFILE=user2"
    set "FLASK_PORT=5001"
    set "JOB_USER=2"
)
if /i "%~1"=="cli" goto cli
title Job Monitor - User %JOB_USER%
echo =========================================
echo Job Monitor Web - User %JOB_USER%
echo Open: http://127.0.0.1:%FLASK_PORT%
echo Press Ctrl+C to stop the service.
echo =========================================
python app.py
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" echo Service exited with code %APP_EXIT_CODE%.
pause
exit /b %APP_EXIT_CODE%

:cli
title Job Monitor CLI
echo Job Monitor - Command Line Mode
python main.py
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" echo Monitor exited with code %APP_EXIT_CODE%.
pause
exit /b %APP_EXIT_CODE%
