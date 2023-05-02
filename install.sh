#!/usr/bin/env bash
# (c) Copyright by Deepwave Digital, Inc.
set -e # Forces exit on errors

CURRENT_DIR=$(pwd)
INSTALL_DIR="/opt/nmea-gps-emulator"
SERVICES_DIR="/etc/systemd/system"

# Make sure you are sudo
if [ "$(id -u)" -ne "0" ] ; then
    echo "This script must be executed with root privileges. Exiting Now."
    exit 1
fi

# Copy files
mkdir $INSTALL_DIR
cp uninstall.sh "$INSTALL_DIR/uninstall.sh"
cp main.py "$INSTALL_DIR/main.py"
cp nmea_gps.py "$INSTALL_DIR/nmea_gps.py"
cp settings.json "$INSTALL_DIR/settings.json"
cp requirements.txt "$INSTALL_DIR/requirements.txt"
cp nmea_gps_emulator.service "$INSTALL_DIR/nmea_gps_emulator.service"
cp nmea_gps_emulator.service "$SERVICES_DIR/nmea_gps_emulator.service"

# Make executable
chmod +x "$INSTALL_DIR/main.py"

# Create venv
cd "$INSTALL_DIR"
virtualenv venv --python=python3.6
source "$INSTALL_DIR/venv/bin/activate"
pip install -r requirements.txt
deactivate
cd "$CURRENT_DIR"

# Reload system services
systemctl daemon-reload
