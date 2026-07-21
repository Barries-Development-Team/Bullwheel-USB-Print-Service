@echo off
REM Bullwheel USB Print Service launcher
REM Place this file in the same folder as usb_print_service.py

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH. Install Python 3 ^(64-bit recommended^) and try again.
    pause
    exit /b 1
)

if not exist "usb_print_service.py" (
    echo usb_print_service.py was not found in this folder: %~dp0
    pause
    exit /b 1
)

echo Starting Bullwheel USB Print Service...
python usb_print_service.py
if errorlevel 1 (
    echo.
    echo The service exited with an error. See usb_print_service.log in:
    echo %%APPDATA%%\Bullwheel\USB Print Service\
    pause
)
