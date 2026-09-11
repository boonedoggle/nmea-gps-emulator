#!/usr/bin/env python3
"""Small web interface for the NMEA GPS emulator settings."""

import argparse
import cgi
import html
import io
import json
import logging
import math
import os
import socket
import subprocess
import tempfile
import threading
import time
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .configure import DEFAULT_SETTINGS_FILE, write_settings
from .route import ROUTE_STATUS_FILE, parse_route


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
ROUTES_DIR = DEFAULT_SETTINGS_FILE.parent / "routes"


def real_gps_status():
    """Read the physical receiver status published by the fallback supervisor."""
    try:
        with open(REAL_STATUS_FILE) as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError):
        return {"mode": 1, "visible_satellites": None, "used_satellites": None, "source": None}


def active_route_status():
    try:
        with open(ROUTE_STATUS_FILE) as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError):
        return {"active": False}


def current_fix(timeout=1.5):
    """Read the active gpsd source and position without changing gpsd state."""
    source = "Unavailable"
    mode = None
    visible_satellites = None
    used_satellites = None
    latitude = None
    longitude = None

    try:
        with socket.create_connection((GPSD_HOST, GPSD_PORT), timeout=timeout) as sock:
            sock.settimeout(0.25)
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
         mode=None, visible_satellites=None, used_satellites=None, route_points=(), route_active=False):
    fields = []
    for key, label, _minimum, _maximum, _inclusive, units in FIELDS:
        value = html.escape(str(settings.get(key, "")))
        fields.append(
            f'<label>{label} ({units})<input name="{key}" value="{value}" required></label>'
        )
    error_html = "".join(f"<li>{html.escape(error)}</li>" for error in errors)
    status = f'<p class="message">{html.escape(message)}</p>' if message else ""
    map_panel = '<p id="map-message">Map unavailable until a position is available.</p>'
    map_script = ""
    auto_update = '<label class="auto-update"><input type="checkbox" id="auto-update"> Auto Update (10s)</label>'
    has_map_data = latitude is not None and longitude is not None or route_points
    route_file = settings.get("route_file")
    route_name = Path(route_file).name if route_file else "No route selected"
    if route_file and not route_active:
        route_name += " (inactive)"
    route_loop = bool(settings.get("route_loop", True))
    route_controls = f"""
<fieldset class="route-controls">
<legend>Route playback</legend>
<p>Current route: {html.escape(route_name)}</p>
<label>Upload KMZ/KML route<input type="file" name="route_file_upload" accept=".kmz,.kml"></label>
<label class="check-option"><input type="checkbox" name="route_loop" value="true"{' checked' if route_loop else ''}> Loop route back and forth</label>
<label class="check-option"><input type="checkbox" name="clear_route" value="true"> Clear selected route</label>
</fieldset>"""
    if has_map_data:
        map_panel = '<div id="map" class="map" aria-label="GPS location map"></div>'
        # Coordinates originate from gpsd and are numeric, but JSON encoding keeps
        # the values safe when inserted into the page's script.
        initial_latitude = latitude if latitude is not None else route_points[0][0]
        initial_longitude = longitude if longitude is not None else route_points[0][1]
        map_latitude = json.dumps(initial_latitude)
        map_longitude = json.dumps(initial_longitude)
        hostname = json.dumps(socket.gethostname())
        route_coordinates = json.dumps([[point[0], point[1]] for point in route_points])
        map_script = f"""
<script>
const gpsLatitude = {map_latitude};
const gpsLongitude = {map_longitude};
const hostname = {hostname};
const routeCoordinates = {route_coordinates};
const map = L.map('map').setView([gpsLatitude, gpsLongitude], 15);
const street = L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
}});
const satellite = L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
        maxZoom: 19,
        attribution: 'Tiles &copy; Esri — Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community'
    }});
const topo = L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
        maxZoom: 19,
        attribution: 'Tiles &copy; Esri — Sources: Esri, DeLorme, HERE, Garmin, Intermap, increment P Corp., GEBCO, USGS, FAO, NPS, NRCAN, GeoBase, IGN, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), and the GIS User Community'
    }});
street.addTo(map);
L.control.layers({{Street: street, Satellite: satellite, Topographic: topo}}).addTo(map);
if (routeCoordinates.length > 1) {{
    const routeLine = L.polyline(routeCoordinates, {{color: '#d22', weight: 4}}).addTo(map);
    map.fitBounds(routeLine.getBounds(), {{padding: [20, 20]}});
}}
if ({json.dumps(latitude is not None and longitude is not None)}) {{
    L.marker([gpsLatitude, gpsLongitude]).addTo(map)
        .bindPopup(hostname).openPopup();
}}
</script>"""
    auto_update_script = """
<script>
const autoUpdate = document.getElementById('auto-update');
let autoUpdateTimer;
try {
    autoUpdate.checked = localStorage.getItem('nmea-auto-update') === 'true';
} catch (error) {
    // Continue without persistence if browser storage is unavailable.
}
function scheduleAutoUpdate() {
    clearTimeout(autoUpdateTimer);
    if (!autoUpdate.checked) {
        return;
    }
    try {
        localStorage.setItem('nmea-auto-update', 'true');
    } catch (error) {
        // Continue without persistence if browser storage is unavailable.
    }
    // Navigate to the page URL with GET. This is important after a form POST:
    // reloading the POST result would submit the settings again every 10s.
    autoUpdateTimer = setTimeout(() => window.location.replace('/'), 10000);
}
autoUpdate.addEventListener('change', () => {
    if (!autoUpdate.checked) {
        try {
            localStorage.setItem('nmea-auto-update', 'false');
        } catch (error) {
            // Continue without persistence if browser storage is unavailable.
        }
    }
    scheduleAutoUpdate();
});
scheduleAutoUpdate();
</script>"""
    fix_status = {1: "No fix", 2: "2D fix", 3: "3D fix"}.get(mode or 1, "No fix")
    satellite_status = "Unavailable"
    if visible_satellites is not None:
        satellite_status = f"{visible_satellites} visible"
        if used_satellites is not None:
            satellite_status += f" ({used_satellites} used in solution)"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>NMEA GPS Emulator</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
