@echo off
setlocal
cd /d "%~dp0"
title Job Monitor Web
set "JOB_PROFILE="
set "FLASK_PORT=5000"
echo =========================================
echo Job Monitor Web
echo Open: http://127.0.0.1:5000
echo Press Ctrl+C to stop the service.
echo =========================================
python app.py
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" echo Service exited with code %APP_EXIT_CODE%.
pause
exit /b %APP_EXIT_CODE%
