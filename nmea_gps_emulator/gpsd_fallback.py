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
                response = sock.recv(256).decode("ascii", errors="ignore")
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
    def hardware_fix_from_gpsd(timeout=1.5):
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
                        if (msg.get("class") == "TPV"
                                and msg.get("device") == GPS_DEVICE
                                and msg.get("mode", 0) >= 2):
                            return True
        except (OSError, ValueError):
            return False
        return False

    @staticmethod
    def serial_has_fix(timeout=SERIAL_PROBE_SECONDS):
        try:
            receiver = serial.Serial(GPS_DEVICE, 38400, timeout=0.5)
        except (OSError, serial.SerialException):
            return False
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
                            if fix_type >= 2 and flags & 0x01:
                                return True
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
                                if int(fields[6] or 0) > 0:
                                    return True
                            except ValueError:
                                pass
                        elif fields[0].endswith("RMC") and len(fields) > 2 and fields[2] == "A":
                            return True
                        continue
                    next_markers = [marker for marker in (buffer.find(b"\xb5\x62"), buffer.find(b"$")) if marker >= 0]
                    if not next_markers:
                        buffer.clear()
                    else:
                        del buffer[:min(next_markers)]
        finally:
            receiver.close()
        return False

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
        logging.info("gpsd source selected: NMEA emulator fallback")
        return True

    def switch_to_hardware(self):
        self.remove_device(EMULATOR_DEVICE)
        self.add_device(GPS_DEVICE)
        self.mode = "hardware"
        logging.info("gpsd source selected: AIR-T hardware GPS")

    def run(self):
        no_fix_since = None
        logging.info("gpsd fallback supervisor started; preserving stock gpsd service")
        try:
            while not self.stop_requested:
                if self.mode == "hardware":
                    if self.hardware_fix_from_gpsd():
                        no_fix_since = None
                    else:
                        no_fix_since = no_fix_since or time.monotonic()
                        if time.monotonic() - no_fix_since >= NO_FIX_GRACE:
                            if self.switch_to_emulator():
                                no_fix_since = None
                else:
                    if not self.emulator_available():
                        logging.info("Emulator unavailable; returning to hardware GPS")
                        self.switch_to_hardware()
                    elif self.serial_has_fix():
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