body {{ font: 16px sans-serif; max-width: 75rem; margin: 2rem auto; padding: 0 1rem; }}
.layout {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(20rem, 1fr); gap: 2rem; align-items: start; }}
.map-panel {{ min-height: 28rem; }}
.map {{ height: 28rem; width: 100%; }}
.auto-update {{ display: flex; align-items: center; gap: .4rem; margin-top: .75rem; }}
.auto-update input {{ width: auto; }}
.route-controls {{ display: grid; gap: .75rem; padding: .75rem; }}
.route-controls p {{ margin: 0; }}
.check-option {{ display: flex; align-items: center; gap: .4rem; }}
.check-option input {{ width: auto; }}
form {{ display: grid; gap: 1rem; }}
label {{ display: grid; gap: .3rem; }}
input {{ box-sizing: border-box; font: inherit; padding: .45rem; width: 100%; }}
button {{ font: inherit; padding: .55rem .9rem; }}
.errors {{ color: #a00; }} .message {{ color: #064; }}
@media (max-width: 800px) {{ .layout {{ grid-template-columns: 1fr; }} }}
</style></head><body>
<h1>NMEA GPS Emulator</h1>
<div class="layout"><section class="map-panel">{map_panel}{auto_update}</section><section>
<p><strong>GPSd Source:</strong> {html.escape(source)}</p>
<p><strong>GPS Status:</strong> {html.escape(fix_status)}</p>
<p><strong>GPS Satellites:</strong> {html.escape(satellite_status)}</p>
{status}
{('<ul class="errors">' + error_html + '</ul>') if errors else ''}
<form method="post" enctype="multipart/form-data">
<input type="hidden" name="route_loop" value="false">
{''.join(fields)}
{route_controls}
<button type="submit">Save and restart emulator</button>
</form>
</section></div>{map_script}{auto_update_script}</body></html>"""


class Handler(BaseHTTPRequestHandler):
    settings_file = DEFAULT_SETTINGS_FILE

    def send_page(self, status, settings, errors=(), message=""):
        fix = current_fix()
        real_status = real_gps_status()
        source = real_status.get("source") or fix["source"]
        latitude = fix["latitude"]
        longitude = fix["longitude"]
        # GPSd may not emit a TPV message during this page request even though
        # the supervisor has already selected a source and recorded its latest
        # position. Use that position to keep the map stable across refreshes.
        if latitude is None:
            latitude = real_status.get("latitude")
        if longitude is None:
            longitude = real_status.get("longitude")
        route_points = ()
        route_file = settings.get("route_file")
        route_status = active_route_status()
        if route_status.get("active") and route_status.get("route_file") == route_file:
            try:
                route_points = parse_route(route_file)
            except (OSError, ValueError, zipfile.BadZipFile) as error:
                logging.warning("Unable to plot route %s: %s", route_file, error)
        body = page(
            settings, errors, message, source, latitude, longitude,
            real_status.get("mode"), real_status.get("visible_satellites"),
            real_status.get("used_satellites"), route_points, bool(route_points),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def load(self):
        with open(self.settings_file) as stream:
            return json.load(stream)

    def parse_form(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 50 * 1024 * 1024:
            raise ValueError("uploaded request is too large")
        body = self.rfile.read(size)
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            return parse_qs(body.decode("utf-8")), None

        form = cgi.FieldStorage(
            fp=io.BytesIO(body),
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type},
        )
        values = {}
        upload = None
        for key in form.keys():
            item = form[key]
            items = item if isinstance(item, list) else [item]
            values[key] = [entry.value for entry in items if not entry.filename]
            for entry in items:
                if entry.filename:
                    upload = entry
        return values, upload

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        try:
            self.send_page(HTTPStatus.OK, self.load())
        except (OSError, json.JSONDecodeError) as error:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def do_POST(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        try:
            form, route_upload = self.parse_form()
            current = self.load()
            settings, errors = validate_values(form, current)
            if errors:
                self.send_page(HTTPStatus.BAD_REQUEST, settings, errors)
                return

            settings["route_loop"] = form.get("route_loop", ["false"])[-1].lower() == "true"
            if form.get("clear_route", ["false"])[-1].lower() == "true":
                settings["route_file"] = None
                settings["route_enabled"] = False
            if route_upload is not None:
                filename = Path(route_upload.filename).name
                if Path(filename).suffix.lower() not in (".kmz", ".kml"):
                    self.send_page(HTTPStatus.BAD_REQUEST, settings, ["Route must be a .kmz or .kml file."])
                    return
                if settings["gps_speed"] <= 0:
                    self.send_page(
                        HTTPStatus.BAD_REQUEST,
                        settings,
                        ["Speed must be greater than zero knots when using a route."],
                    )
                    return
                ROUTES_DIR.mkdir(parents=True, exist_ok=True)
                suffix = Path(filename).suffix.lower()
                with tempfile.NamedTemporaryFile("wb", dir=ROUTES_DIR, suffix=suffix, delete=False) as stream:
                    temporary_path = Path(stream.name)
                    stream.write(route_upload.file.read())
                try:
                    route_points = parse_route(temporary_path)
                except (OSError, ValueError, zipfile.BadZipFile) as error:
                    try:
                        temporary_path.unlink()
                    except FileNotFoundError:
                        pass
                    self.send_page(HTTPStatus.BAD_REQUEST, settings, [f"Invalid route: {error}"])
                    return
                route_path = ROUTES_DIR / f"uploaded_route{suffix}"
                os.replace(temporary_path, route_path)
                settings["route_file"] = str(route_path)
                settings["route_enabled"] = True
                settings["route_loop"] = form.get("route_loop", ["false"])[-1].lower() == "true"
                logging.info("Uploaded route %s with %d points", filename, len(route_points))
            write_settings(self.settings_file, settings)
            self.send_page(HTTPStatus.OK, settings, message="Settings saved; emulator restarting.")
            self.wfile.flush()
            # This process owns the web server, so let the response leave the
            # socket before asking systemd to restart the service.
            time.sleep(0.2)
            subprocess.Popen(
                ["systemctl", "restart", "nmea_gps_emulator.service"],
                start_new_session=True,
            )
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
