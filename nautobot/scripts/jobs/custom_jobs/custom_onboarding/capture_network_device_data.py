"""Purpose: Capture network device data with Nautobot."""

from datetime import datetime
from netmiko import ConnectHandler
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from ncclient import manager
from netaddr import EUI
import json
import os
from netutils.ip import is_ip
import ipaddress

from nautobot.dcim.models import (
    Device,
    DeviceType,
    Manufacturer,
    Location,
    Platform,
    Interface,
)
from nautobot_golden_config.utilities.graphql import graph_ql_query
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

from nautobot.apps.jobs import register_jobs, Job, IntegerVar, BooleanVar
from nautobot_golden_config.models import GoldenConfig
from nautobot.extras.models.groups import DynamicGroup
from nautobot.core.utils.data import render_jinja2

from custom_jobs.modules.tools import get_device_connection_info
from custom_jobs.modules.tools import xml_to_dict
from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import parallel_execution
from custom_jobs.modules.tools import parse_command_output

name = "Custom Onboarding"

SUPPORTED_PLATFORMS = [
    # "keymile_nos",
    # "fiberstore_fsos",
    # "mikrotik_routeros",
    # "netonix_os",
    # "cisco_ios",
    "cisco_xr",
    # "cisco_xe",
    # "cisco_nxos",
    # "cisco_s300",
    # "ubiquiti_airos",
    # "siklu_os",
]


class CustomCaptureDeviceData(Job, DeviceFormEntry):
    """Job to capture device data with Nautobot."""

    parallel_task = BooleanVar(
        description="Execute backup tasks in parallel",
        default=False,
        required=False,
    )
    max_workers = IntegerVar(
        description="Number of workers to use for parallel execution",
        default=20,
        min_value=1,
        max_value=20,
        required=False,
    )

    class Meta:
        name = "Capture Network Device Data"
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
        tenant_group=None,
        tenant=None,
        location=None,
        rack_group=None,
        rack=None,
        role=None,
        manufacturer=None,
        platform=None,
        device_type=None,
        device=None,
        tags=None,
        status=None,
        parallel_task=True,
        max_workers=None,
    ):
        all_devices = set()

        all_devices = apply_device_filters(
            all_devices,
            tenant_group=tenant_group,
            tenant=tenant,
            location=location,
            rack_group=rack_group,
            rack=rack,
            role=role,
            manufacturer=manufacturer,
            platform=platform,
            device_type=device_type,
            tags=tags,
            status=status,
        )
        if device:
            all_devices.update(device)

        def capture_device_data(device):
            try:
                if device.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.warning(
                        f"Platform {device.platform.network_driver} is not supported."
                    )
                    return
                self.logger.info(f"Capturing data for device: {device.name}")
                capture = CaptureDeviceData(self, device)
                capture.execute()
            except Exception as e:
                self.logger.error(f"Error processing device {device.name}: {e}")

        if parallel_task:
            parallel_execution(all_devices, capture_device_data, max_workers)
        else:
            for device in all_devices:
                capture_device_data(device)


