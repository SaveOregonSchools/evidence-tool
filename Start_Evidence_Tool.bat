@echo off
setlocal
title Evidence Tool

cd /d "%~dp0"

echo Starting Evidence Tool from:
echo %CD%
echo.
echo Leave this window open while using the app.
echo Press Ctrl+C in this window to stop the app.
echo.

REM Open the browser after a short delay so Flask has time to start.
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:5000'"

REM Prefer the local virtual environment if it exists.
if exist ".venv\Scripts\python.exe" (
    echo Using local virtual environment: .venv
    ".venv\Scripts\python.exe" app.py
) else (
    echo No .venv found. Using Windows Python launcher:
    py app.py
)

echo.
echo Evidence Tool has stopped.
pause