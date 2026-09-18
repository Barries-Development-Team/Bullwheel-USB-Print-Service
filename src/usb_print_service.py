# Copyright (c) 2026, Barrie's Ski and Sports and contributors
# For license information, please see license.txt

"""
Bullwheel USB Print Service.

Runs two listeners side by side and forwards both to USB-connected Zebra printers
through the Windows print spooler (RAW pass-through):

- USB method: a raw TCP listener on port 9100. The Frappe server connects to this
  service exactly as it would to a networked printer — the ZebraPrinter handler opens
  a socket to this service's host and port instead of to a printer's own :9100
  listener — and this service relays the bytes to the local USB device.
- Browser method: an HTTP listener on 127.0.0.1:9110. The user's browser, running
  Bullwheel on the same computer, POSTs already-rendered ZPL to /print (with CORS and
  Private Network Access handling so the production HTTPS site can reach a loopback
  service), and this service forwards it to the local USB device the same way.

Neither listener renders templates or looks up data — both receive finished ZPL and
send it to the printer unchanged.

The service runs as a Windows task-tray application. Right-clicking the tray icon
shows the current target, lets the user switch the default target printer (the choice
is saved and restored on the next run), toggles starting the service automatically at
logon, and opens the log file. Passing --headless runs the original console-only
behavior instead, with no tray icon. Both listeners run in either mode.

The service is send-only — it does not read status back from the printer — so a printer
reached this way reports "reachable, status unknown" from a ~HS host-status check.

The service is normally deployed as a standalone exe built with PyInstaller (see the
README's Building section), so target computers need no Python installation. Running
the script directly behaves identically and is the usual way to work on it.

Usage:
    BullwheelUSBPrintService.exe [--host 0.0.0.0] [--port 9100] [--http-port 9110]
                                 [--printer "<name>"]
                                 [--headless] [--install-startup] [--uninstall-startup]
    uv run python src/usb_print_service.py [same options]

If --printer is omitted, the printer last selected from the tray menu is used, falling
back to the Windows default printer. The --port must match the port the ZebraPrinter
handler dials (9100) and the address must match the `connected_computer_address` set
on the Label Printer record in Bullwheel. The --http-port must match the port in
`BROWSER_PRINT_SERVICE_URL` in Bullwheel's printing.js.

The printer-name mapping (Bullwheel `printer_name` → Windows printer, for the Browser
method) and the allowed browser origins (for CORS) are configured in settings.json —
see the README's Configuration section.
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

try:
	import tkinter as tk

	TKINTER_SUPPORT_AVAILABLE = True
except ImportError:
	TKINTER_SUPPORT_AVAILABLE = False


APPLICATION_NAME = "Bullwheel USB Print Service"

LISTEN_BACKLOG = 5
CONNECTION_IDLE_TIMEOUT = 30  # seconds a single connection may stall before it is dropped
RECEIVE_BUFFER_SIZE = 4096

DEFAULT_HTTP_PORT = 9110
# Loopback only — the Browser endpoint is for browsers on this computer, never the network.
HTTP_HOST = "127.0.0.1"
MAX_HTTP_BODY_SIZE = 25_000_000  # bytes; generous headroom over any realistic ZPL batch

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
#
# settings.json holds everything the README documents as configurable without a code
# change: the default printer (also editable from the tray), the Browser method's
# printer_name → Windows printer mapping, and its allowed CORS origins. The file is
# read at startup (and again whenever the tray changes the default printer); editing
# printer_mapping or allowed_origins by hand takes effect on the next restart.


def load_settings() -> dict:
	"""Return the full saved settings dict, or {} when nothing has been saved yet or
	the file is unreadable/corrupt."""
	try:
		with open(SETTINGS_FILE_PATH, encoding="utf-8") as settings_file:
			settings = json.load(settings_file)
		return settings if isinstance(settings, dict) else {}
	except (OSError, ValueError):
		return {}


def save_settings(settings: dict) -> None:
	"""Write the full settings dict, creating the application data directory if needed."""
	os.makedirs(APPLICATION_DATA_DIRECTORY, exist_ok=True)
	with open(SETTINGS_FILE_PATH, "w", encoding="utf-8") as settings_file:
		json.dump(settings, settings_file, indent="\t")


def load_saved_printer_name() -> str | None:
	"""Return the printer name persisted by a previous tray selection, or None when
	nothing has been saved yet (the service then follows the Windows default printer)."""
	return load_settings().get("printer_name") or None


def save_printer_name(printer_name: str | None) -> None:
	"""Persist the selected printer so the tray choice survives restarts and logons,
	preserving the rest of settings.json. Saving None records that the service should
	follow the Windows default printer."""
	settings = load_settings()
	settings["printer_name"] = printer_name
	save_settings(settings)


def load_printer_mapping() -> dict[str, str]:
	"""Return the configured Bullwheel printer_name → Windows printer mapping, used to
	resolve Browser-method jobs. Empty when nothing is configured, so every Browser job
	falls back to the default printer — fine for a computer with a single Zebra printer."""
	mapping = load_settings().get("printer_mapping")
	return mapping if isinstance(mapping, dict) else {}


def load_allowed_origins() -> list[str]:
	"""Return the configured list of browser origins allowed to call the Browser
	method's HTTP endpoint. Empty means no browser origin is allowed — the Browser
	method won't work until this is configured, which is deliberate: without an
	allow-list any website the user visits could send print jobs to their printer."""
	origins = load_settings().get("allowed_origins")
	return [origin for origin in origins if isinstance(origin, str)] if isinstance(origins, list) else []


def save_allowed_origins(allowed_origins: list[str]) -> None:
	"""Persist the allowed-origins list, preserving the rest of settings.json. Called
	whenever the tray's Allowed Origins menu adds or removes one, so the change
	survives a restart."""
	settings = load_settings()
	settings["allowed_origins"] = allowed_origins
	save_settings(settings)


def normalize_origin(raw_origin: str) -> str:
	"""Parse user-entered text into a bare origin (scheme://host[:port]), the form
	browsers send in the Origin header and the only form the allow-list should hold.
	Raises ValueError with a user-facing message when the text isn't a usable origin."""
	from urllib.parse import urlsplit

	raw_origin = raw_origin.strip()
	if not raw_origin:
		raise ValueError("Enter an origin, e.g. https://your-bullwheel-host.")
	parsed = urlsplit(raw_origin)
	if parsed.scheme not in ("http", "https"):
		raise ValueError("The origin must start with http:// or https://.")
	if not parsed.netloc:
		raise ValueError("The origin must include a host, e.g. https://your-bullwheel-host.")
	return f"{parsed.scheme}://{parsed.netloc}"


# ─── Printers ─────────────────────────────────────────────────────


def list_installed_printers() -> list[str]:
	"""Return the queue names of every printer installed on this computer, including
	connected network printers, sorted so the tray menu order is stable."""
	enumeration_flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
	return sorted(printer[2] for printer in win32print.EnumPrinters(enumeration_flags))


_printer_locks: dict[str, threading.Lock] = {}
_printer_locks_guard = threading.Lock()


def get_printer_lock(printer_name: str) -> threading.Lock:
	"""Return the lock guarding writes to the named Windows printer, creating it on
	first use. USB jobs and Browser jobs can target the same printer from different
	threads at the same moment; this serializes them per-printer so one job's spooler
	call always completes before the next starts, without blocking unrelated printers."""
	with _printer_locks_guard:
		return _printer_locks.setdefault(printer_name, threading.Lock())


def send_to_printer(printer_name: str, data: bytes) -> None:
	"""Forward raw bytes to the named Windows printer as a single RAW spooler job, so
	the ZPL reaches the printer verbatim without the driver reformatting or
	interpreting it, and without interleaving with any other job on the same printer."""
	with get_printer_lock(printer_name):
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
	"""Owns the TCP listener and the mutable printer target and browser allow-list. The
	tray menu changes these through set_printer_name / add_allowed_origin /
	remove_allowed_origin while the server threads read them per job, so a change
	applies to the very next job without restarting the service."""

	def __init__(
		self,
		host: str,
		port: int,
		printer_name: str | None,
		printer_mapping: dict[str, str] | None = None,
		allowed_origins: list[str] | None = None,
	):
		self.host = host
		self.port = port
		self.printer_name = printer_name  # None → follow the Windows default printer
		self.printer_mapping = printer_mapping or {}  # Browser printer_name → Windows printer
		self.allowed_origins = list(allowed_origins) if allowed_origins else []  # Browser method's CORS allow-list
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

	def resolve_windows_printer(self, bullwheel_printer_name: str | None) -> str | None:
		"""Resolve a Bullwheel-side printer_name (Browser method) to a Windows printer:
		the configured mapping entry first, then the default target. Used for USB jobs
		too, with bullwheel_printer_name always None, so they only ever use the default."""
		if bullwheel_printer_name and bullwheel_printer_name in self.printer_mapping:
			return self.printer_mapping[bullwheel_printer_name]
		return self.resolve_printer_name()

	def set_printer_name(self, printer_name: str | None) -> None:
		"""Switch the target printer and persist the choice; it takes effect on the
		next job. None selects the Windows default printer."""
		self.printer_name = printer_name
		save_printer_name(printer_name)
		logger.info(f"Target printer changed to '{printer_name or 'system default'}'.")

	def add_allowed_origin(self, origin: str) -> bool:
		"""Add a browser origin to the Browser method's CORS allow-list and persist it,
		taking effect on the very next request. Returns False without changing anything
		if the origin is already allowed."""
		if origin in self.allowed_origins:
			return False
		self.allowed_origins.append(origin)
		save_allowed_origins(self.allowed_origins)
		logger.info(f"Allowed origin added: '{origin}'.")
		self.refresh_tray_menu()
		return True

	def remove_allowed_origin(self, origin: str) -> None:
		"""Remove a browser origin from the allow-list and persist it, taking effect on
		the very next request."""
		if origin not in self.allowed_origins:
			return
		self.allowed_origins.remove(origin)
		save_allowed_origins(self.allowed_origins)
		logger.info(f"Allowed origin removed: '{origin}'.")
		self.refresh_tray_menu()

	def refresh_tray_menu(self) -> None:
		"""Rebuild the tray menu so it reflects the current settings. On Windows pystray
		builds the native menu ahead of time rather than each time it opens, so a change
		made outside its own click handling — such as the Add Origin dialog, which runs on
		its own thread — doesn't appear until this is called."""
		if self.tray_icon is not None:
			self.tray_icon.update_menu()

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
					logger.info(f"[USB] Empty job from {client} — nothing to print.")
					continue
				printer_name = self.resolve_printer_name()
				if not printer_name:
					raise RuntimeError("no target printer is selected and Windows has no default printer")
				send_to_printer(printer_name, data)
				logger.info(f"[USB] Printed {len(data)} bytes from {client} to '{printer_name}'.")
			except Exception as error:
				# Never let one bad job take the service down.
				logger.error(f"[USB] Failed to handle job from {client}: {error}")
				self.notify(f"USB print job failed: {error}")
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


# ─── Browser Method — HTTP Listener ───────────────────────────────
#
# The user's browser, running Bullwheel on this same computer, POSTs already-rendered
# ZPL to /print. Unlike the USB listener this must handle CORS (the page and the
# service are different origins) and Chrome/Edge Private Network Access (a public
# HTTPS page calling a loopback address) or the browser blocks the request before it
# ever reaches here.


def is_origin_allowed(origin: str | None, allowed_origins: list[str]) -> bool:
	"""Return whether the given Origin header value is on the configured allow-list.
	An endpoint that accepted any origin would let any website the user visits print to
	their printer, so this is a hard allow-list match, never a wildcard."""
	return origin is not None and origin in allowed_origins


class PrintRequestError(Exception):
	"""A Browser-method request that should be rejected with a specific HTTP status
	and a short, human-readable message — the message becomes Bullwheel's error text."""

	def __init__(self, status_code: int, message: str):
		super().__init__(message)
		self.status_code = status_code
		self.message = message


def make_browser_print_handler(service: "USBPrintService"):
	"""Build the BaseHTTPRequestHandler subclass used by the Browser method's HTTP
	server, bound to this service instance. Reads service.allowed_origins fresh on
	every request (rather than capturing a snapshot) so the tray's Allowed Origins menu
	takes effect on the very next request, with no restart. A factory is used because
	http.server instantiates a fresh handler per request and only takes a class, not an
	already-constructed object."""

	class BrowserPrintRequestHandler(BaseHTTPRequestHandler):
		server_version = "BullwheelUSBPrintService/1.0"

		def log_message(self, format, *args):  # noqa: A002 — BaseHTTPRequestHandler's signature
			# Route the built-in per-request access log through our own logger instead
			# of stderr, and skip it entirely — do_POST/do_OPTIONS already log outcomes.
			pass

		def _send_cors_headers(self, origin: str | None) -> None:
			if is_origin_allowed(origin, service.allowed_origins):
				self.send_header("Access-Control-Allow-Origin", origin)
			self.send_header("Vary", "Origin")

		def _send_text_response(self, status_code: int, body: str, origin: str | None) -> None:
			body_bytes = body.encode("utf-8")
			self.send_response(status_code)
			self._send_cors_headers(origin)
			self.send_header("Content-Type", "text/plain; charset=utf-8")
			self.send_header("Content-Length", str(len(body_bytes)))
			self.end_headers()
			if body_bytes:
				self.wfile.write(body_bytes)

		def do_OPTIONS(self) -> None:
			"""Answer the CORS/Private Network Access preflight the browser sends before
			the real POST, since the request carries a JSON Content-Type."""
			origin = self.headers.get("Origin")
			if self.path != "/print":
				self.send_response(204)
				self.end_headers()
				return
			self.send_response(204)
			self._send_cors_headers(origin)
			self.send_header("Access-Control-Allow-Methods", "POST")
			self.send_header("Access-Control-Allow-Headers", "Content-Type")
			self.send_header("Access-Control-Allow-Private-Network", "true")
			self.send_header("Access-Control-Max-Age", "600")
			self.end_headers()

		def do_POST(self) -> None:
			origin = self.headers.get("Origin")
			if self.path != "/print":
				self._send_text_response(404, "Not found.", origin)
				return
			try:
				self._handle_print(origin)
			except PrintRequestError as error:
				logger.error(f"[Browser] Rejected request from origin '{origin}': {error.message}")
				self._send_text_response(error.status_code, error.message, origin)
			except Exception as error:
				logger.error(f"[Browser] Unexpected error handling request from origin '{origin}': {error}")
				self._send_text_response(500, "Unexpected error handling the print job.", origin)

		def _handle_print(self, origin: str | None) -> None:
			if origin is not None and not is_origin_allowed(origin, service.allowed_origins):
				raise PrintRequestError(403, "This origin is not allowed to print.")

			content_type = self.headers.get("Content-Type", "")
			if content_type.split(";")[0].strip().lower() != "application/json":
				raise PrintRequestError(415, "Content-Type must be application/json.")

			try:
				content_length = int(self.headers.get("Content-Length", "0"))
			except ValueError:
				raise PrintRequestError(400, "Missing or invalid Content-Length.")
			if content_length <= 0:
				raise PrintRequestError(400, "Request body is empty.")
			if content_length > MAX_HTTP_BODY_SIZE:
				raise PrintRequestError(400, "Request body is too large.")
			raw_body = self.rfile.read(content_length)

			try:
				payload = json.loads(raw_body.decode("utf-8"))
			except (UnicodeDecodeError, ValueError):
				raise PrintRequestError(400, "Request body is not valid JSON.")
			if not isinstance(payload, dict):
				raise PrintRequestError(400, "Request body must be a JSON object.")

			bullwheel_printer_name = payload.get("printer_name")
			if not isinstance(bullwheel_printer_name, str) or not bullwheel_printer_name:
				raise PrintRequestError(400, "Missing or invalid 'printer_name'.")
			zpl = payload.get("zpl")
			if not isinstance(zpl, str) or not zpl:
				raise PrintRequestError(400, "Missing or empty 'zpl'.")
			media_type = payload.get("media_type")
			dpi = payload.get("dpi")

			windows_printer_name = service.resolve_windows_printer(bullwheel_printer_name)
			if not windows_printer_name:
				raise PrintRequestError(404, f"No printer is configured for \"{bullwheel_printer_name}\".")

			data = zpl.encode("utf-8")
			try:
				send_to_printer(windows_printer_name, data)
			except Exception as error:
				raise PrintRequestError(503, f'Printer "{windows_printer_name}" is offline or could not be reached.') from error

			logger.info(
				f"[Browser] Printed {len(data)} bytes (printer_name='{bullwheel_printer_name}', "
				f"media_type={media_type!r}, dpi={dpi!r}) to '{windows_printer_name}'."
			)
			self.send_response(204)
			self._send_cors_headers(origin)
			self.send_header("Content-Length", "0")
			self.end_headers()

		def do_GET(self) -> None:
			self._send_text_response(404, "Not found.", self.headers.get("Origin"))

	return BrowserPrintRequestHandler


def start_browser_print_server(service: "USBPrintService", http_port: int) -> ThreadingHTTPServer:
	"""Start the Browser method's HTTP listener on 127.0.0.1, bound to loopback only —
	this endpoint is for browsers on this computer, never reachable from the network.
	Raises OSError if the port is already taken."""
	if not service.allowed_origins:
		logger.warning(
			"No allowed_origins are configured — every Browser-method request will be "
			"rejected with 403 until at least one origin is added (tray menu: Allowed "
			"Origins ▸ Add Origin…, or edit settings.json directly)."
		)
	handler_class = make_browser_print_handler(service)
	http_server = ThreadingHTTPServer((HTTP_HOST, http_port), handler_class)
	logger.info(f"{APPLICATION_NAME} listening for Browser print jobs on http://{HTTP_HOST}:{http_port}/print.")
	return http_server


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


def prompt_for_origin() -> str | None:
	"""Show a small text-entry dialog asking for a browser origin to allow, returning
	the raw text entered or None if the dialog was cancelled. Requires tkinter, which
	ships with a standard Python install; callers should check TKINTER_SUPPORT_AVAILABLE
	first.

	The dialog is the Tk root itself rather than a simpledialog over a withdrawn root: a
	dialog whose parent is withdrawn never receives keyboard focus when opened from a
	background process, which left the text box dead and the window unclosable. It must
	be called from a thread that is not running the tray's message loop."""
	result: list[str | None] = [None]
	root = tk.Tk()
	root.title(APPLICATION_NAME)
	root.resizable(False, False)

	def accept(event=None):
		result[0] = entry.get()
		root.destroy()

	def cancel(event=None):
		root.destroy()

	tk.Label(
		root,
		text="Browser origin to allow (scheme + host, e.g. https://your-bullwheel-host):",
		anchor="w",
	).pack(fill="x", padx=12, pady=(12, 4))
	entry = tk.Entry(root, width=55)
	entry.pack(fill="x", padx=12)
	button_row = tk.Frame(root)
	button_row.pack(fill="x", padx=12, pady=12)
	tk.Button(button_row, text="Cancel", width=10, command=cancel).pack(side="right")
	tk.Button(button_row, text="OK", width=10, command=accept).pack(side="right", padx=(0, 6))

	root.protocol("WM_DELETE_WINDOW", cancel)
	root.bind("<Return>", accept)
	root.bind("<Escape>", cancel)

	# Centre on screen, then force the window to the foreground so it takes input even
	# though this process was started in the background.
	root.update_idletasks()
	x = (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2
	y = (root.winfo_screenheight() - root.winfo_reqheight()) // 3
	root.geometry(f"+{x}+{y}")
	root.attributes("-topmost", True)
	root.lift()
	root.focus_force()
	entry.focus_set()

	root.mainloop()
	return result[0]


_tray_dialog_lock = threading.Lock()


def run_tray_dialog(name: str, dialog_action) -> None:
	"""Run a dialog-showing action on its own thread so the tray's message loop stays
	responsive while the dialog is open — blocking that loop is what left dialogs
	unresponsive and unclosable. Only one tray dialog can be open at a time; a request
	made while another is showing is ignored."""
	if not _tray_dialog_lock.acquire(blocking=False):
		return

	def run() -> None:
		try:
			dialog_action()
		except Exception:
			logger.exception(f"The {name} dialog failed.")
		finally:
			_tray_dialog_lock.release()

	threading.Thread(target=run, name=f"{name}-dialog", daemon=True).start()


def add_allowed_origin_interactively(service: USBPrintService) -> None:
	"""Handle the tray's Add Origin… command."""
	run_tray_dialog("add-origin", lambda: add_allowed_origin_from_dialog(service))


def add_allowed_origin_from_dialog(service: USBPrintService) -> None:
	"""Prompt for text, validate it as an origin, and add it to the service's
	allow-list, notifying on any problem."""
	if not TKINTER_SUPPORT_AVAILABLE:
		show_error_message_box(
			"Adding an origin from the tray needs tkinter, which is missing from this "
			"build. Add it to allowed_origins in settings.json instead, then restart."
		)
		return
	raw_origin = prompt_for_origin()
	if raw_origin is None:  # dialog cancelled
		return
	try:
		origin = normalize_origin(raw_origin)
	except ValueError as error:
		show_error_message_box(str(error))
		return
	if not service.add_allowed_origin(origin):
		show_error_message_box(f'"{origin}" is already on the allowed list.')


def make_allowed_origin_menu_item(service: USBPrintService, origin: str):
	"""Build one menu item that removes the given origin when clicked, after
	confirming — removing one breaks Browser printing for anyone using it, so it isn't
	a single accidental click away."""

	def confirm_and_remove() -> None:
		if confirm_message_box(f"Stop allowing this origin to print?\n\n{origin}"):
			service.remove_allowed_origin(origin)

	def remove_origin(icon, item):
		run_tray_dialog("remove-origin", confirm_and_remove)

	return pystray.MenuItem(f"Remove: {origin}", remove_origin)


def build_allowed_origins_menu_items(service: USBPrintService):
	"""Yield Add Origin… plus one removable item per currently allowed origin, or a
	disabled placeholder when none are configured. The tray menu is rebuilt by
	USBPrintService.refresh_tray_menu whenever the allow-list changes."""
	yield pystray.MenuItem("Add Origin…", lambda icon, item: add_allowed_origin_interactively(service))
	yield pystray.Menu.SEPARATOR
	if service.allowed_origins:
		for origin in service.allowed_origins:
			yield make_allowed_origin_menu_item(service, origin)
	else:
		yield pystray.MenuItem("(none allowed — Browser printing is disabled)", None, enabled=False)


def run_tray_icon(service: USBPrintService, http_port: int) -> None:
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
		pystray.MenuItem("Allowed Origins", pystray.Menu(lambda: build_allowed_origins_menu_items(service))),
		pystray.Menu.SEPARATOR,
		pystray.MenuItem("Start with Windows", toggle_startup, checked=lambda item: is_startup_enabled()),
		pystray.MenuItem("Open Log File", open_log_file),
		pystray.Menu.SEPARATOR,
		pystray.MenuItem("Exit", exit_service),
	)
	tray_icon = pystray.Icon(
		"bullwheel_usb_print_service",
		load_tray_image(),
		f"{APPLICATION_NAME} (USB port {service.port}, Browser port {http_port})",
		menu,
	)
	service.tray_icon = tray_icon
	tray_icon.run()


def show_error_message_box(message: str) -> None:
	"""Show a blocking Windows error dialog, used for fatal startup errors when the
	service runs windowless (pythonw) and has no console to print to."""
	import ctypes

	MB_ICONERROR = 0x00000010
	MB_SETFOREGROUND = 0x00010000
	MB_TOPMOST = 0x00040000
	ctypes.windll.user32.MessageBoxW(
		None, message, APPLICATION_NAME, MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST
	)


def confirm_message_box(message: str) -> bool:
	"""Show a blocking Yes/No Windows dialog, used to confirm a change made from the
	tray before it takes effect — e.g. removing an allowed origin."""
	import ctypes

	MB_YESNO = 0x00000004
	MB_ICONQUESTION = 0x00000020
	MB_SETFOREGROUND = 0x00010000
	MB_TOPMOST = 0x00040000
	IDYES = 6
	result = ctypes.windll.user32.MessageBoxW(
		None, message, APPLICATION_NAME, MB_YESNO | MB_ICONQUESTION | MB_SETFOREGROUND | MB_TOPMOST
	)
	return result == IDYES


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
		"--http-port",
		type=int,
		default=DEFAULT_HTTP_PORT,
		help=f"Port for the Browser method's HTTP listener on 127.0.0.1 (default: {DEFAULT_HTTP_PORT}, "
		"must match BROWSER_PRINT_SERVICE_URL in Bullwheel's printing.js).",
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

	printer_mapping = load_printer_mapping()
	allowed_origins = load_allowed_origins()

	service = USBPrintService(arguments.host, arguments.port, printer_name, printer_mapping, allowed_origins)
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

	try:
		http_server = start_browser_print_server(service, arguments.http_port)
	except OSError as error:
		message = (
			f"Could not listen on {HTTP_HOST}:{arguments.http_port}: {error}\n"
			"Another program may already be using this port."
		)
		logger.error(message)
		if not arguments.headless:
			show_error_message_box(message)
		sys.exit(1)
	http_server_thread = threading.Thread(target=http_server.serve_forever, name="browser-print-server", daemon=True)
	http_server_thread.start()

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
		run_tray_icon(service, arguments.http_port)
		logger.info("Shutting down.")


if __name__ == "__main__":
	main()
