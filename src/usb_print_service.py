# Copyright (c) 2026, Barrie's Ski and Sports and contributors
# For license information, please see license.txt

"""
Bullwheel USB Print Service.

Listens for raw ZPL print jobs over TCP and forwards them to a USB-connected Zebra
printer through the Windows print spooler (RAW pass-through). This lets the Bullwheel
server print to USB printers exactly as it prints to networked ones: the ZebraPrinter
handler opens a socket to this service's host and port instead of to a printer's own
:9100 listener, and this service relays the bytes to the local USB device.

The service runs as a Windows task-tray application. Right-clicking the tray icon
shows the current target, lets the user switch the target printer (the choice is
saved and restored on the next run), toggles starting the service automatically at
logon, and opens the log file. Passing --headless runs the original console-only
behavior instead, with no tray icon.

The service is send-only — it does not read status back from the printer — so a printer
reached this way reports "reachable, status unknown" from a ~HS host-status check.

The service is normally deployed as a standalone exe built with PyInstaller (see the
README's Building section), so target computers need no Python installation. Running
the script directly behaves identically and is the usual way to work on it.

Usage:
    BullwheelUSBPrintService.exe [--host 0.0.0.0] [--port 9100] [--printer "<name>"]
                                 [--headless] [--install-startup] [--uninstall-startup]
    uv run python src/usb_print_service.py [same options]

If --printer is omitted, the printer last selected from the tray menu is used, falling
back to the Windows default printer. The --port must match the port the ZebraPrinter
handler dials (9100) and the address must match the `connected_computer_address` set
on the Label Printer record in Bullwheel.
"""

import argparse
import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import winreg

try:
	import win32api
	import win32event
	import win32print
	import winerror
except ImportError:
	sys.exit("pywin32 is required to run this service. Install the project dependencies with: uv sync")

try:
	import pystray
	from PIL import Image, ImageDraw

	TRAY_SUPPORT_AVAILABLE = True
except ImportError:
	TRAY_SUPPORT_AVAILABLE = False


APPLICATION_NAME = "Bullwheel USB Print Service"

LISTEN_BACKLOG = 5
CONNECTION_IDLE_TIMEOUT = 30  # seconds a single connection may stall before it is dropped
RECEIVE_BUFFER_SIZE = 4096

# Settings and logs live under %APPDATA% because the service normally runs windowless
# (pythonw at logon) with no console and no fixed working directory.
APPLICATION_DATA_DIRECTORY = os.path.join(
	os.environ.get("APPDATA", os.path.expanduser("~")), "Bullwheel", "USB Print Service"
)
SETTINGS_FILE_PATH = os.path.join(APPLICATION_DATA_DIRECTORY, "settings.json")
LOG_FILE_PATH = os.path.join(APPLICATION_DATA_DIRECTORY, "usb_print_service.log")

# The per-user Run key: entries here are launched by Windows at logon without
# requiring administrator rights or a Task Scheduler entry.
STARTUP_RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_RUN_VALUE_NAME = APPLICATION_NAME

# Named mutex used to detect a second instance starting while one is already running.
# Session-local (no "Global\" prefix) since the service is a per-user, no-admin-rights
# app used by one interactive session at a time.
SINGLE_INSTANCE_MUTEX_NAME = f"{APPLICATION_NAME}_SingleInstance"
_single_instance_mutex_handle = None  # kept alive for the process lifetime; see is_another_instance_running

# The app icon, shared with the exe itself: the PyInstaller spec stamps this same
# .ico onto the exe and bundles a copy for the tray. A frozen build unpacks bundled
# files under sys._MEIPASS; a source checkout resolves it from the repository root
# (this file's parent's parent).
TRAY_ICON_FILE_PATH = os.path.join(
	getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
	"assets",
	"ski_lift_chair.ico",
)

logger = logging.getLogger("bullwheel_usb_print_service")


# ─── Logging ──────────────────────────────────────────────────────


def configure_logging() -> None:
	"""Send log lines to a rotating file in %APPDATA% — the service usually runs
	windowless via pythonw, so a console is not guaranteed — and mirror them to the
	console when one is attached (headless mode, or running from a terminal)."""
	os.makedirs(APPLICATION_DATA_DIRECTORY, exist_ok=True)
	formatter = logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

	file_handler = logging.handlers.RotatingFileHandler(
		LOG_FILE_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
	)
	file_handler.setFormatter(formatter)
	logger.addHandler(file_handler)

	if sys.stderr is not None:
		console_handler = logging.StreamHandler()
		console_handler.setFormatter(formatter)
		logger.addHandler(console_handler)

	logger.setLevel(logging.INFO)


# ─── Saved Settings ───────────────────────────────────────────────


