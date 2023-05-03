import json
import subprocess

GPSD_CONFIG_FILE = '/etc/default/gpsd'
DEFAULT_SETTINGS_FILE = '/opt/nmea-gps-emulator/settings.json'


def update_gpsd_devices(settings_file=DEFAULT_SETTINGS_FILE,
                        gpsd_config_file=GPSD_CONFIG_FILE):
    """
    Adds a server to the GPSD Config file
    """
    # Get the GPSD server from the settings file
    with open(settings_file) as f:
        settings = json.load(f)
    ip_address = 'localhost'
    port = settings['port']
    new_server = f'tcp://{ip_address}:{port}'
    # Find line with DEVICES and create new line
    with open(gpsd_config_file) as f:
        for line in f:
            if line.startswith('DEVICES'):
                old_devices = line
                key, server_string = line.split('=')
                server_string = server_string.replace('"', '')
                server_list = server_string.split()
                if new_server not in server_list:
                    server_list += [new_server]
                new_devices = f'DEVICES="{" ".join(server_list)}"\n'
    # Update file with new entry
    with open(gpsd_config_file, 'r') as fr:
        file_content = fr.read()
    file_content = file_content.replace(old_devices, new_devices)
    with open(gpsd_config_file, 'w') as fw:
        fw.write(file_content)
    subprocess.Popen(['systemctl', 'restart', 'gpsd'], shell=False)


if __name__ == '__main__':
    update_gpsd_devices()
