#!/usr/bin/env python3
#
# Copyright 2021, Deepwave Digital, Inc.
# SPDX-License-Identifier: Commercial

# Setuptools / installation script for cbrs-watchdog


import distutils.command.build
import distutils.core
import glob
import os
from utils import update_gpsd_devices
import setuptools
import pkg_resources
import sys

assert os.getuid() == 0, 'This script must be executed with sudo privileges'

if sys.version_info < (3, 6):
    sys.exit('Sorry, Python < 3.6 is not supported')

pkg_resources.require(['pip >= 10.0.1'])

settings_files = list()
settings_files += glob.glob('settings/*.json')

with open('requirements.txt') as f:
    required_packages = f.read().splitlines()
print(required_packages)

try:
    setuptools.setup(
        name='nmea_gps_emulator',
        version='1.0.0',
        python_requires='>=3',
        install_requires=required_packages,
        packages=['nmea_gps_emulator'],
        license='Commercial',
        long_description=open('README.md').read(),
        data_files=[
            ('settings', settings_files)
        ],
        entry_points={
            'console_scripts': [
                'nmea_gps_emulator = nmea_gps_emulator.main:main',
            ]
        },
    )
finally:
    update_gpsd_devices()
