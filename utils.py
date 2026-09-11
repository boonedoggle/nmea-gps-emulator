import glob
import os
import shutil
import subprocess


def remove_legacy_gpsd_override():
    """Remove only the gpsd override files created by earlier releases."""
    paths = [
        '/etc/systemd/system/gpsd.service',
        '/etc/systemd/system/gpsd.service.d/nmea-gps-fallback.conf',
    ]
    for path in paths:
        if not os.path.isfile(path):
            continue
        with open(path) as stream:
            contents = stream.read()
        if 'nmea_gps_emulator.gpsd_fallback' in contents or 'nmea-gps-fallback' in contents:
            os.remove(path)


def add_system_services():
    remove_legacy_gpsd_override()
    services_files = list()
    services_files += glob.glob('services/*.service')
    for service_file in services_files:
        shutil.copy(service_file, '/etc/systemd/system/')
    subprocess.Popen(['systemctl', 'daemon-reload'], shell=False)
