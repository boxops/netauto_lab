"""
Purpose:
Onboard devices to Nautobot that are not supported by the default Nautobot onboarding app.
User input:
- Location
- IP Addresses
- Port
- Timeout
- Credentials
- Platform
- Role
- Device Type
"""

import re
import csv
import json
import time
from io import StringIO
from django.conf import settings
from netmiko import ConnectHandler

from nautobot.dcim.models import (
    Device,
    DeviceType,
    Manufacturer,
    Location,
    Platform,
    Interface,
)
from nautobot.ipam.models import IPAddress, Prefix, Namespace
from nautobot.extras.models import Role
from nautobot.extras.models.secrets import SecretsGroup
from nautobot.extras.models.secrets import (
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)
from nautobot.extras.models import Status

from nautobot.apps.jobs import (
    register_jobs,
    Job,
    ObjectVar,
    StringVar,
    IntegerVar,
    FileVar,
)
from nautobot.dcim.choices import InterfaceTypeChoices

from custom_jobs.modules.tools import parse_command_output
from custom_jobs.modules.tools import convert_flat_config_to_dict
from custom_jobs.backends.airosapi import AirOS8API
from custom_jobs.backends.tachyon import Tachyon

name = "Custom Onboarding"

SUPPORTED_PLATFORMS = [
    "keymile_nos",
    "fiberstore_fsos",
    "mikrotik_routeros",
    "netonix_os",
    "cisco_ios",
    "cisco_xr",
    "cisco_xe",
    "cisco_nxos",
    "cisco_s300",
    "ubiquiti_airos",
    "ubiquiti_edge",
    "ubiquiti_edgeswitch",
    "ceragon_os",
    "siklu_os",
    "cambium_cnmatrix",
    "fortinet",
    "tachyon_os",
]


def get_default_location():
    try:
        return Location.objects.get(name="Unknown")
    except Location.DoesNotExist:
        return None


class CustomDeviceOnboarding(Job):
    csv_file = FileVar(
        label="CSV File",
        required=False,
        description="If a file is provided all the options below will be ignored.",
    )
    location = ObjectVar(
        model=Location,
        description="Assigned location for the onboarded device.",
        default=get_default_location(),
        required=False,
    )
    ip_addresses = StringVar(
        description="IP Address of the device to onboard, specify in a comma separated list for multiple devices.",
        required=False,
    )
    port = IntegerVar(default=22, required=False)
    credential = ObjectVar(
        model=SecretsGroup,
        description="SecretsGroup for device connection credentials.",
        required=False,
    )
    platform = ObjectVar(model=Platform, required=False)
    role = ObjectVar(model=Role, required=False)
    device_type = ObjectVar(model=DeviceType, description="Optional", required=False)

    class Meta:
        name = "Onboard New Devices to Nautobot"
        description = f"Supported platforms: {SUPPORTED_PLATFORMS}"
        has_sensitive_variables = False
        soft_time_limit = 1800  # 30 minutes
        time_limit = 2400  # 40 minutes
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
            "bulk",
        ]

    def run(
        self,
        location,
        ip_addresses,
        port,
        credential,
        platform,
        role,
        device_type,
        csv_file=None,
    ):
        failed_devices = []

        if csv_file:
            processed_csv_data = self._process_csv_data(csv_file)
            for device_data in processed_csv_data:
                self.logger.info(f"Processing device data from CSV: {device_data}")
                try:
                    self._create_onboard_task(
                        location=device_data["location"],
                        ip_address=device_data["ip_address"],
                        port=device_data["port"],
                        credential=device_data["credential"],
                        platform=device_data["platform"],
                        role=device_data["role"],
                        device_type=device_data["device_type"],
                    )
                except Exception as e:
                    failed_devices.append(
                        {"ip_address": device_data["ip_address"], "error": e}
                    )
        else:
            if "," in ip_addresses:
                for ip_address in ip_addresses.split(","):
                    self.logger.info(f"Processing IP address: {ip_address}")
                    try:
                        self._create_onboard_task(
                            location,
                            ip_address,
                            port,
                            credential,
                            platform,
                            role,
                            device_type,
                        )
                    except Exception as e:
                        failed_devices.append({"ip_address": ip_address, "error": e})
            else:
                self.logger.info(f"Processing IP address: {ip_addresses}")
                try:
                    self._create_onboard_task(
                        location,
                        ip_addresses,
                        port,
                        credential,
                        platform,
                        role,
                        device_type,
                    )
                except Exception as e:
                    failed_devices.append({"ip_address": ip_addresses, "error": e})

        if failed_devices:
            output = StringIO()
            writer = csv.DictWriter(output, fieldnames=["ip_address", "error"])
            writer.writeheader()
            writer.writerows(failed_devices)
            self.create_file("failed_devices.csv", output.getvalue().encode("utf-8"))

    def _process_csv_data(self, csv_file):
        """Convert CSV data into a dictionary containing Nautobot objects."""
        self.logger.info("Decoding CSV file...")
        decoded_csv_file = csv_file.read().decode("utf-8")
        csv_reader = csv.DictReader(StringIO(decoded_csv_file))
        self.logger.info("Processing CSV data...")
        processed_csv_data = []
        for row in csv_reader:
            if any(row.values()):
                processed_csv_data.append(
                    {
                        "location": row.get("location"),
                        "ip_address": row.get("ip_address"),
                        "port": int(row.get("port", 22)),
                        "credential": row.get("credential"),
                        "platform": row.get("platform"),
                        "role": row.get("role"),
                        "device_type": row.get("device_type"),
                    }
                )
        return processed_csv_data

    def _create_onboard_task(
        self, location, ip_address, port, credential, platform, role, device_type
    ):
        try:
            if not platform:
                raise Exception("Platform is None")
            task = OnboardDevice(
                job=self,
                location=location,
                ip_address=ip_address,
                port=port,
                credential=credential,
                platform=platform,
                role=role,
                device_type=device_type,
            )
            task.onboard()
        except Exception as e:
            self.logger.error(f"Failed to onboard device with IP {ip_address}: {e}")
            raise e


