#!/usr/bin/env python3
"""Select the AIR-T GPS or emulator inside the stock gpsd instance."""

import json
import logging
import signal
import socket
import time

import serial


GPSD_CONTROL_SOCKET = "/run/gpsd.sock"
GPSD_HOST = "127.0.0.1"
GPSD_PORT = 2947
GPS_DEVICE = "/dev/gps"
EMULATOR_HOST = "127.0.0.1"
EMULATOR_PORT = 10110
EMULATOR_DEVICE = f"tcp://localhost:{EMULATOR_PORT}"
CHECK_INTERVAL = 2.0
NO_FIX_GRACE = 10.0
SERIAL_PROBE_SECONDS = 3.0
STATUS_FILE = "/run/nmea_gpsd_fallback.json"


class GpsdFallback:
    def __init__(self):
        self.mode = "hardware"
        self.stop_requested = False

    def stop(self, *_args):
        self.stop_requested = True

    @staticmethod
    def gpsd_command(command):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(GPSD_CONTROL_SOCKET)
                sock.sendall((command + "\n").encode("ascii"))
                try:
                    response = sock.recv(256).decode("ascii", errors="ignore")
                except socket.timeout:
                    # gpsd 3.22 may apply a control command without returning
                    # a response before the short control-socket timeout.
                    return True
                if response.startswith("OK"):
                    return True
                logging.warning("gpsd rejected control command %r: %s", command, response.strip())
                return False
        except OSError as error:
            logging.warning("Unable to send gpsd command: %s", error)
            return False

    @classmethod
    def add_device(cls, path):
        return cls.gpsd_command("+" + path)

    @classmethod
    def remove_device(cls, path):
        return cls.gpsd_command("-" + path)

    @staticmethod
    def hardware_status_from_gpsd(timeout=1.5):
        status = {"mode": None, "visible_satellites": None, "used_satellites": None,
                  "latitude": None, "longitude": None}
        try:
            with socket.create_connection((GPSD_HOST, GPSD_PORT), timeout=1) as sock:
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
                            msg = json.loads(raw.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if msg.get("device") != GPS_DEVICE:
                            continue
                        if msg.get("class") == "TPV":
                            status["mode"] = msg.get("mode", status["mode"])
                            status["latitude"] = msg.get("lat", status["latitude"])
                            status["longitude"] = msg.get("lon", status["longitude"])
                        elif msg.get("class") == "SKY":
                            status["visible_satellites"] = msg.get("nSat", status["visible_satellites"])
                            status["used_satellites"] = msg.get("uSat", status["used_satellites"])
        except (OSError, ValueError):
            pass
        return status

    @staticmethod
    def serial_status(timeout=SERIAL_PROBE_SECONDS):
        status = {"mode": 1, "visible_satellites": None, "used_satellites": None,
                  "latitude": None, "longitude": None}
        try:
            receiver = serial.Serial(GPS_DEVICE, 38400, timeout=0.5)
        except (OSError, serial.SerialException):
            return status
        buffer = bytearray()
        end = time.monotonic() + timeout
        try:
            while time.monotonic() < end:
                try:
                    chunk = receiver.read(512)
                except serial.SerialException:
                    return False
                if not chunk:
                    continue
                buffer.extend(chunk)
                while buffer:
                    if buffer.startswith(b"\xb5\x62"):
                        if len(buffer) < 8:
                            break
                        length = buffer[4] | (buffer[5] << 8)
                        frame_length = 8 + length
                        if len(buffer) < frame_length:
                            break
                        frame = bytes(buffer[:frame_length])
                        del buffer[:frame_length]
                        if frame[2:4] == b"\x01\x07" and length >= 24:
                            payload = frame[6:6 + length]
                            fix_type = payload[20]
                            flags = payload[21]
                            status["mode"] = (
                                3 if fix_type >= 3 else 2 if fix_type == 2 else 1
                            ) if flags & 0x01 else 1
                            status["visible_satellites"] = payload[23]
                            if length >= 32:
                                status["longitude"] = int.from_bytes(payload[24:28], "little", signed=True) / 1e7
                                status["latitude"] = int.from_bytes(payload[28:32], "little", signed=True) / 1e7
                            if status["mode"] >= 2:
                                return status
                        continue
                    if buffer.startswith(b"$"):
                        newline = buffer.find(b"\n")
                        if newline < 0:
                            break
                        line = bytes(buffer[:newline]).decode("ascii", errors="ignore").strip()
                        del buffer[:newline + 1]
                        fields = line.split("*")[0].split(",")
                        if fields[0].endswith("GGA") and len(fields) > 6:
                            try:
                                status["mode"] = 3 if int(fields[6] or 0) > 0 else 1
                                if len(fields) > 7 and fields[7]:
                                    status["visible_satellites"] = int(fields[7])
                                if status["mode"] >= 2:
                                    return status
                            except ValueError:
                                pass
                        elif fields[0].endswith("RMC") and len(fields) > 2 and fields[2] == "A":
                            status["mode"] = 3
                            return status
                        continue
                    next_markers = [marker for marker in (buffer.find(b"\xb5\x62"), buffer.find(b"$")) if marker >= 0]
                    if not next_markers:
                        buffer.clear()
                    else:
                        del buffer[:min(next_markers)]
        finally:
            receiver.close()
        return status

    @staticmethod
    def write_status(status, source):
        status = dict(status)
        status["source"] = source
        try:
            with open(STATUS_FILE, "w") as stream:
                json.dump(status, stream)
        except OSError:
            pass

    @staticmethod
    def emulator_available():
        try:
            with socket.create_connection((EMULATOR_HOST, EMULATOR_PORT), timeout=0.5):
                return True
        except OSError:
            return False

    def switch_to_emulator(self):
        if not self.emulator_available():
            return False
        self.remove_device(GPS_DEVICE)
        self.add_device(EMULATOR_DEVICE)
        self.mode = "emulator"
        logging.info("gpsd source selected: %s", EMULATOR_DEVICE)
        return True

    def switch_to_hardware(self):
        self.remove_device(EMULATOR_DEVICE)
        self.add_device(GPS_DEVICE)
        self.mode = "hardware"
        logging.info("gpsd source selected: %s", GPS_DEVICE)

    def run(self):
        no_fix_since = None
        logging.info("gpsd fallback supervisor started; preserving stock gpsd service")
        try:
            while not self.stop_requested:
                if self.mode == "hardware":
                    status = self.hardware_status_from_gpsd()
                    self.write_status(status, GPS_DEVICE)
                    if (status.get("mode") or 0) >= 2:
                        no_fix_since = None
                    else:
                        no_fix_since = no_fix_since or time.monotonic()
                        if time.monotonic() - no_fix_since >= NO_FIX_GRACE:
                            if self.switch_to_emulator():
                                no_fix_since = None
                else:
                    status = self.serial_status()
                    self.write_status(status, EMULATOR_DEVICE)
                    if not self.emulator_available():
                        logging.info("Emulator unavailable; returning to hardware GPS")
                        self.switch_to_hardware()
                    elif (status.get("mode") or 0) >= 2:
                        logging.info("Hardware GPS fix detected; leaving fallback mode")
                        self.switch_to_hardware()
                time.sleep(CHECK_INTERVAL)
        finally:
            if self.mode == "emulator":
                self.switch_to_hardware()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s: %(message)s")
    supervisor = GpsdFallback()
    signal.signal(signal.SIGTERM, supervisor.stop)
    signal.signal(signal.SIGINT, supervisor.stop)
    supervisor.run()


if __name__ == "__main__":
    main()
