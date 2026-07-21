@echo off
REM Bullwheel USB Print Service launcher — runs the service from source, for development.
REM Deployed computers should run the PyInstaller exe instead (see build_usb_print_service.bat).
REM Runs via uv, using pythonw so no console window appears.

cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo uv was not found on PATH. Install it from https://docs.astral.sh/uv/ and try again.
    pause
    exit /b 1
)

if not exist "src\usb_print_service.py" (
    echo src\usb_print_service.py was not found under this folder: %~dp0
    pause
    exit /b 1
)

start "" /b uv run pythonw src/usb_print_service.py
exit /b 0