class OnboardDevice:
    def __init__(
        self,
        job,
        location,
        ip_address,
        port,
        credential,
        platform,
        role,
        device_type,
    ):
        self.job = job
        self.location = Location.objects.get(name=location)
        self.ip_address = ip_address
        self.port = port
        self.credential = SecretsGroup.objects.get(name=credential)
        self.platform = Platform.objects.get(name=platform)
        self.role = Role.objects.get(name=role)
        self.device_type = device_type
        self.status_active = Status.objects.get(name="Active")

        self.session = None
        self.device_info = None
        self.device = None
        self.device_info = None
        self.hostname = None
        self.serial_number = None
        self.management_interface_name = None
        self.management_interface = None
        self.device_model = None
        self.manufacturer = None

        if self.platform.network_driver not in SUPPORTED_PLATFORMS:
            raise Exception(f"Platform {self.platform.network_driver} is not supported")

    def find_match(self, pattern, output):
        match = re.search(pattern, output)
        if match:
            found = (match.group(1)).strip()
            self.job.logger.info(f"Found match: {found}")
            return found
        else:
            raise Exception(
                f"Could not parse, pattern: {pattern} not found in the input string: {output}"
            )

    def _keymile_nos(self):
        self.connect()
        self.manufacturer = "Keymile"
        hostname = self.session.send_command("show run | inc hostname")
        hostname_pattern = r"hostname\s+(\S+)"
        self.hostname = self.find_match(hostname_pattern, hostname)

        interface = self.session.send_command("show ip interface brief")
        parsed_interface = parse_command_output(
            interface, "keymile_nos_show_ip_interface_brief.textfsm"
        )
        found = False
        for item in parsed_interface:
            if (
                item["PRIMARY_IP"] == self.ip_address
                or item["SECONDARY_IP"] == self.ip_address
            ):
                self.management_interface_name = item["INTERFACE"]
                self.job.logger.info(
                    f"Management interface name: {self.management_interface_name}"
                )
                found = True
                break
        if not found:
            raise Exception(
                f"Could not find management interface name in output: {parsed_interface}"
            )

        model = self.session.send_command("show system")
        parsed_model = parse_command_output(model, "keymile_nos_show_system.textfsm")
        self.device_model = parsed_model[0]["MODEL"]

    def _fiberstore_fsos(self):
        self.connect()
        self.manufacturer = "FS.Com"
        hostname = self.session.send_command("sh run | inc hostname")
        hostname_pattern = r"hostname\s+(\S+)"
        self.hostname = self.find_match(hostname_pattern, hostname)

        interface = self.session.send_command("show interface")
        parsed_interface = parse_command_output(
            interface, "fiberstore_fsos_show_interface.textfsm"
        )
        self._get_management_interface(parsed_interface)

        # Concatenate Hardware Type with Model (eg. S5850-24S2Q)
        version = self.session.send_command("sh version")
        parsed_version = parse_command_output(
            version, "fiberstore_fsos_show_version.textfsm"
        )
        device_model = parsed_version[0]["MODEL"]
        hardware_type = parsed_version[0]["HARDWARE_TYPE"]
        self.device_model = f"{hardware_type}-{device_model}"

    def _mikrotik_routeros(self):
        self.connect()
        self.manufacturer = "MikroTik"
        hostname = self.session.send_command_timing("system identity print")
        parsed_hostname = parse_command_output(
            hostname, "mikrotik_routeros_system_identity_print.textfsm"
        )
        self.hostname = parsed_hostname[0]["HOSTNAME"]

        interface = self.session.send_command_timing("ip address print")
        parsed_interface = parse_command_output(
            interface, "mikrotik_routeros_ip_address_print.textfsm"
        )
        found = False
        for item in parsed_interface:
            if item["NETWORK"] == self.ip_address:
                self.management_interface_name = item["INTERFACE"]
                self.job.logger.info(
                    f"Management interface name: {self.management_interface_name}"
                )
                found = True
                break
        if not found:
            raise Exception(
                f"Could not find management interface name in output: {parsed_interface}"
            )

        model = self.session.send_command_timing("system routerboard print")
        parsed_model = parse_command_output(
            model, "mikrotik_routeros_system_routerboard_print.textfsm"
        )
        self.device_model = parsed_model[0]["MODEL"]

    def _netonix_os(self):
        self.connect()
        self.manufacturer = "Netonix"
        self.session.send_command_timing("terminal length 0")
        hostname = self.session.send_command_timing("show config")
        config_json = json.loads("".join(hostname))
        self.hostname = config_json["Switch_Name"]

        # No management interface name on Netonix switches, create a dummy interface name
        self.management_interface_name = "MgmtEth0"

        model = self.session.send_command("show status")
        parsed_model = parse_command_output(model, "netonix_os_show_status.textfsm")
        self.device_model = parsed_model[0]["MODEL"]

    def _cisco_ios(self):
        self.connect()
        self.manufacturer = "Cisco"
        hostname = self.session.send_command_timing("show version")
        parsed_hostname = parse_command_output(
            hostname, "cisco_ios_show_version.textfsm"
        )
        self.hostname = parsed_hostname[0]["HOSTNAME"]

        interface = self.session.send_command_timing("show ip interface brief")
        parsed_interface = parse_command_output(
            interface, "cisco_ios_show_ip_interface_brief.textfsm"
        )
        self._get_management_interface(parsed_interface)

        model = self.session.send_command_timing("show inventory")
        parsed_model = parse_command_output(model, "cisco_ios_show_inventory.textfsm")
        self.device_model = parsed_model[0]["PID"]

    def _cisco_xr(self):
        self.connect()
        self.manufacturer = "Cisco"
        hostname = self.session.send_command("sh run | inc hostname")
        hostname_pattern = r"hostname\s+(\S+)"
        self.hostname = self.find_match(hostname_pattern, hostname)

        interface = self.session.send_command_timing("show ip interface brief")
        parsed_interface = parse_command_output(
            interface, "cisco_xr_show_ip_interface_brief.textfsm"
        )
        self._get_management_interface(parsed_interface)

        model = self.session.send_command_timing("show inventory")
        parsed_model = parse_command_output(model, "cisco_xr_show_inventory.textfsm")
        self.device_model = parsed_model[0]["PID"]

    def _cisco_xe(self):
        self.connect()
        self.manufacturer = "Cisco"
        hostname = self.session.send_command_timing("show version")
        parsed_hostname = parse_command_output(
            hostname, "cisco_xe_show_version.textfsm"
        )
        self.hostname = parsed_hostname[0]["HOSTNAME"]

        interface = self.session.send_command_timing("show ip interface brief")
        parsed_interface = parse_command_output(
            interface, "cisco_xe_show_ip_interface_brief.textfsm"
        )
        self._get_management_interface(parsed_interface)

        model = self.session.send_command_timing("show inventory")
        parsed_model = parse_command_output(model, "cisco_xe_show_inventory.textfsm")
        self.device_model = parsed_model[0]["PID"]

    def _cisco_nxos(self):
        self.connect()
        self.manufacturer = "Cisco"
        hostname = self.session.send_command_timing("show version")
        parsed_hostname = parse_command_output(
            hostname, "cisco_nxos_show_version.textfsm"
        )
        self.hostname = parsed_hostname[0]["HOSTNAME"]

        interface = self.session.send_command_timing("show ip interface brief")
        parsed_interface = parse_command_output(
            interface, "cisco_nxos_show_ip_interface_brief.textfsm"
        )
        self._get_management_interface(parsed_interface)

        model = self.session.send_command_timing("show inventory")
        parsed_model = parse_command_output(model, "cisco_nxos_show_inventory.textfsm")
        self.device_model = parsed_model[0]["PID"]

    def _cisco_s300(self):
        self.connect()
        self.manufacturer = "Cisco"
        hostname = self.session.send_command_timing("show system")
        parsed_hostname = parse_command_output(
            hostname, "cisco_s300_show_system.textfsm"
        )
        self.hostname = parsed_hostname[0]["HOSTNAME"]

        interface = self.session.send_command_timing("show ip interface")
        parsed_interface = parse_command_output(
            interface, "cisco_s300_show_ip_interface.textfsm"
        )
        self._get_management_interface(parsed_interface)

        model = self.session.send_command("show inventory")
        parsed_model = parse_command_output(model, "cisco_s300_show_inventory.textfsm")
        self.device_model = parsed_model[0]["PID"]

    def _ubiquiti_airos(self):
        self.connect()
        self.manufacturer = "Ubiquiti"
        system_config = self.session.send_command_timing("cat /tmp/system.cfg")
        # convert_flat_config_to_dict
        system_config = convert_flat_config_to_dict(system_config)
        # "resolv.host.1.name": "AB-CLX-SH214-1-AP3"
        self.hostname = system_config["resolv.host.1.name"]

        for key, value in system_config.items():
            if "netconf" in key and "ip" in key and value == self.ip_address:
                self.management_interface_name = system_config[
                    key.replace("ip", "devname")
                ]
                break

        # Find the model
        board_config = self.session.send_command_timing("cat /etc/board.info")
        board_config = convert_flat_config_to_dict(board_config)
        # "board.name": "airFiber 24G",
        self.device_model = board_config["board.name"]

    def _ubiquiti_edge(self):
        self.connect()
        self.manufacturer = "Ubiquiti"
        show_host_name = self.session.send_command_timing(
            "show configuration | match host"
        )
        parsed_hostname = parse_command_output(
            show_host_name, "ubiquiti_edge_show_host_name.textfsm"
        )
        self.hostname = parsed_hostname[0]["HOSTNAME"]

        show_interfaces = self.session.send_command_timing("show interfaces")
        parsed_interfaces = parse_command_output(
            show_interfaces, "ubiquiti_edge_show_interfaces.textfsm"
        )
        for item in parsed_interfaces:
            if self.ip_address in item["IP_ADDRESS"]:
                self.management_interface_name = item["INTERFACE"]
                self.job.logger.info(
                    f"Management interface name: {self.management_interface_name}"
                )
                break

        show_version = self.session.send_command_timing("show version")
        parsed_version = parse_command_output(
            show_version, "ubiquiti_edge_show_version.textfsm"
        )
        self.device_model = parsed_version[0]["MODEL"]

    def _ubiquiti_edgeswitch(self):
        self.connect()
        self.manufacturer = "Ubiquiti"
        show_sysinfo = self.session.send_command_timing("show sysinfo")
        parsed_version = parse_command_output(
            show_sysinfo, "ubiquiti_edgeswitch_show_sysinfo.textfsm"
        )
        self.hostname = parsed_version[0]["HOSTNAME"]

        self.management_interface_name = (
            "mgmt0"  # No management interface name on Ubiquiti EdgeSwitches
        )

        show_version = self.session.send_command_timing("show version")
        parsed_version = parse_command_output(
            show_version, "ubiquiti_edgeswitch_show_version.textfsm"
        )
        self.device_model = parsed_version[0]["MODEL"]

    def _ceragon_os(self):
        self.connect()
        self.manufacturer = "Ceragon"
        hostname = self.session.send_command_timing("platform management unit-status")
        # Wait for the command to complete
        time.sleep(2)
        parsed_status = parse_command_output(hostname, "ceragon_os_show_status.textfsm")
        self.session.send_command_timing("quit")
        self.hostname = parsed_status[0]["SYSTEM_NAME"]

        # No management interface name on Ceragon switches, create a dummy interface name
        self.management_interface_name = "Ethernet1/1"

        self.device_model = parsed_status[0]["UNIT_TYPE"]

    def _siklu_os(self):
        self.connect()
        self.manufacturer = "Siklu"
        hostname = self.session.send_command_timing("show system name")
        parsed_hostname = parse_command_output(hostname, "siklu_os_show_system.textfsm")
        self.hostname = parsed_hostname[0]["NAME"]

        # No management interface name on Siklu devices, create a dummy interface name
        self.management_interface_name = "eth1"

        model = self.session.send_command_timing("show system state product")
        parsed_model = parse_command_output(model, "siklu_os_show_state.textfsm")
        self.device_model = parsed_model[0]["DEVICE_TYPE"]
        self.session.send_command_timing("quit")

    def _cambium_cnmatrix(self):
        self.connect()
        self.manufacturer = "Cambium"
        system = self.session.send_command_timing("show system information")
        parsed_system = parse_command_output(
            system, "cambium_cnmatrix_show_system.textfsm"
        )
        self.session.send_command_timing("q")
        self.hostname = parsed_system[0]["SYS_NAME"]

        # No management interface name on Cambium devices, create a dummy interface name
        self.management_interface_name = "vlan 20"

        self.device_model = parsed_system[0]["MODEL_NAME"]

    def _fortinet(self):
        self.connect()
        self.manufacturer = "Fortinet"
        status = self.session.send_command_timing("get system status")
        parsed_status = parse_command_output(
            status, "fortinet_get_system_status.textfsm"
        )
        self.hostname = parsed_status[0]["HOSTNAME"]
        self.job.logger.info(f"Hostname: {self.hostname}")

        # Default management interface name on Fortinet devices
        self.management_interface_name = "mgmt"
        self.job.logger.info(
            f"Management interface name: {self.management_interface_name}"
        )

        # "VERSION": "FortiGate-200F v6.4.15,build2095,240129 (GA.M)",
        self.device_model = parsed_status[0]["VERSION"].split(" ")[0]
        self.job.logger.info(f"Device model: {self.device_model}")

    def _tachyon_os(self):
        self.manufacturer = "Tachyon"
        self.get_device_info()
        device = Tachyon(
            job=self.job,
            ip=self.device_info["host"],
            username=self.device_info["username"],
            password=self.device_info["password"],
            verbose=True,
            use_https=True,
            verify_ssl=False,
        )
        try:
            device.login()
            output = device.get_stats()
            device.logout()

            # {'system': {'bootbanks': {'active': {'bootbank': 2,
            #                                     'version': '1.12.0 rev 54573'},
            #                         'backup': {'bootbank': 1,
            #                                     'version': '1.11.3 rev 54283'}},
            #             'clock_datetime': '2025/07/28 09:37:56',
            #             'cpu_temp': 21,
            #             'cpu_usage_percent': 26,
            #             'description': '',
            #             'epoch_time': 1753695476,
            #             'fw_version': '1.12.0 rev 54573',
            #             'hostname': 'AB.NW113.GNODE.G.NW113.GPOP',
            #             'location': '',
            #             'memory': {'free': 196804608,
            #                     'total': 419278848,
            #                     'usage': 54,
            #                     'used': 222474240},
            #             'model': 'TNA-303X',
            #             'name': 'AB.NW113.GNODEG-BACKHAUL',
            #             'serial_no': 'TNA3031272400013'},
            # 'wireless': {'histogram_updated_at': 1753695322,
            #             'peers': [{'antenna_kit': 'none',
            #                         'connection_time': 95000,
            #                         'est_link_distance': 126,
            #                         'fw': '1.12.0.r54573',
            #                         'iface_name': 'prs0',
            #                         'ipv4': '172.19.43.200',
            #                         'ipv6': '2001::1',
            #                         'mac': '78:5E:E8:D0:62:E7',
            #                         'mcs_histogram': {'mcs_1': 0,
            #                                         'mcs_10': 4,
            #                                         'mcs_11': 122,
            #                                         'mcs_12': 81198,
            #                                         'mcs_2': 0,
            #                                         'mcs_3': 0,
            #                                         'mcs_4': 0,
            #                                         'mcs_5': 0,
            #                                         'mcs_6': 0,
            #                                         'mcs_7': 0,
            #                         'model': 'TNA-301',
            #                         'remote_rssi': -57,
            #                         'retry_histogram': {'0': 81324,
            #                                             '1-3': 2022,
            #                                             '10+': 0,
            #                                             '4-6': 0,
            #                                             '7-9': 0},
            #                         'rssi': -48,
            #                         'rx_bytes': 1712464183021,
            #                         'rx_mcs': 12,
            #                         'rx_packets': 3191411166,
            #                         'rx_rate': 4620,
            #                         'rx_sector': 4,
            #                         'snr': 16,
            #                         'system_description': 'Tachyon Networks TNA-301: '
            #                                             'v1.12.0 r54573',
            #                         'system_name': 'AB.NW113.GPOP.AP1',
            #                         'tx_bytes': 21011168992324,
            #                         'tx_mcs': 12,
            #                         'tx_packets': 2092153226,
            #                         'tx_rate': 4620,
            #                         'tx_sector': 4}],
            #             'radios': {'wlan0': {'antenna_kit': 'ak-100',
            #                                 'boresight_lock': False,
            #                                 'channel': 5,
            #                                 'channel_label': '5',
            #                                 'channel_width': 2160,
            #                                 'freq': 66960,
            #                                 'mac': '78:5E:E8:D0:5B:70',
            #                                 'modem_temp': 28,
            #                                 'name': 'wlan0',
            #                                 'op_mode': 'station',
            #                                 'ptp': False,
            #                                 'radio_temp': 23,
            #                                 'security': 'WPA2-PSK (GCMP)',
            #                                 'ssid': 'GPOP.AP1',
            #                                 'tx_power': 54}}}}

            self.hostname = output["system"]["hostname"]
            self.job.logger.info(f"Hostname: {self.hostname}")
            self.device_model = output["system"]["model"]
            self.job.logger.info(f"Device model: {self.device_model}")
            self.management_interface_name = "mgmt0"
            self.job.logger.info(
                f"Management interface name: {self.management_interface_name}"
            )

        except Exception as e:
            self.job.logger.error(
                f"{self.device} Error connecting to Tachyon device: {e}"
            )

    def _get_management_interface(self, interfaces):
        found = False
        for item in interfaces:
            if self.ip_address in item["IP_ADDRESS"]:
                self.management_interface_name = item["INTERFACE"]
                self.job.logger.info(
                    f"Management interface name: {self.management_interface_name}"
                )
                found = True
                break
        if not found:
            raise Exception(
                f"Could not find management interface name in output: {interfaces}"
            )

    def get_or_create_device_type(self):
        manufacturer = Manufacturer.objects.get(name=self.manufacturer)
        device_type, created = DeviceType.objects.get_or_create(
            manufacturer=manufacturer,
            model=self.device_model,
        )
        self.device_type = device_type
        self.job.logger.info(f"Device type: {device_type}. Created: {created}")

    def get_or_create_device(self):
        device_model = DeviceType.objects.get(model=self.device_model)
        device, created = Device.objects.get_or_create(
            name=self.hostname,
            device_type=device_model if device_model else self.device_type,
            location=self.location,
            status=self.status_active,
            role=self.role,
        )
        self.device = device
        self.job.logger.info(f"Device: {device}. Created: {created}")
        # self.job.logger.info(
        #     "Device: %s",
        #     device.name,
        #     extra={"object": device},
        # )
        # self.job.logger.info(f"Created device: {created}")

        self.device.platform = self.platform
        self.device.secrets_group = self.credential
        self.device.validated_save()
        self.job.logger.info(
            f"Assigned platform: {self.platform} to device: {self.device}"
        )
        self.job.logger.info(
            f"Assigned secrets_group: {self.credential} to device: {self.device}"
        )

    def get_or_create_device_interface(self):
        interface, created = Interface.objects.get_or_create(
            name=self.management_interface_name,
            device=self.device,
            status=self.status_active,
            type=InterfaceTypeChoices.TYPE_VIRTUAL,
        )
        self.management_interface = interface
        self.job.logger.info(f"Interface: {interface}. Created: {created}")

    def get_or_create_prefix(self):
        calculate_prefix = lambda ip: ".".join(ip.split(".")[:3]) + ".0/24"
        prefix, created = Prefix.objects.get_or_create(
            prefix=calculate_prefix(self.ip_address),
            namespace=Namespace.objects.get(name="Global"),
            status=self.status_active,
        )
        self.job.logger.info(f"Prefix: {prefix}. Created: {created}")

    def get_or_create_ip_address(self):
        ip_address, created = IPAddress.objects.get_or_create(
            host=self.ip_address,
            type="host",
            mask_length=24,
            status=self.status_active,
        )
        self.job.logger.info(f"IP address: {ip_address}. Created: {created}")

        ip_address.interfaces.set([self.management_interface])
        ip_address.validated_save()
        self.job.logger.info(
            f"Set IP address: {ip_address} on interface: {self.management_interface}"
        )

        self.device.primary_ip4 = ip_address
        self.device.validated_save()
        self.job.logger.info(f"Set primary_ip4: {ip_address} on device: {self.device}")

    def get_device_info(self):
        """Get device information for use in Netmiko connection."""
        self.device_info = {
            "device_type": self.platform.network_driver,
            "host": self.ip_address,
            "username": self.credential.get_secret_value(
                access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
                secret_type=SecretsGroupSecretTypeChoices.TYPE_USERNAME,
            ),
            "password": self.credential.get_secret_value(
                access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
                secret_type=SecretsGroupSecretTypeChoices.TYPE_PASSWORD,
            ),
            "secret": self.credential.get_secret_value(
                access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
                secret_type=SecretsGroupSecretTypeChoices.TYPE_SECRET,
            ),
            "global_delay_factor": 2,
        }
        if self.platform.network_driver in [
            "fiberstore_fsos",
            "netonix_os",
            "ubiquiti_airos",
            "ubiquiti_edge",
            "ubiquiti_edgeswitch",
            "ceragon_os",
            "siklu_os",
            "cambium_cnmatrix",
            "tachyon_os",
        ]:
            self.device_info["device_type"] = "generic"

    def connect(self):
        """Open an SSH connection to the device."""
        self.get_device_info()
        self.session = ConnectHandler(**self.device_info)
        self.session.enable()
        self.job.logger.info(f"Device info: {self.device_info}")

    def disconnect(self):
        """Close the SSH connection to the device."""
        if self.session:
            self.session.disconnect()
            self.job.logger.info("Disconnected from device")

    def onboard(self):
        try:
            if self.platform.network_driver == "keymile_nos":
                self._keymile_nos()
            elif self.platform.network_driver == "fiberstore_fsos":
                self._fiberstore_fsos()
            elif self.platform.network_driver == "mikrotik_routeros":
                self._mikrotik_routeros()
            elif self.platform.network_driver == "netonix_os":
                self._netonix_os()
            elif self.platform.network_driver == "cisco_ios":
                self._cisco_ios()
            elif self.platform.network_driver == "cisco_xr":
                self._cisco_xr()
            elif self.platform.network_driver == "cisco_xe":
                self._cisco_xe()
            elif self.platform.network_driver == "cisco_nxos":
                self._cisco_nxos()
            elif self.platform.network_driver == "cisco_s300":
                self._cisco_s300()
            elif self.platform.network_driver == "ubiquiti_airos":
                self._ubiquiti_airos()
            elif self.platform.network_driver == "ubiquiti_edge":
                self._ubiquiti_edge()
            elif self.platform.network_driver == "ubiquiti_edgeswitch":
                self._ubiquiti_edgeswitch()
            elif self.platform.network_driver == "ceragon_os":
                self._ceragon_os()
            elif self.platform.network_driver == "siklu_os":
                self._siklu_os()
            elif self.platform.network_driver == "cambium_cnmatrix":
                self._cambium_cnmatrix()
            elif self.platform.network_driver == "fortinet":
                self._fortinet()
            elif self.platform.network_driver == "tachyon_os":
                self._tachyon_os()
            else:
                raise Exception(
                    f"Platform {self.platform.network_driver} is not supported"
                )
        except Exception as e:
            self.job.logger.error(
                f"Failed to onboard device with IP {self.ip_address}: {e}"
            )
        finally:
            self.disconnect()

        self.get_or_create_device_type()
        self.get_or_create_device()
        self.get_or_create_device_interface()
        self.get_or_create_prefix()
        self.get_or_create_ip_address()

        self.job.logger.info(
            "Device onboarded successfully: %s",
            self.device.name,
            extra={"object": self.device},
        )
        # self.job.logger.info(
        #     f"Device onboarded successfully: {self.device}",
        #     extra={"object": self.device},
        # )


register_jobs(CustomDeviceOnboarding)