class CaptureDeviceData:
    def __init__(self, job, device):
        self.job = job
        self.device = device

        self.session = None
        self.captured_data = {}

    def open(self):
        """Open a connection to the device."""
        device_info = get_device_connection_info(self.device)
        self.session = ConnectHandler(**device_info)
        self.session.enable()
        self.session_prep()

    def close(self):
        """Close the connection to the device."""
        self.session.disconnect()

    def session_prep(self):
        """Prepare the session for sending commands."""
        if self.device.platform.network_driver in ["fiberstore_fsos", "netonix_os"]:
            self.session.send_command_timing("terminal length 0")

    def graphql_query(self):
        """Get device data from Nautobot using GraphQL."""
        intended_dynamic_groups = DynamicGroup.objects.exclude(
            golden_config_setting__isnull=True
        )
        intended_dynamic_group = intended_dynamic_groups[0]

        self.job.request.user = self.job.user
        status, device_data = graph_ql_query(
            self.job.request,
            self.device,
            intended_dynamic_group.golden_config_setting.sot_agg_query.query,
        )
        return status, device_data

    def capture_interfaces(self):
        """Get a list of interfaces from the device."""
        try:
            platform_commands = {
                "cisco_xr": (
                    "show interfaces",
                    "cisco_xr_show_interfaces.textfsm",
                ),
            }
            self.open()
            command, parser_file = platform_commands[
                self.device.platform.network_driver
            ]
            output = self.session.send_command(command)
            parsed_output = parse_command_output(output, parser_file)
            self.captured_data.update({"interfaces": parsed_output})
            self.job.logger.info(self.captured_data)

        except Exception as e:
            self.job.logger.error(f"Error processing device: {e}")
        finally:
            self.close()

        # Example output format:
        # [{
        #     "INTERFACE": "GigabitEthernet0/0/0/0",
        #     "LINK_STATUS": "administratively down",
        #     "ADMIN_STATE": "administratively down",
        #     "HARDWARE_TYPE": "GigabitEthernet",
        #     "MAC_ADDRESS": "20cf.ae15.6304",
        #     "BIA": "20cf.ae15.6304",
        #     "DESCRIPTION": "",
        #     "IP_ADDRESS": "Unknown",
        #     "MTU": "1514",
        #     "DUPLEX": "Full",
        #     "HARDWARE_MEDIA": "TFD",
        #     "SPEED": "1000Mb/s",
        #     "BANDWIDTH": "1000000 Kbit",
        #     "ENCAPSULATION": "ARPA",
        #     "VLAN_ID": "",
        #     "LAST_INPUT": "never",
        #     "LAST_OUTPUT": "never",
        #     "INPUT_RATE": "0",
        #     "OUTPUT_RATE": "0",
        #     "INPUT_PPS": "0",
        #     "OUTPUT_PPS": "0",
        #     "INPUT_PACKETS": "0",
        #     "OUTPUT_PACKETS": "0",
        #     "RUNTS": "0",
        #     "GIANTS": "0",
        #     "INPUT_ERRORS": "0",
        #     "CRC": "0",
        #     "FRAME": "0",
        #     "OVERRUN": "0",
        #     "ABORT": "0",
        #     "OUTPUT_ERRORS": "0"
        # }]

    def update_device_type(self, device, device_type):
        """Normalize and update the device type."""
        if device.device_type != device_type:
            device.device_type = device_type

    def update_interface_description(self, interface, description):
        """Normalize and update the interface description."""
        if interface.description != description:
            interface.description = description

    def update_interface_mgmt_only(self, interface, mgmt_only):
        """Normalize and update the interface management status."""
        if "yes" in mgmt_only.lower():
            interface.mgmt_only = True
        else:
            interface.mgmt_only = False

    def update_interface_mode(self, interface, mode):
        """Normalize and update the interface mode."""
        # access
        # tagged
        # tagged-all
        pass

    def update_interface_lag(self, interface, lag):
        """Normalize and update the interface LAG."""
        pass

    def update_interface_enabled(self, interface, enabled):
        """Normalize and update the interface enabled status."""
        if "up" in enabled.lower():
            interface.enabled = True
        else:
            interface.enabled = False

    def update_interface_mac_address(self, interface, mac_address):
        """Normalize and update the interface MAC address."""
        if mac_address:
            converted_mac = EUI(mac_address)
            if interface.mac_address != converted_mac:
                interface.mac_address = converted_mac

    def update_interface_mtu(self, interface, mtu):
        """Normalize and update the interface MTU."""
        if mtu:
            if interface.mtu != mtu:
                interface.mtu = mtu

    def update_interface_speed(self, interface, speed):
        """Normalize and update the interface speed."""
        if speed:
            if interface.cf.speed != speed:
                interface.cf.speed = speed

    def update_interface_duplex(self, interface, duplex):
        """Normalize and update the interface duplex."""
        if duplex:
            if interface.cf.duplex != duplex:
                interface.cf.duplex = duplex

    def update_interface_ip_address(self, interface, ip_address):
        """Normalize and update the interface IP address."""
        self.job.logger.info(f"Interface IP Address: {ip_address}")
        if ip_address:
            # if ip_address is a valid IP address
            if not is_ip((ip_address).split("/")[0]):
                self.job.logger.error(f"Invalid IP address: {ip_address}")
                return
            else:
                self.job.logger.info(f"Valid IP address: {ip_address}")

            self.get_or_create_prefix(ip_address)

            try:
                ip = IPAddress.objects.get(address=ip_address)
                if ip:
                    self.job.logger.info(f"Found IP address {ip_address} in Nautobot.")
                if interface.ip_addresses:
                    for ip in interface.ip_addresses.all():
                        if ip.address == ip_address:
                            self.job.logger.info(
                                f"IP address {ip_address} is already assigned."
                            )
                            return
                else:
                    interface.ip_addresses.add(ip)
            except ObjectDoesNotExist:
                ip = IPAddress.objects.create(
                    address=ip_address,
                    namespace=Namespace.objects.get(name="Global"),
                    status=Status.objects.get(name="Active"),
                )
                self.job.logger.info(f"IP address {ip_address} created.")
                interface.ip_addresses.add(ip)

    def get_or_create_prefix(self, ip_address):
        """Get or create a prefix."""
        try:
            ip_network = ipaddress.ip_network(ip_address, strict=False)
            ip_prefix = str(ip_network)
            prefix = Prefix.objects.get(prefix=ip_prefix)
            self.job.logger.info(f"Prefix {prefix} already exists.")
        except ObjectDoesNotExist:
            prefix = Prefix.objects.create(
                prefix=ip_prefix,
                status=Status.objects.get(name="Active"),
                namespace=Namespace.objects.get(name="Global"),
            )
            self.job.logger.info(f"Prefix {prefix} created.")
        return prefix

    def get_or_create_interface(self, name, device, type, status):
        """Get or create an interface."""
        try:
            interface = Interface.objects.get(
                name=name,
                device=device,
            )
        except ObjectDoesNotExist:
            interface = Interface(
                name=name,
                device=device,
                type=type,
                status=status,
            )
        return interface

    def import_interfaces(self):
        """Import interfaces into Nautobot."""
        try:
            for item in self.captured_data["interfaces"]:
                interface = self.get_or_create_interface(
                    item["INTERFACE"],
                    self.device,
                    InterfaceTypeChoices.TYPE_1GE_FIXED,
                    # type=item["HARDWARE_TYPE"], # TODO: create a mapping for hardware types
                    Status.objects.get(name="Active"),
                )

                ### Interface imports

                # name: "TenGigE0/0/0/21"
                # type__value: "1GE_FIXED"
                # status__name: "Active"
                # enabled: True
                # description: "This is a test description"
                # mgmt_only: False
                # mode:
                # - access
                #   - untagged_vlan
                # - tagged
                #   - untagged_vlan
                #   - tagged_vlans
                # - tagged-all
                #   - untagged_vlan
                #   - tagged_vlans
                # lag
                # ip_addresses:
                # - address
                # mac_address
                # mtu
                # custom_fields:
                # - speed
                # - duplex
                # cable

                self.job.logger.info(f"Interface: {interface}")
                # self.update_device_type(self.device, self.device.device_type)
                self.update_interface_enabled(interface, item["LINK_STATUS"])
                self.update_interface_description(interface, item["DESCRIPTION"])
                # self.update_interface_mgt_only(interface, item["INTERFACE"])
                # self.update_interface_mode(interface, item["MODE"])
                # self.update_interface_lag(interface, item["LAG"])
                self.update_interface_mac_address(interface, item["MAC_ADDRESS"])
                self.update_interface_mtu(interface, item["MTU"])
                # self.update_interface_speed(interface, interf["SPEED"])
                # self.update_interface_duplex(interface, interf["DUPLEX"])

                try:
                    self.update_interface_ip_address(interface, item["IP_ADDRESS"])
                except Exception as e:
                    self.job.logger.error(f"Error processing IP address: {e}")
                    continue

                interface.validated_save()
                self.job.logger.info(f"Interface {interface} updated.")

        except Exception as e:
            self.job.logger.error(f"Error processing device: {e}")
        finally:
            self.close()

    def execute(self):
        self.capture_interfaces()
        self.import_interfaces()

        # # Synchronize with the device data in Nautobot
        # status, device_data = self.graphql_query()
        # self.job.logger.info(f"Status: {status}")
        # self.job.logger.info(f"Device Data: {json.dumps(device_data, indent=2)}")


register_jobs(CustomCaptureDeviceData)
