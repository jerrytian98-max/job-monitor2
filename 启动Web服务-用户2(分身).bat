@echo off
setlocal
cd /d "%~dp0"
title Job Monitor - User 2
set "JOB_PROFILE=user2"
set "FLASK_PORT=5001"
echo =========================================
echo Job Monitor Web - User 2
echo Open: http://127.0.0.1:5001
echo Press Ctrl+C to stop the service.
echo =========================================
python app.py
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" echo Service exited with code %APP_EXIT_CODE%.
pause
exit /b %APP_EXIT_CODE%
