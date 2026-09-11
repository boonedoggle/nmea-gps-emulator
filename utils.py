import glob
import shutil
import subprocess


def add_system_services():
    services_files = list()
    services_files += glob.glob('services/*.service')
    for service_file in services_files:
        shutil.copy(service_file, '/etc/systemd/system/')
    subprocess.Popen(['systemctl', 'daemon-reload'], shell=False)