def load_saved_printer_name() -> str | None:
	"""Return the printer name persisted by a previous tray selection, or None when
	nothing has been saved yet (the service then follows the Windows default printer)."""
	try:
		with open(SETTINGS_FILE_PATH, encoding="utf-8") as settings_file:
			settings = json.load(settings_file)
		return settings.get("printer_name") or None
	except (OSError, ValueError):
		return None


def save_printer_name(printer_name: str | None) -> None:
	"""Persist the selected printer so the tray choice survives restarts and logons.
	Saving None records that the service should follow the Windows default printer."""
	os.makedirs(APPLICATION_DATA_DIRECTORY, exist_ok=True)
	with open(SETTINGS_FILE_PATH, "w", encoding="utf-8") as settings_file:
		json.dump({"printer_name": printer_name}, settings_file, indent="\t")


# ─── Printers ─────────────────────────────────────────────────────


def list_installed_printers() -> list[str]:
	"""Return the queue names of every printer installed on this computer, including
	connected network printers, sorted so the tray menu order is stable."""
	enumeration_flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
	return sorted(printer[2] for printer in win32print.EnumPrinters(enumeration_flags))


def send_to_printer(printer_name: str, data: bytes) -> None:
	"""Forward raw bytes to the named Windows printer as a RAW spooler job, so the ZPL
	reaches the printer verbatim without the driver reformatting or interpreting it."""
	printer_handle = win32print.OpenPrinter(printer_name)
	try:
		win32print.StartDocPrinter(printer_handle, 1, ("Bullwheel Label", None, "RAW"))
		try:
			win32print.StartPagePrinter(printer_handle)
			win32print.WritePrinter(printer_handle, data)
			win32print.EndPagePrinter(printer_handle)
		finally:
			win32print.EndDocPrinter(printer_handle)
	finally:
		win32print.ClosePrinter(printer_handle)


def receive_job(connection: socket.socket) -> bytes:
	"""Read an entire print job from a client connection, returning every byte received
	until the client closes the connection or it stalls past the idle timeout."""
	connection.settimeout(CONNECTION_IDLE_TIMEOUT)
	received = b""
	while True:
		try:
			chunk = connection.recv(RECEIVE_BUFFER_SIZE)
		except TimeoutError:
			break
		if not chunk:
			break
		received += chunk
	return received


# ─── Service ──────────────────────────────────────────────────────


class USBPrintService:
	"""Owns the TCP listener and the mutable printer target. The tray menu changes the
	target through set_printer_name while the server thread resolves it per job, so a
	new selection applies to the very next job without restarting the service."""

	def __init__(self, host: str, port: int, printer_name: str | None):
		self.host = host
		self.port = port
		self.printer_name = printer_name  # None → follow the Windows default printer
		self.listener = None
		self.tray_icon = None  # set by run_tray_icon; used for failure notifications

	def resolve_printer_name(self) -> str | None:
		"""Return the queue the next job will print to — the selected printer, or the
		Windows default when no selection has been made. Returns None when there is no
		selection and no default printer exists."""
		if self.printer_name:
			return self.printer_name
		try:
			return win32print.GetDefaultPrinter()
		except Exception:
			return None

	def set_printer_name(self, printer_name: str | None) -> None:
		"""Switch the target printer and persist the choice; it takes effect on the
		next job. None selects the Windows default printer."""
		self.printer_name = printer_name
		save_printer_name(printer_name)
		logger.info(f"Target printer changed to '{printer_name or 'system default'}'.")

	def start_listening(self) -> None:
		"""Bind and listen on the configured host and port, raising OSError on failure —
		most commonly the port is already taken by another running instance."""
		self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		self.listener.bind((self.host, self.port))
		self.listener.listen(LISTEN_BACKLOG)
		logger.info(
			f"{APPLICATION_NAME} listening on {self.host}:{self.port}, "
			f"forwarding to printer '{self.resolve_printer_name()}'."
		)

	def serve_forever(self) -> None:
		"""Accept connections and forward each received job to the current target
		printer, running until the process exits. Each connection is handled to
		completion before the next is accepted so raw jobs never interleave."""
		while True:
			connection, client_address = self.listener.accept()
			client = f"{client_address[0]}:{client_address[1]}"
			try:
				data = receive_job(connection)
				if not data:
					logger.info(f"Empty job from {client} — nothing to print.")
					continue
				printer_name = self.resolve_printer_name()
				if not printer_name:
					raise RuntimeError("no target printer is selected and Windows has no default printer")
				send_to_printer(printer_name, data)
				logger.info(f"Printed {len(data)} bytes from {client} to '{printer_name}'.")
			except Exception as error:
				# Never let one bad job take the service down.
				logger.error(f"Failed to handle job from {client}: {error}")
				self.notify(f"Print job failed: {error}")
			finally:
				connection.close()

	def notify(self, message: str) -> None:
		"""Show a best-effort tray notification so failures are visible even though the
		service has no console window. Silently does nothing in headless mode."""
		if self.tray_icon is None:
			return
		try:
			self.tray_icon.notify(message, APPLICATION_NAME)
		except Exception:
			pass


