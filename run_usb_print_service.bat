@echo off
REM Bullwheel USB Print Service launcher
REM Place this file in the same folder as usb_print_service.py
REM Runs via uv, using pythonw so no console window appears.

cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo uv was not found on PATH. Install it from https://docs.astral.sh/uv/ and try again.
    pause
    exit /b 1
)

if not exist "usb_print_service.py" (
    echo usb_print_service.py was not found in this folder: %~dp0
    pause
    exit /b 1
)

start "" /b uv run pythonw usb_print_service.py
exit /b 0
