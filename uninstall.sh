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

# Stop System Services functions
systemctl stop nmea_gps_emulator.service

# Delete installed files
rm -r $INSTALL_DIR
rm "$SERVICES_DIR/nmea_gps_emulator.service"

# Reload system services
systemctl daemon-reload