# ─── Single Instance ──────────────────────────────────────────────


def is_another_instance_running() -> bool:
	"""Create (or open) the service's named mutex and report whether another process
	already holds it. The handle is kept in _single_instance_mutex_handle for the
	life of this process — releasing it early would let a second instance pass the
	check — so the OS releases it automatically when the process exits."""
	global _single_instance_mutex_handle
	_single_instance_mutex_handle = win32event.CreateMutex(None, False, SINGLE_INSTANCE_MUTEX_NAME)
	return win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS


# ─── Start-at-Logon Registration ──────────────────────────────────


def build_startup_command() -> str:
	"""Build the command Windows runs at logon. A PyInstaller exe registers its own
	path — sys.executable is the exe itself, and it is already windowless. A source
	checkout instead registers the script launched by the windowless pythonw
	interpreter (when available) so no console window appears. The command has no
	--printer argument — the saved tray selection is restored instead."""
	if getattr(sys, "frozen", False):
		return f'"{sys.executable}"'
	interpreter_path = sys.executable
	windowless_interpreter_path = os.path.join(os.path.dirname(interpreter_path), "pythonw.exe")
	if os.path.exists(windowless_interpreter_path):
		interpreter_path = windowless_interpreter_path
	script_path = os.path.abspath(__file__)
	return f'"{interpreter_path}" "{script_path}"'


def is_startup_enabled() -> bool:
	"""Report whether the per-user Run registry entry for this service exists."""
	try:
		with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY_PATH) as run_key:
			winreg.QueryValueEx(run_key, STARTUP_RUN_VALUE_NAME)
		return True
	except OSError:
		return False


def enable_startup() -> None:
	"""Register the service to start automatically at logon by writing a per-user Run
	registry entry — no administrator rights or Task Scheduler entry required."""
	startup_command = build_startup_command()
	with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as run_key:
		winreg.SetValueEx(run_key, STARTUP_RUN_VALUE_NAME, 0, winreg.REG_SZ, startup_command)
	logger.info(f"Registered to start at logon: {startup_command}")


def disable_startup() -> None:
	"""Remove the per-user Run registry entry so the service no longer starts at logon."""
	try:
		with winreg.OpenKey(
			winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
		) as run_key:
			winreg.DeleteValue(run_key, STARTUP_RUN_VALUE_NAME)
	except OSError:
		pass
	logger.info("Removed the start-at-logon registration.")


# ─── Tray Application ─────────────────────────────────────────────


def draw_fallback_tray_image():
	"""Draw a stand-in tray icon in code — a white label tag with barcode stripes —
	used only when the app icon file cannot be loaded."""
	image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
	drawing = ImageDraw.Draw(image)
	drawing.rounded_rectangle(
		(2, 10, 62, 54), radius=8, fill=(245, 246, 248, 255), outline=(60, 64, 72, 255), width=3
	)
	barcode_stripes = [(12, 3), (18, 2), (23, 4), (30, 2), (35, 3), (41, 2), (46, 4)]  # (x, width)
	for stripe_x, stripe_width in barcode_stripes:
		drawing.rectangle((stripe_x, 18, stripe_x + stripe_width, 46), fill=(60, 64, 72, 255))
	return image


def load_tray_image():
	"""Load the ski-lift-chair app icon for the tray, falling back to the drawn
	stand-in so a missing or unreadable icon file cannot keep the service from
	starting."""
	try:
		return Image.open(TRAY_ICON_FILE_PATH)
	except OSError:
		logger.warning(f"Could not load the tray icon from '{TRAY_ICON_FILE_PATH}' — using the built-in stand-in icon.")
		return draw_fallback_tray_image()


def make_printer_menu_item(service: USBPrintService, printer_name: str):
	"""Build one radio menu item that targets the given printer when selected. A factory
	is used so each item's callbacks bind their own printer name rather than sharing the
	enumeration loop's variable."""

	def select_printer(icon, item):
		service.set_printer_name(printer_name)

	def is_selected(item):
		return service.printer_name == printer_name

	return pystray.MenuItem(printer_name, select_printer, checked=is_selected, radio=True)


def build_printer_menu_items(service: USBPrintService):
	"""Yield a system-default entry plus one radio item per installed printer. The tray
	menu calls this every time it opens, so printers installed after startup appear
	without restarting the service."""

	def select_system_default(icon, item):
		service.set_printer_name(None)

	def is_system_default_selected(item):
		return service.printer_name is None

	yield pystray.MenuItem(
		"System Default",
		select_system_default,
		checked=is_system_default_selected,
		radio=True,
	)
	for printer_name in list_installed_printers():
		yield make_printer_menu_item(service, printer_name)


