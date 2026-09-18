<img width="128" height="128" alt="ski_lift_chair_icon" src="https://github.com/user-attachments/assets/b0898516-f6e3-4056-b301-c161b5393a5b" />

# Bullwheel USB Print Service

A small Windows-side relay that lets Bullwheel print to a **USB-connected Zebra printer**.
It runs two listeners side by side, for Bullwheel's two ways of reaching a USB printer:

| Method | Who connects | Transport | How it reaches this service |
|---|---|---|---|
| **USB** | The Frappe **server** | Raw TCP, port **9100** | `ZebraPrinter` opens a socket to this computer instead of a networked printer's own `:9100` listener. |
| **Browser** | The user's **browser**, on this same computer | HTTP `POST /print`, `127.0.0.1:9110` | Bullwheel has already rendered the ZPL client-side and posts it straight here. |

In both cases Bullwheel has already rendered the ZPL — this service never renders
templates or looks up data. It receives finished ZPL and forwards it to the printer
unchanged through the **Windows print spooler (RAW pass-through)**.

```
Bullwheel server (Docker host)  ──TCP :9100───────▶ USB Print Service ──win32print RAW──▶ USB Zebra printer
Bullwheel in the browser        ──HTTP :9110/print─▶ USB Print Service ──win32print RAW──▶ USB Zebra printer
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
| `--host` | `0.0.0.0` | USB listener's bind address (all interfaces) — the Frappe server connects here over the network. |
| `--port` | `9100` | USB listener's port. **Must stay 9100** — it must match `ZebraPrinter.USB_PRINT_SERVICE_PORT`. |
| `--http-port` | `9110` | Browser listener's port on `127.0.0.1`. Must match the port in `BROWSER_PRINT_SERVICE_URL` in Bullwheel's `printing.js`. |
| `--printer` | saved tray selection, then system default | Windows printer queue name. Overrides the saved selection for this run only. |
| `--headless` | off | Run without the tray icon — for Task Scheduler or debugging. From source this logs to the console; the exe is windowless and has no console, so it logs to the log file only. Both listeners still run. |
| `--install-startup` / `--uninstall-startup` | — | Add / remove the start-at-logon registration from the command line, then exit. |

## Configuration

Most configuration is the tray's **Target Printer** menu (the default Windows printer).
Two more settings — the Browser method's printer mapping and its allowed origins — are
edited directly in `%APPDATA%\Bullwheel\USB Print Service\settings.json`, since they
don't fit a simple menu. Restart the service after editing them by hand.

```json
{
	"printer_name": "ZD421-Front",
	"printer_mapping": {
		"Front Desk": "ZD421-Front",
		"Rental Counter": "ZD421-Rental"
	},
	"allowed_origins": [
		"https://joesskiandsports.bullwheelapp.com",
		"http://barriesdev.localhost:8000"
	]
}
```

| Key | Used by | Meaning |
|---|---|---|
| `printer_name` | Both methods | The default target Windows printer — same as the tray's **Target Printer** selection; editing it here is equivalent. |
| `printer_mapping` | Browser method | Maps a Bullwheel `Label Printer`'s name to a Windows printer on **this** computer. One Browser `Label Printer` record can be shared by many computers, each with its own Zebra printer, so each computer's service resolves the same name to its own queue. If a `printer_name` from Bullwheel isn't in the mapping, the default printer is used instead — for a computer with a single Zebra printer, a default alone is enough and this key can be omitted. |
| `allowed_origins` | Browser method | The exact browser origins (scheme + host + port, e.g. `https://your-bullwheel-host`) allowed to call the Browser endpoint. **Required** — with none configured, every Browser-method request is rejected with `403` and nothing prints. Never add an origin you don't control: any page open at an allowed origin can print to this computer while the service is running. |

## Browser method — how it works

The user's browser, on the same computer as this service, POSTs ZPL that Bullwheel has
already rendered:

```
POST http://127.0.0.1:9110/print
Content-Type: application/json

{"printer_name": "Front Desk", "media_type": "Direct Thermal", "dpi": 203, "zpl": "^XA...^XZ"}
```

- The listener binds to **127.0.0.1 only** — it is never reachable from the network,
  only from browsers open on this computer.
- `printer_name` is resolved through `printer_mapping`, falling back to the default
  printer; `media_type` and `dpi` are logged but don't change how the job is sent.
- A successful job returns `204`; any failure returns a non-2xx status with a short
  plain-text message that Bullwheel shows to the user (e.g. a printer offline message).
- Because the page and the service are different origins, and the request is JSON, the
  browser sends a CORS preflight (`OPTIONS /print`) first. The service answers it with
  `Access-Control-Allow-Private-Network: true`, which Chrome and Edge require before a
  public HTTPS page is allowed to call a loopback address — without it the browser
  blocks the request before it reaches this service.
- A request from an origin not in `allowed_origins` is rejected with `403` before
  anything is sent to a printer.

If the service isn't running, or the port is blocked, Bullwheel tells the user the
Bullwheel Print Service must be running on this computer.

## Finding the printer name

The **Target Printer** tray menu lists the installed printers — normally you never need to
look names up by hand. They are the Windows queue names shown in
*Settings → Bluetooth & devices → Printers & scanners*; to list them from Python:

```python
import win32print
print([p[2] for p in win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL)])
```

## Wiring it up in Bullwheel

On the **Label Printer** record, depending on the connection method:

**USB** (server connects over the network):
1. Set **Connection Method** to `USB`.
2. Set **Connected Computer Address** to this computer's IP or hostname (reachable from the
   Bullwheel server).

Then **Test Connection** / **Print Label** work the same as for network printers.

**Browser** (the user's browser connects, on this same computer):
1. Set **Connection Method** to `Browser`.
2. Add this computer's Bullwheel origin(s) to `allowed_origins` in `settings.json` (see
   Configuration above) and restart the service.

Printing then works from any Bullwheel page open on this computer, once the user picks
this Label Printer.

## Networking

- **USB method:** allow inbound **TCP 9100** through Windows Firewall on this machine.
  Give the computer a **static IP or DHCP reservation** so `Connected Computer Address`
  stays valid.
- **Browser method:** the HTTP listener on port **9110** binds to `127.0.0.1` only, so it
  is not reachable from the network and needs no firewall rule. Confirm it isn't
  reachable from another computer as part of testing.

## Limitations

- **Send-only.** The service does not read status back from the printer, so a USB printer's
  **Test Connection** reports *"reachable, status unknown"* — it confirms the service is up
  and the printer queue accepts the job, but not paper/head state. (Network printers still
  get full `~HS` status.)
- Handles one job at a time per printer (jobs to different printers can run concurrently;
  jobs to the same printer are serialized so bytes never interleave).
- One instance per computer: starting a second copy while one is already running shows
  an "already running" error dialog and exits without starting. Both listeners run from
  that one instance.
