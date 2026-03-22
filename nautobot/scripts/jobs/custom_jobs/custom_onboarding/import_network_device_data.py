"""Purpose: Import device interfaces from actual devices to Nautobot."""

from nautobot.dcim.models import Interface, Device
from nautobot.apps.jobs import register_jobs, Job
from nautobot_ssot.contrib import NautobotModel, NautobotAdapter, CustomFieldAnnotation
from nautobot_ssot.jobs.base import DataSource, DataMapping
from nautobot.extras.models import Status
from nautobot.extras.jobs import ObjectVar, StringVar

from custom_jobs.modules.tools import get_device_connection_info
from custom_jobs.modules.tools import parse_command_output
from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import convert_flat_config_to_dict

from netmiko import ConnectHandler
from textfsm import TextFSM
import requests
import json

name = "Custom Importing"

SUPPORTED_PLATFORMS = [
    "fiberstore_fsos",
]


class DeviceCLIScraper:
    def __init__(self, device, job):
        self.device = device
        self.job = job
        self.session = None
        self.device_info = get_device_connection_info(self.device)

    def connect(self):
        """Open an SSH connection to the device."""
        self.session = ConnectHandler(**self.device_info)
        self.session.enable()
        self.job.logger.info(f"Device info: {self.device_info}")

    def disconnect(self):
        """Close the SSH connection to the device."""
        if self.session:
            self.session.disconnect()
            self.job.logger.info("Disconnected from device")

    def get_interfaces(self):
        """Get a list of interfaces from the device."""
        platform_commands = {
            "fiberstore_fsos": (
                "show interfaces",
                "fiberstore_fsos_show_interfaces.textfsm",
            ),
        }
        try:
            command, template = platform_commands[self.device.platform.network_driver]
            self.connect()
            output = self.session.send_command(command)
            parsed_output = parse_command_output(output, template)
            return parsed_output
        except Exception as e:
            self.job.logger.error(f"Failed to get interfaces: {e}")
            return []
        finally:
            self.disconnect()

    def get_serial_number(self):
        """Get the serial number of the device."""
        platform_commands = {
            "fiberstore_fsos": ("show version", "fiberstore_fsos_show_version.textfsm"),
        }
        try:
            command, template = platform_commands[self.device.platform.network_driver]
            self.connect()
            output = self.session.send_command(command)
            parsed_output = parse_command_output(output, template)
            return parsed_output[0]["SERIAL_NUMBER"]
        except Exception as e:
            self.job.logger.error(f"Failed to get serial number: {e}")
            return None
        finally:
            self.disconnect()