def run_tray_icon(service: USBPrintService) -> None:
	"""Create the task-tray icon and block on its event loop until Exit is chosen.
	The header row shows the live target, Target Printer switches it, Start with
	Windows toggles the logon registration, and Open Log File jumps to the log."""

	def describe_target(item):
		return f"Forwarding to: {service.resolve_printer_name() or 'no printer available'}"

	def toggle_startup(icon, item):
		if is_startup_enabled():
			disable_startup()
		else:
			enable_startup()

	def open_log_file(icon, item):
		os.startfile(LOG_FILE_PATH)

	def exit_service(icon, item):
		logger.info("Exit selected from the tray menu.")
		icon.stop()

	menu = pystray.Menu(
		pystray.MenuItem(describe_target, None, enabled=False),
		pystray.Menu.SEPARATOR,
		pystray.MenuItem("Target Printer", pystray.Menu(lambda: build_printer_menu_items(service))),
		pystray.Menu.SEPARATOR,
		pystray.MenuItem("Start with Windows", toggle_startup, checked=lambda item: is_startup_enabled()),
		pystray.MenuItem("Open Log File", open_log_file),
		pystray.Menu.SEPARATOR,
		pystray.MenuItem("Exit", exit_service),
	)
	tray_icon = pystray.Icon(
		"bullwheel_usb_print_service",
		load_tray_image(),
		f"{APPLICATION_NAME} (port {service.port})",
		menu,
	)
	service.tray_icon = tray_icon
	tray_icon.run()


def show_error_message_box(message: str) -> None:
	"""Show a blocking Windows error dialog, used for fatal startup errors when the
	service runs windowless (pythonw) and has no console to print to."""
	import ctypes

	MB_ICONERROR = 0x00000010
	ctypes.windll.user32.MessageBoxW(None, message, APPLICATION_NAME, MB_ICONERROR)


# ─── Entry Point ──────────────────────────────────────────────────


def main() -> None:
	"""Parse arguments and run the service — as a tray application by default, or as a
	plain console process with --headless. --install-startup and --uninstall-startup
	manage the logon registration from the command line and exit without serving."""
	parser = argparse.ArgumentParser(
		description="Forward raw ZPL print jobs from the network to a USB Zebra printer."
	)
	parser.add_argument("--host", default="0.0.0.0", help="Address to listen on (default: all interfaces).")
	parser.add_argument(
		"--port",
		type=int,
		default=9100,
		help="Port to listen on (default: 9100, must match the Label Printer configuration).",
	)
	parser.add_argument(
		"--printer",
		default=None,
		help="Windows printer name, overriding the saved tray selection for this run only "
		"(default: the printer last selected from the tray, then the system default).",
	)
	parser.add_argument(
		"--headless",
		action="store_true",
		help="Run without the tray icon, logging to the console (the original behavior).",
	)
	parser.add_argument(
		"--install-startup",
		action="store_true",
		help="Register the service to start at logon (per-user Run registry entry), then exit.",
	)
	parser.add_argument(
		"--uninstall-startup",
		action="store_true",
		help="Remove the start-at-logon registration, then exit.",
	)
	arguments = parser.parse_args()

	configure_logging()

	if arguments.install_startup:
		enable_startup()
		return
	if arguments.uninstall_startup:
		disable_startup()
		return

	if is_another_instance_running():
		message = f"{APPLICATION_NAME} is already running."
		logger.error(message)
		if not arguments.headless:
			show_error_message_box(message)
		sys.exit(1)

	printer_name = arguments.printer or load_saved_printer_name()
	if printer_name and printer_name not in list_installed_printers():
		logger.warning(
			f"Configured printer '{printer_name}' is not installed on this computer; "
			"jobs will fail until another printer is selected from the tray menu."
		)

	service = USBPrintService(arguments.host, arguments.port, printer_name)
	try:
		service.start_listening()
	except OSError as error:
		message = (
			f"Could not listen on {arguments.host}:{arguments.port}: {error}\n"
			"Another program may already be using this port."
		)
		logger.error(message)
		if not arguments.headless:
			show_error_message_box(message)
		sys.exit(1)

	run_headless = arguments.headless or not TRAY_SUPPORT_AVAILABLE
	if run_headless and not arguments.headless:
		logger.warning(
			"pystray and Pillow are not installed — running without a tray icon. "
			"Install the project dependencies with: uv sync"
		)

	if run_headless:
		try:
			service.serve_forever()
		except KeyboardInterrupt:
			logger.info("Shutting down.")
	else:
		server_thread = threading.Thread(target=service.serve_forever, name="usb-print-server", daemon=True)
		server_thread.start()
		run_tray_icon(service)
		logger.info("Shutting down.")


if __name__ == "__main__":
	main()
