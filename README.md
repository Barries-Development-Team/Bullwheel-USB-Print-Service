# Bullwheel USB Print Service

A small Windows-side relay that lets Bullwheel print to a **USB-connected Zebra printer**
as if it were a networked one.

Bullwheel's `ZebraPrinter` handler always prints by opening a TCP socket and sending raw
ZPL. For a **Network** printer it dials the printer's own `:9100` listener. For a **USB**
printer it dials this service on the connected computer instead, and this service forwards
the ZPL to the local printer through the **Windows print spooler (RAW pass-through)**.

```
Bullwheel server ──TCP :9100──▶ USB Print Service ──win32print RAW──▶ USB Zebra printer
```

The service runs as a **task-tray application**: its ski-lift-chair icon
(`assets/ski_lift_chair.ico`, also the exe's file icon) appears in the notification
area, and everything — picking the target printer, starting at logon, opening the
log — is done from its right-click menu.

The service is distributed as a **standalone exe built with PyInstaller**
(`BullwheelUSBPrintService.exe`), so the computers it runs on need no Python
installation — copy the exe over and run it.

## Requirements

- Windows, with the Zebra printer installed and printing from a normal Windows app.
- Nothing else on the target computer — the exe bundles Python and every dependency.

To **build** the exe or run from source you additionally need
[uv](https://docs.astral.sh/uv/), which installs Python and the dependencies
(`pywin32` for spooler access, `pystray` + `Pillow` for the tray icon, and
`pyinstaller` for building):

```
uv sync
```

## Building the exe

Double-click `build_usb_print_service.bat`, or run the same steps by hand:

```
uv sync
uv run pyinstaller usb_print_service.spec --noconfirm
```

The build recipe is checked in as `usb_print_service.spec`; it produces a single
windowless exe at `dist\BullwheelUSBPrintService.exe`, stamped with the app icon and
with a copy of it bundled inside for the tray. That one file is the whole
deployment — copy it to the computer the printer is attached to and run it.

> Build on the same architecture you deploy to (a normal 64-bit Windows machine).
> PyInstaller does not cross-compile, so the exe must be built on Windows.

## Running

Run `BullwheelUSBPrintService.exe`. During development, run from source instead:

```
uv run python src/usb_print_service.py
```

Either way the icon appears in the task tray (check the overflow chevron ^ if it is
hidden), and the service starts listening immediately.

### Tray menu

Right-click the icon:

| Item | Behavior |
|---|---|
| **Forwarding to: …** | Shows the printer the next job will print to. |
| **Target Printer ▸** | Lists every installed printer plus **System Default**. Click one to switch — it takes effect on the very next job, and the choice is **saved** and restored on the next run. The list is refreshed each time the menu opens. |
| **Start with Windows** | Toggles starting the service automatically at logon (see below). |
| **Open Log File** | Opens the job log in your default text viewer. |
| **Exit** | Stops the service. |

The saved printer selection and the log live in
`%APPDATA%\Bullwheel\USB Print Service\` (`settings.json`, `usb_print_service.log`).

## Start at logon

Tick **Start with Windows** in the tray menu (or run
`BullwheelUSBPrintService.exe --install-startup`). This writes a per-user Run registry
entry — no administrator rights needed — that launches the exe at logon; the exe is
windowless, so nothing flashes on screen. Untick the menu item (or
`--uninstall-startup`) to remove it. When running from source, the same toggle
registers the script under `pythonw.exe` instead.

> If you later move or replace the exe (or, from source, move the script or reinstall
> Python), toggle **Start with Windows** off and on again to refresh the registered path.

## Options

| Flag | Default | Notes |
|---|---|---|
| `--host` | `0.0.0.0` | Listen address (all interfaces). |
| `--port` | `9100` | **Must stay 9100** — it must match `ZebraPrinter.USB_PRINT_SERVICE_PORT`. |
| `--printer` | saved tray selection, then system default | Windows printer queue name. Overrides the saved selection for this run only. |
| `--headless` | off | Run without the tray icon — for Task Scheduler or debugging. From source this logs to the console; the exe is windowless and has no console, so it logs to the log file only. |
| `--install-startup` / `--uninstall-startup` | — | Add / remove the start-at-logon registration from the command line, then exit. |

## Finding the printer name

The **Target Printer** tray menu lists the installed printers — normally you never need to
look names up by hand. They are the Windows queue names shown in
*Settings → Bluetooth & devices → Printers & scanners*; to list them from Python:

```python
import win32print
print([p[2] for p in win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL)])
```

## Wiring it up in Bullwheel

On the **Label Printer** record:

1. Set **Connection Method** to `USB`.
2. Set **Connected Computer Address** to this computer's IP or hostname (reachable from the
   Bullwheel server).

Then **Test Connection** / **Print Label** work the same as for network printers.

## Networking

Allow inbound **TCP 9100** through Windows Firewall on this machine. Give the computer a
**static IP or DHCP reservation** so `Connected Computer Address` stays valid.

## Limitations

- **Send-only.** The service does not read status back from the printer, so a USB printer's
  **Test Connection** reports *"reachable, status unknown"* — it confirms the service is up
  and the printer queue accepts the job, but not paper/head state. (Network printers still
  get full `~HS` status.)
- Handles one job at a time (correct for a single printer; jobs never interleave).
- One instance per computer: starting a second copy while one is already running shows
  an "already running" error dialog and exits without starting.
