"""
Purpose:
- Provide a job form that will allow the user to select a device from Nautobot
- Run a firmware version check on the device
"""

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from netmiko import ConnectHandler
from nautobot.dcim.models import Device, Location
from nautobot.apps.jobs import register_jobs, Job, ObjectVar
from nautobot.extras.models.secrets import (
    SecretsGroup,
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)
from nautobot.extras.models import Relationship, RelationshipAssociation
from nautobot_device_lifecycle_mgmt.models import SoftwareLCM

import re
import os

name = "Upgrading"


class ReadinessCheck(Job):
    location_to_check = ObjectVar(model=Location)

    class Meta:
        name = "Check Device Firmware Upgrade Readiness"
        description = "Supported platforms: keymile_nos"
        has_sensitive_variables = False
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
            "bulk",
        ]

    def run(self, location_to_check):
        devices = Device.objects.filter(location=location_to_check)
        for device in devices:
            self.logger.info(f"{device} Processing device: {device}")
            try:
                task = CheckVersion(self, device)
                task.execute()
            except Exception as e:
                self.logger.error(f"{device} Error processing device {device}: {e}")


def print_status(job, status, message):
    if status == "info":
        job.logger.info(message)
    elif status == "success":
        job.logger.success(message)
    elif status == "warning":
        job.logger.warning(message)
    elif status == "failure":
        job.logger.failure(message)


class CheckVersion:
    def __init__(self, job, device):
        self.job = job
        self.device = device
        self.supported_drivers = ["keymile_nos"]
        print_status(
            self.job,
            "info",
            f"{self.device} Supported platforms: {self.supported_drivers}",
        )

        self.platform = self.device.platform.network_driver
        if self.platform not in self.supported_drivers:
            raise Exception(
                f"{self.device} Device with platform {self.platform} is not supported"
            )

        self.device_info = self.get_device_connection_info()
        self.show_version_commands = self.get_show_version_commands()
        self.patterns = self.get_patterns()

        self.raw_version = None
        self.parsed_version = None
        self.nautobot_software = None

    def get_device_connection_info(self):
        secrets_group = SecretsGroup.objects.get(name=self.device.secrets_group)
        return {
            "device_type": self.platform,
            "ip": self.device.primary_ip.host,
            "username": secrets_group.get_secret_value(
                access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
                secret_type=SecretsGroupSecretTypeChoices.TYPE_USERNAME,
                obj=self.device,
            ),
            "password": secrets_group.get_secret_value(
                access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
                secret_type=SecretsGroupSecretTypeChoices.TYPE_PASSWORD,
                obj=self.device,
            ),
            "secret": secrets_group.get_secret_value(
                access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
                secret_type=SecretsGroupSecretTypeChoices.TYPE_SECRET,
                obj=self.device,
            ),
        }

    def get_show_version_commands(self):
        return {
            "arista_eos": "show version",
            "keymile_nos": "show version",
            "cisco_xr": "show version",
            "cisco_ios": "show version | inc Cisco IOS Software",
            "mikrotik_routeros": "/system resource print",
            "fiberstore_fsos": "show version",
        }

    def get_patterns(self):
        return {
            "arista_eos": r"Software image version: (\S+)",
            "keymile_nos": r"NOS version (\S+)",
            "cisco_xr": r"Cisco IOS XR Software, Version (\S+)",
            "cisco_ios": r"Version (\S+)",
            "mikrotik_routeros": r"version: (\S+) ",
            "fiberstore_fsos": r"FSOS Software, \w+, Version (\S+)",
        }

    def get_version(self):
        print_status(self.job, "info", f"{self.device} Device: {self.device}")
        print_status(self.job, "info", f"{self.device} IP: {self.device_info['ip']}")
        print_status(
            self.job,
            "info",
            f"{self.device} Platform: {self.device_info['device_type']}",
        )

        # if os.system(f"ping -c 1 {self.device_info['ip']}") != 0:
        #     raise Exception(f"Device with IP {self.device_info['ip']} is unreachable")

        with ConnectHandler(**self.device_info) as session:
            session.enable()
            self.raw_version = session.send_command(
                self.show_version_commands[self.platform]
            )

    def parse_version(self):
        match = re.search(self.patterns[self.platform], self.raw_version)
        if match:
            self.parsed_version = match.group(1).strip(",")
            print_status(
                self.job,
                "info",
                f"{self.device} Software version: {self.parsed_version}",
            )
        else:
            raise Exception(
                f"Pattern not found in output: {self.patterns[self.platform]}"
            )

    def execute(self):
        self.get_version()
        self.parse_version()


register_jobs(ReadinessCheck)
