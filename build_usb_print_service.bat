@echo off
REM Bullwheel USB Print Service exe build
REM Builds dist\BullwheelUSBPrintService.exe with PyInstaller, via uv.
REM Run from the repository root (double-clicking works).

cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo uv was not found on PATH. Install it from https://docs.astral.sh/uv/ and try again.
    pause
    exit /b 1
)

echo Installing dependencies...
uv sync
if errorlevel 1 (
    echo uv sync failed.
    pause
    exit /b 1
)

echo Building the exe...
uv run pyinstaller usb_print_service.spec --noconfirm
if errorlevel 1 (
    echo PyInstaller build failed.
    pause
    exit /b 1
)

echo.
echo Build complete: %~dp0dist\BullwheelUSBPrintService.exe
pause
exit /b 0
