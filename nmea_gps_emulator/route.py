"""KMZ/KML route parsing and smooth route playback."""

import math
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from pyproj import Geod


GEOD = Geod(ellps="WGS84")
ROUTE_STATUS_FILE = "/run/nmea_route_status.json"


def parse_route(path):
    """Return ``(latitude, longitude, altitude_msl)`` points from a KMZ/KML."""
    path = Path(path)
    if path.suffix.lower() == ".kmz":
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".kml")]
            if not names:
                raise ValueError("KMZ file does not contain a KML file")
            preferred = next((name for name in names if Path(name).name.lower() == "doc.kml"), names[0])
            root = ET.fromstring(archive.read(preferred))
    elif path.suffix.lower() == ".kml":
        root = ET.parse(path).getroot()
    else:
        raise ValueError("route must be a .kmz or .kml file")

    points = []
    for element in root.findall(".//{*}coordinates"):
        for coordinate in (element.text or "").split():
            values = coordinate.split(",")
            if len(values) < 2:
                continue
            try:
                longitude = float(values[0])
                latitude = float(values[1])
                altitude = float(values[2]) if len(values) >= 3 and values[2] else 0.0
            except ValueError:
                continue
            if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                points.append((latitude, longitude, altitude))

    if not points:
        raise ValueError("route contains no valid coordinates")
    return points


class RoutePlayer:
    """Update an NMEA object along a route at a constant speed."""

    def __init__(self, nmea_object, points, speed_knots, loop=True, update_rate_hz=10.0):
        if len(points) < 2:
            raise ValueError("route must contain at least two coordinates")
        if speed_knots <= 0:
            raise ValueError("route speed must be greater than zero")
        self.nmea_object = nmea_object
        self.points = points
        self.speed_knots = speed_knots
        self.loop = loop
        self.update_interval = 1.0 / update_rate_hz
        self.segment_distances = []
        self.segment_bearings = []
        self.cumulative_distances = [0.0]
        for start, end in zip(points, points[1:]):
            bearing, _back_bearing, distance = GEOD.inv(start[1], start[0], end[1], end[0])
            self.segment_bearings.append(bearing % 360)
            self.segment_distances.append(distance)
            self.cumulative_distances.append(self.cumulative_distances[-1] + distance)
        self.total_distance = self.cumulative_distances[-1]
        if self.total_distance <= 0:
            raise ValueError("route coordinates do not span any distance")
        self.running = True

    def _position_at_distance(self, distance, reverse=False):
        distance = max(0.0, min(self.total_distance, distance))
        for index, (start_distance, end_distance) in enumerate(
            zip(self.cumulative_distances, self.cumulative_distances[1:])
        ):
            if distance <= end_distance or index == len(self.segment_distances) - 1:
                segment_distance = self.segment_distances[index]
                if segment_distance <= 0:
                    bearing = self.segment_bearings[index]
                    if reverse:
                        bearing = (bearing + 180) % 360
                    return self.points[index] + (bearing,)
                fraction = (distance - start_distance) / segment_distance
                start = self.points[index]
                end = self.points[index + 1]
                if reverse:
                    longitude, latitude, _ = GEOD.fwd(
                        end[1], end[0], (self.segment_bearings[index] + 180) % 360,
                        segment_distance * (1 - fraction),
                    )
                else:
                    longitude, latitude, _ = GEOD.fwd(
                        start[1], start[0], self.segment_bearings[index], segment_distance * fraction
                    )
                altitude = start[2] + (end[2] - start[2]) * fraction
                bearing = self.segment_bearings[index]
                if reverse:
                    bearing = (bearing + 180) % 360
                return latitude, longitude, altitude, bearing
        return self.points[-1] + (self.segment_bearings[-1],)

    def run(self):
        speed_mps = self.speed_knots * 0.514444
        start_time = time.monotonic()
        while self.running:
            elapsed_distance = (time.monotonic() - start_time) * speed_mps
            if self.loop:
                cycle_distance = self.total_distance * 2
                cycle_position = elapsed_distance % cycle_distance
                reverse = cycle_position > self.total_distance
                distance = cycle_distance - cycle_position if reverse else cycle_position
            else:
                reverse = False
                distance = min(elapsed_distance, self.total_distance)
                if elapsed_distance >= self.total_distance:
                    self.running = False

            latitude, longitude, altitude, heading = self._position_at_distance(distance, reverse)
            self.nmea_object.set_route_position(latitude, longitude, altitude, heading, self.speed_knots)
            time.sleep(self.update_interval)

    def stop(self):
        self.running = False
