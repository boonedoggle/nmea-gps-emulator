#!/usr/bin/env python3

import sys
import logging
import uuid
import socket
import json
import argparse
import threading
import time
from pathlib import Path

from nmea_gps import NmeaMsg


_SCRIPT_DIR = Path(__file__).parent.absolute()
_PACKAGE_ROOT = _SCRIPT_DIR
_DEFAULT_SETTINGS_FILE = _PACKAGE_ROOT / 'settings.json'


class NmeaEmulator:
    def __init__(self, settings_file):
        self.nmea_thread = None
        self.nmea_obj = None
        with open(settings_file) as f:
            settings = json.load(f)
        self.position = {'latitude_value': settings['latitude_value'],
                         'latitude_direction': settings['latitude_direction'],
                         'longitude_value': settings['longitude_value'],
                         'longitude_direction': settings['longitude_direction'],}
        self.altitude = settings['gps_altitude_amsl']
        self.speed = settings['gps_speed']
        self.heading = settings['gps_heading']
        self.ip_address = settings['ip_address']
        self.port = settings['port']
        self.num_allowed_connections = settings['num_allowed_connections']

        # Initialize NmeaMsg object
        self.nmea_obj = NmeaMsg(self.position, self.altitude, self.speed, self.heading)

    def run(self):
        """
        Function starts thread with TCP (telnet) server sending NMEA data to connected
        client (clients).
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcpserver:
            # Bind socket to local host and port.
            try:
                tcpserver.bind((self.ip_address, self.port))
            except socket.error as err:
                print(f'\n*** Bind failed. Error: {err.strerror}. ***')
                print('Change IP/port settings or try again in next 2 minutes.')
                sys.exit()
            # Start listening on socket
            tcpserver.listen(10)
            print(f'Server listening on {self.ip_address}:{self.port}')
            while True:
                # Number of allowed connections to TCP server.
                max_threads = self.num_allowed_connections
                # Scripts waiting for client calls
                # The server is blocked and is waiting for a client
                conn, ip_add = tcpserver.accept()
                logging.info(f'Connected with {ip_add[0]}:{ip_add[1]}')
                thread_list = [thread.name for thread in threading.enumerate()]
                if len([thread_name for thread_name in thread_list if
                        thread_name.startswith('nmea_srv')]) < max_threads:
                    nmea_srv_thread = NmeaSrvThread(name=f'nmea_srv{uuid.uuid4().hex}',
                                                    daemon=True, conn=conn, ip_add=ip_add,
                                                    nmea_object=self.nmea_obj)
                    nmea_srv_thread.start()
                else:
                    # Close connection if number of scheduler jobs > max_sched_jobs
                    conn.close()
                    logging.info(f'Connection closed with {ip_add[0]}:{ip_add[1]}')


class NmeaSrvThread(threading.Thread):
    """
    A class that represents a thread dedicated for TCP (telnet) server-client connection.
    """
    def __init__(self, nmea_object, ip_add=None, conn=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.heading = None
        self.speed = None
        self._heading_cache = 0
        self._speed_cache = 0
        self.conn = conn
        self.ip_add = ip_add
        self.nmea_object = nmea_object
        self._lock = threading.RLock()

    def set_speed(self, speed):
        with self._lock:
            self.speed = speed

    def set_heading(self, heading):
        with self._lock:
            self.heading = heading

    def run(self):
        while True:
            timer_start = time.perf_counter()
            with self._lock:
                # Nmea object speed and heading update
                if self.heading and self.heading != self._heading_cache:
                    self.nmea_object.heading_targeted = self.heading
                    self._heading_cache = self.heading
                if self.speed and self.speed != self._speed_cache:
                    self.nmea_object.speed_targeted = self.speed
                    self._speed_cache = self.speed
                # The following commands allow the same copies of NMEA data is sent on all threads
                # Only first thread in a list can iterate over NMEA object (the same nmea output in all threads)
                thread_list = [thread.name for thread in threading.enumerate() if thread.name.startswith('nmea_srv')]
                current_thread_name = threading.current_thread().name
                if len(thread_list) > 1 and current_thread_name != thread_list[0]:
                    nmea_list = [f'{_}' for _ in self.nmea_object.nmea_sentences]
                else:
                    nmea_list = [f'{_}' for _ in next(self.nmea_object)]
                try:
                    for nmea in nmea_list:
                        self.conn.sendall(nmea.encode())
                        time.sleep(0.05)
                except (BrokenPipeError, OSError):
                    self.conn.close()
                    # print(f'\n*** Connection closed with {self.ip_add[0]}:{self.ip_add[1]} ***')
                    logging.info(f'Connection closed with {self.ip_add[0]}:{self.ip_add[1]}')
                    # Close thread
                    sys.exit()
            time.sleep(1 - (time.perf_counter() - timer_start))


def parse_command_line_args():
    help_formatter = argparse.ArgumentDefaultsHelpFormatter
    parser = argparse.ArgumentParser(description='NMEA Emulator',
                                     formatter_class=help_formatter)
    parser.add_argument('-s', required=False,
                        dest='settings_file', default=_DEFAULT_SETTINGS_FILE,
                        help='JSON Settings File')
    return parser.parse_args(sys.argv[1:])


if __name__ == '__main__':
    # Logging config
    log_format = '%(asctime)s: %(message)s'
    logging.basicConfig(format=log_format, level=logging.INFO, datefmt='%H:%M:%S')
    pars = parse_command_line_args()
    emulator = NmeaEmulator(pars.settings_file)
    try:
        emulator.run()
    except KeyboardInterrupt:
        sys.exit()
