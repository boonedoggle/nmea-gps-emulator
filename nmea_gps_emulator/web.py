#!/usr/bin/env python3
"""Small web interface for the NMEA GPS emulator settings."""

import argparse
import html
import json
import logging
import math
import socket
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .configure import DEFAULT_SETTINGS_FILE, write_settings


FIELDS = (
    ("latitude", "Latitude", -90, 90, True, "degrees"),
    ("longitude", "Longitude", -180, 180, True, "degrees"),
    ("gps_altitude_amsl", "Altitude", None, None, True, "meters MSL"),
    ("gps_heading", "Heading", 0, 360, False, "degrees"),
    ("gps_speed", "Speed", 0, None, True, "knots"),
)
GPSD_HOST = "127.0.0.1"
GPSD_PORT = 2947
REAL_STATUS_FILE = "/run/nmea_gpsd_fallback.json"


def real_gps_status():
    """Read the physical receiver status published by the fallback supervisor."""
    try:
        with open(REAL_STATUS_FILE) as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError):
        return {"mode": 1, "visible_satellites": None, "used_satellites": None, "source": None}


def current_fix(timeout=0.75):
    """Read the active gpsd source and position without changing gpsd state."""
    source = "Unavailable"
    mode = None
    visible_satellites = None
    used_satellites = None
    latitude = None
    longitude = None

    try:
        with socket.create_connection((GPSD_HOST, GPSD_PORT), timeout=timeout) as sock:
            sock.settimeout(0.15)
            sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
            end = time.monotonic() + timeout
            data = b""
            while time.monotonic() < end:
                try:
                    data += sock.recv(8192)
                except socket.timeout:
                    continue
                while b"\n" in data:
                    raw, data = data.split(b"\n", 1)
                    try:
                        message = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if message.get("class") in ("TPV", "SKY"):
                        device = message.get("device")
                        source = str(device) if device else "Unavailable"
                    if message.get("class") == "TPV":
                        mode = message.get("mode", mode)
                        latitude = message.get("lat", latitude)
                        longitude = message.get("lon", longitude)
                    elif message.get("class") == "SKY":
                        visible_satellites = message.get("nSat", visible_satellites)
                        used_satellites = message.get("uSat", used_satellites)
    except OSError:
        pass
    return {
        "source": source,
        "mode": mode,
        "latitude": latitude,
        "longitude": longitude,
        "visible_satellites": visible_satellites,
        "used_satellites": used_satellites,
    }


def validate_values(form, current):
    values = dict(current)
    errors = []
    for key, label, minimum, maximum, max_inclusive, units in FIELDS:
        raw = form.get(key, [""])[0].strip()
        if not raw:
            errors.append(f"{label} is required.")
            continue
        try:
            value = float(raw)
        except ValueError:
            errors.append(f"{label} must be a number.")
            continue
        if not math.isfinite(value):
            errors.append(f"{label} must be finite.")
        elif minimum is not None and value < minimum:
            errors.append(f"{label} must be at least {minimum} {units}.")
        elif maximum is not None and (value > maximum or (not max_inclusive and value == maximum)):
            comparator = "less than" if not max_inclusive else "at most"
            errors.append(f"{label} must be {comparator} {maximum} {units}.")
        else:
            values[key] = value
    return values, errors


def page(settings, errors=(), message="", source="Unavailable", latitude=None, longitude=None,
         mode=None, visible_satellites=None, used_satellites=None):
    fields = []
    for key, label, _minimum, _maximum, _inclusive, units in FIELDS:
        value = html.escape(str(settings.get(key, "")))
        fields.append(
            f'<label>{label} ({units})<input name="{key}" value="{value}" required></label>'
        )
    error_html = "".join(f"<li>{html.escape(error)}</li>" for error in errors)
    status = f'<p class="message">{html.escape(message)}</p>' if message else ""
    location_link = ""
    if latitude is not None and longitude is not None:
        location_link = (
            f'<p><a href="https://www.google.com/maps/search/?api=1&query='
            f'{latitude},{longitude}" target="_blank" rel="noopener noreferrer">'
            "Show Location in Google Maps</a></p>"
        )
    fix_status = {1: "No fix", 2: "2D fix", 3: "3D fix"}.get(mode, "Unavailable")
    satellite_status = "Unavailable"
    if visible_satellites is not None:
        satellite_status = f"{visible_satellites} visible"
        if used_satellites is not None:
            satellite_status += f" ({used_satellites} used in solution)"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>NMEA GPS Emulator</title>
<style>
body {{ font: 16px sans-serif; max-width: 38rem; margin: 2rem auto; padding: 0 1rem; }}
form {{ display: grid; gap: 1rem; }}
label {{ display: grid; gap: .3rem; }}
input {{ box-sizing: border-box; font: inherit; padding: .45rem; width: 100%; }}
button {{ font: inherit; padding: .55rem .9rem; }}
.errors {{ color: #a00; }} .message {{ color: #064; }}
</style></head><body>
<h1>NMEA GPS Emulator</h1>
<p><strong>GPSd Source:</strong> {html.escape(source)}</p>
<p><strong>GPS Status:</strong> {html.escape(fix_status)}</p>
<p><strong>GPS Satellites:</strong> {html.escape(satellite_status)}</p>
{location_link}
{status}
{('<ul class="errors">' + error_html + '</ul>') if errors else ''}
<form method="post">
{''.join(fields)}
<button type="submit">Save and restart emulator</button>
</form>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    settings_file = DEFAULT_SETTINGS_FILE

    def send_page(self, status, settings, errors=(), message=""):
        fix = current_fix()
        real_status = real_gps_status()
        source = real_status.get("source") or fix["source"]
        body = page(
            settings, errors, message, source, fix["latitude"], fix["longitude"],
            real_status.get("mode"), real_status.get("visible_satellites"),
            real_status.get("used_satellites"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def load(self):
        with open(self.settings_file) as stream:
            return json.load(stream)

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        try:
            self.send_page(HTTPStatus.OK, self.load())
        except (OSError, json.JSONDecodeError) as error:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def do_POST(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 8192:
                self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            form = parse_qs(self.rfile.read(size).decode("utf-8"))
            current = self.load()
            settings, errors = validate_values(form, current)
            if errors:
                self.send_page(HTTPStatus.BAD_REQUEST, settings, errors)
                return
            write_settings(self.settings_file, settings)
            subprocess.run(["systemctl", "restart", "nmea_gps_emulator.service"], check=True)
            self.send_page(HTTPStatus.OK, settings, message="Settings saved and emulator restarted.")
        except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
            logging.exception("Web configuration update failed")
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def log_message(self, format_string, *args):
        logging.info("%s - %s", self.address_string(), format_string % args)


def start_server(settings_file=DEFAULT_SETTINGS_FILE, host="0.0.0.0", port=8013):
    Handler.settings_file = Path(settings_file)
    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="nmea-web",
        daemon=True,
    )
    thread.start()
    logging.info("NMEA GPS web interface listening on http://%s:%d", host, port)
    return server


def main():
    parser = argparse.ArgumentParser(description="Web interface for the NMEA GPS emulator")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: all IPv4 interfaces)")
    parser.add_argument("--port", type=int, default=8013, help="listen port (default: 8013)")
    parser.add_argument("-s", "--settings-file", type=Path, default=DEFAULT_SETTINGS_FILE)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s: %(message)s")
    server = start_server(args.settings_file, args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
