# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build recipe for the Bullwheel USB Print Service.
# Produces a single windowless exe: dist/BullwheelUSBPrintService.exe
# Build with: uv run pyinstaller usb_print_service.spec --noconfirm

a = Analysis(
    ['src/usb_print_service.py'],
    pathex=[],
    binaries=[],
    # Bundle the app icon so the tray can load it at runtime (unpacked under
    # sys._MEIPASS/assets in the onefile exe).
    datas=[('assets/ski_lift_chair.ico', 'assets')],
    # pystray selects its platform backend with a dynamic import, so name the
    # Windows backend explicitly rather than relying on hook coverage.
    hiddenimports=['pystray._win32'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='BullwheelUSBPrintService',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Windowless: the service lives in the task tray, so it must never open a
    # console window. Fatal startup errors surface in a message box instead.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # The exe's file icon, as shown in Explorer and the taskbar. The tray icon is
    # the same file, loaded at runtime from the bundled copy above.
    icon=['assets/ski_lift_chair.ico'],
)
