#!/usr/bin/env python3
"""Interactive configuration utility for the NMEA GPS emulator."""

import argparse
import json
import math
import os
import subprocess
import tempfile
from pathlib import Path


DEFAULT_SETTINGS_FILE = Path(__file__).resolve().parent.parent / "settings" / "settings.json"
SERVICE_NAME = "nmea_gps_emulator.service"


def parse_number(label, current, minimum=None, maximum=None, maximum_inclusive=True):
    while True:
        response = input(f"{label} [{current}]: ").strip()
        if not response:
            return current
        try:
            value = float(response)
        except ValueError:
            print("Please enter a number, or press Enter to keep the current value.")
            continue
        if not math.isfinite(value):
            print("Please enter a finite number.")
            continue
        if minimum is not None and value < minimum:
            print(f"Value must be at least {minimum}.")
            continue
        if maximum is not None and (value > maximum or (not maximum_inclusive and value == maximum)):
            operator = "less than" if not maximum_inclusive else "at most"
            print(f"Value must be {operator} {maximum}.")
            continue
        return value


def write_settings(path, settings):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        temporary_path = Path(stream.name)
        json.dump(settings, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fchmod(stream.fileno(), mode)
    os.replace(temporary_path, path)


def configure(settings_file):
    with open(settings_file) as stream:
        settings = json.load(stream)

    print(f"Current emulator settings ({settings_file}):")
    print(f"  Latitude:  {settings['latitude']}")
    print(f"  Longitude: {settings['longitude']}")
    print(f"  Altitude:  {settings['gps_altitude_amsl']} m MSL")
    print(f"  Heading:   {settings['gps_heading']} degrees")
    print(f"  Speed:     {settings['gps_speed']} knots")
    print("Press Enter at any prompt to keep the current value.\n")

    settings["latitude"] = parse_number("Latitude", settings["latitude"], -90, 90)
    settings["longitude"] = parse_number("Longitude", settings["longitude"], -180, 180)
    settings["gps_altitude_amsl"] = parse_number(
        "Altitude (m MSL)", settings["gps_altitude_amsl"]
    )
    settings["gps_heading"] = parse_number(
        "Heading (degrees)", settings["gps_heading"], 0, 360, maximum_inclusive=False
    )
    settings["gps_speed"] = parse_number("Speed (knots)", settings["gps_speed"], 0)

    write_settings(settings_file, settings)
    print(f"Updated {settings_file}.")
    subprocess.run(["systemctl", "restart", SERVICE_NAME], check=True)
    print(f"Restarted {SERVICE_NAME}.")


def main():
    parser = argparse.ArgumentParser(description="Configure the NMEA GPS emulator")
    parser.add_argument(
        "-s", "--settings-file", type=Path, default=DEFAULT_SETTINGS_FILE,
        help=f"settings JSON file (default: {DEFAULT_SETTINGS_FILE})",
    )
    args = parser.parse_args()
    try:
        configure(args.settings_file)
    except KeyboardInterrupt:
        print("\nConfiguration cancelled.")
        return 130
    except (OSError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f"Configuration failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
