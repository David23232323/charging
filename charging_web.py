#!/usr/bin/env python3
"""Local, dependency-free macOS charging dashboard."""

import argparse
import json
import plistlib
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer


ROOT = Path(__file__).resolve().parent


def number(data, key, scale=1):
    value = data.get(key)
    if type(value) not in (int, float):
        return None
    return value if scale == 1 else value / scale


def minutes(data, key):
    value = number(data, key)
    return value if value is not None and 0 < value < 65535 else None


def battery_snapshot(data):
    percent = number(data, "CurrentCapacity")
    maximum = number(data, "MaxCapacity")
    if percent is not None and maximum and maximum != 100:
        percent = percent / maximum * 100
    voltage = number(data, "AppleRawBatteryVoltage", 1000)
    if voltage is None:
        voltage = number(data, "Voltage", 1000)
    current = number(data, "InstantAmperage")
    if current is None:
        current = number(data, "Amperage")
    # ioreg can encode negative battery current as an unsigned 64-bit integer.
    if current is not None:
        if current >= 2**63:
            current -= 2**64
        current /= 1000
    battery_watts = voltage * current if voltage is not None and current is not None else None
    external = data.get("ExternalConnected")
    charging = data.get("IsCharging")
    if charging is True:
        state = "Charging"
    elif data.get("FullyCharged") is True:
        state = "Fully charged" if external else "On battery"
    elif external is True:
        state = "Plugged in, not charging"
    elif external is False:
        state = "On battery"
    else:
        state = "Status unavailable"

    adapter = data.get("AdapterDetails") or {}
    power = data.get("PowerTelemetryData") or {}
    design = number(data, "DesignCapacity")
    capacity = number(data, "AppleRawMaxCapacity")
    adapter_watts = number(power, "SystemPowerIn", 1000) if external is True else None
    mac_watts = None
    if battery_watts is not None:
        if external is False and battery_watts <= 0:
            mac_watts = -battery_watts
        elif adapter_watts is not None and adapter_watts >= 0 and adapter_watts >= battery_watts:
            mac_watts = adapter_watts - battery_watts
    cells = (data.get("BatteryData") or {}).get("CellVoltage")
    cells = [value / 1000 for value in cells] if (
        isinstance(cells, (list, tuple)) and cells
        and all(type(value) in (int, float) and 0 < value < 10000 for value in cells)
    ) else []
    return {
        "timestamp": time.time(),
        "state": state,
        "percent": percent,
        "external": external,
        "battery_watts": battery_watts,
        "adapter_watts": adapter_watts,
        "mac_watts": mac_watts,
        "cell_voltages": cells,
        "cell_spread_mv": (max(cells) - min(cells)) * 1000 if len(cells) > 1 else None,
        "rated_watts": number(adapter, "Watts") if external is True else None,
        "minutes_to_full": minutes(data, "AvgTimeToFull") if charging is True else None,
        "minutes_to_empty": minutes(data, "AvgTimeToEmpty") if external is False else None,
        "voltage": voltage,
        "current": current,
        "temperature": number(data, "Temperature", 100),
        "cycles": number(data, "CycleCount"),
        "health": capacity / design * 100 if capacity is not None and design else None,
        "adapter_name": adapter.get("Name") if external is True else None,
        "adapter_voltage": number(adapter, "AdapterVoltage", 1000) if external is True else None,
        "adapter_current": number(adapter, "Current", 1000) if external is True else None,
    }


class BatteryReader:
    def __init__(self):
        self.lock = threading.Lock()
        self.snapshot = None
        self.updated = 0

    def read(self):
        with self.lock:
            if self.snapshot is None or time.monotonic() - self.updated >= 2:
                result = subprocess.run(
                    ["ioreg", "-a", "-r", "-c", "AppleSmartBattery"],
                    capture_output=True, check=True, timeout=5,
                )
                records = plistlib.loads(result.stdout)
                if not records or not isinstance(records[0], dict):
                    raise ValueError("No AppleSmartBattery data found.")
                self.snapshot = battery_snapshot(records[0])
                self.updated = time.monotonic()
            return self.snapshot


class DashboardServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer's reverse DNS lookup can stall even on loopback.
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


class DashboardHandler(BaseHTTPRequestHandler):
    reader = BatteryReader()

    def do_GET(self):
        expected_host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected_host:
            self.respond(403, b"Local access only.", "text/plain")
            return
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.respond(403, b"Cross-site access denied.", "text/plain")
            return
        if self.path == "/api/battery":
            try:
                payload = self.reader.read()
            except (OSError, subprocess.SubprocessError, ValueError,
                    plistlib.InvalidFileException) as error:
                print(f"Battery read failed: {error}", file=sys.stderr)
                self.respond(503, json.dumps({"error": "Could not read battery data. See terminal for details."}).encode(),
                             "application/json")
                return
            self.respond(200, json.dumps(payload).encode(), "application/json")
        elif self.path in ("/", "/app.js", "/style.css"):
            filename, content_type = {
                "/": ("index.html", "text/html"),
                "/app.js": ("app.js", "text/javascript"),
                "/style.css": ("style.css", "text/css"),
            }[self.path]
            self.respond(200, (ROOT / "web" / filename).read_bytes(), content_type)
        elif self.path == "/favicon.ico":
            self.respond(204, b"", "image/x-icon")
        else:
            self.respond(404, b"Not found.", "text/plain")

    def respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; object-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # A tab may close while ioreg is running.

    def log_message(self, format, *args):
        if args and str(args[1]) not in ("200", "204"):
            super().log_message(format, *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="Local port (default: automatically choose a free port)")
    parser.add_argument("--no-open", action="store_true", help="Print the URL without opening a browser")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if sys.platform != "darwin":
        parser.error("This dashboard requires macOS.")
    try:
        server = DashboardServer(("127.0.0.1", args.port), DashboardHandler)
    except OSError as error:
        parser.exit(1, f"Could not start dashboard: {error}\n")
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Charging dashboard: {url}\nPress Ctrl+C to stop. Closing the tab pauses sampling.", flush=True)
    with server:
        if not args.no_open and not webbrowser.open(url):
            print("Browser did not open automatically. Open the URL above.", file=sys.stderr)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nCharging dashboard stopped.")


if __name__ == "__main__":
    main()
