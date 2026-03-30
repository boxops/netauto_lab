"""
Purpose:
- Provide a job form that will allow the user to select a device from Nautobot
- Add an option to upload a firmware image by the user, or use a default firmware image
- Run a firmware version check on the device before upgrading
- If the device firmware differs to the intended firmware, then run the upgrade

# Example classes for each platform
class DeviceFirmwareUpgrade:
    def __init__(self, device, firmware_image):
        # Initialize the firmware upgrade process.
        # :param firmware_image: Path to the firmware image file.
        self.device = device
        self.firmware_image = firmware_image

    def connect(self):
        # Establish a connection to the device.
        print(f"Connecting to device {self.device_ip} on port {self.upgrade_port}...")
        print("Connection established.")

    def disconnect(self):
        # Disconnect from the device.
        print(f"Disconnecting from device {self.device_ip}...")
        print("Disconnection successful.")

    def gather_device_info(self):
        # Gather device information to determine upgrade requirements.
        print(f"Gathering device information for {self.device_ip}...")
        print("Device information gathered.")

    def validate_firmware(self):
        # Validate the firmware image file (e.g., checksum verification).
        print(f"Validating firmware image at {self.firmware_image}...")
        print("Firmware validation successful.")

    def upload_firmware(self):
        # Upload the firmware image to the device.
        print(f"Uploading firmware image to device {self.device_ip}...")
        print("Firmware upload complete.")

    def apply_upgrade(self):
        # Apply the firmware upgrade on the device.
        print("Applying firmware upgrade...")
        print("Firmware upgrade applied.")

    def verify_upgrade(self):
        # Verify the firmware upgrade was successful.
        print("Verifying firmware upgrade status...")
        print("Firmware upgrade verified successfully.")

    def upgrade_firmware(self):
        # Full firmware upgrade workflow.
        self.connect()
        self.gather_device_info()
        self.validate_firmware()
        if self.validate_firmware():
            self.upload_firmware()
            self.apply_upgrade()
            self.verify_upgrade()
"""

import time
import os
import subprocess
from django.conf import settings
from netmiko import ConnectHandler

from nautobot.apps.jobs import register_jobs, Job, ObjectVar, BooleanVar, IntegerVar
from nautobot_device_lifecycle_mgmt.models import SoftwareLCM, SoftwareImageLCM
from nautobot.extras.models.secrets import SecretsGroup

from custom_jobs.modules.tools import get_device_connection_info
from custom_jobs.modules.tools import parse_command_output
from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import get_ftp_server_credentials
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import parallel_execution


name = "Upgrading"

SUPPORTED_PLATFORMS = [
    "keymile_nos",
    "fiberstore_fsos",
    "mikrotik_routeros",
    "netonix_os",
    # "cisco_ios",
    # "cisco_xr",
    # "cisco_xe",
    # "cisco_nxos",
    # "cisco_s300",
    # "ubiquiti_airos",
    "arista_eos",
]


def get_default_credential():
    try:
        return SecretsGroup.objects.get(name="AB-THN-FTP01")
    except SecretsGroup.DoesNotExist:
        return None


class FirmwareUpgrade(Job, DeviceFormEntry):
    """Job to upgrade device firmware."""

    software = ObjectVar(
        model=SoftwareLCM,
        description="Software to upgrade to",
        required=True,
    )
    ftp_server = ObjectVar(
        model=SecretsGroup,
        description="FTP server Secrets Group",
        required=True,
        default=get_default_credential(),
    )
    dry_run = BooleanVar(
        description="Run show commands only",
        required=False,
        default=False,
    )
    parallel_task = BooleanVar(
        description="Execute tasks in parallel",
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
        name = "Upgrade Device Firmware"
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
        software=None,
        ftp_server=None,
        dry_run=False,
        parallel_task=False,
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

        def run_firmware_upgrade(device):
            platform_to_class_mapping = {
                "keymile_nos": Keymile_NOS,
                "fiberstore_fsos": Fiberstore_FSOS,
                "mikrotik_routeros": Mikrotik_RouterOS,
                "netonix_os": Netonix_OS,
                # "ubiquiti_airos": Ubiquiti_AirOS,
            }
            try:
                if device.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.error(
                        f"{device} Platform {device.platform.network_driver} is not supported. Skipping..."
                    )
                    return

                software_image = self.find_software_in_software_images(software, device)
                if not software_image:
                    self.logger.error(
                        f"{device} Software {software} or device type {device.device_type} not found in software images. Skipping..."
                    )
                    return

                self.logger.info(f"{device} Processing device...")
                task = platform_to_class_mapping[device.platform.network_driver](
                    self, device, software, software_image, ftp_server, dry_run
                )
                task.upgrade_firmware()
            except Exception as e:
                self.logger.error(f"{device} Error processing device: {e}")

        if parallel_task:
            parallel_execution(
                run_firmware_upgrade, all_devices, max_workers=max_workers
            )
        else:
            for device in all_devices:
                run_firmware_upgrade(device)

    def find_software_in_software_images(self, software, device):
        software_images = SoftwareImageLCM.objects.all()
        for software_image in software_images:
            if software.version == software_image.software.version:
                device_types = software_image.device_types.all()
                if device.device_type in device_types:
                    return software_image
        return None


class Keymile_NOS:

    def __init__(self, job, device, software, software_image, ftp_server, dry_run):
        """
        Initialize the firmware upgrade process.

        :param job: Job object.
        :param device: Device object to upgrade.
        :param software: Software to upgrade to.
        :param software_image: Software image to upload.
        :param ftp_server: FTP server credentials.
        :param dry_run: Dry run mode.
        """
        self.job = job
        self.device = device
        self.software_version = software.version
        self.software_image_location = f"/Keymile/{software_image.image_file_name}"
        self.dry_run = dry_run
        if self.dry_run:
            self.job.logger.info(f"{self.device} Dry run enabled.")
        self.job.logger.info(
            f"{self.device} Selected software version: {self.software_version}"
        )
        self.job.logger.info(
            f"{self.device} Selected software image: {software_image.image_file_name}"
        )

        self.device_info = get_device_connection_info(self.device)
        self.ftp_server = get_ftp_server_credentials(ftp_server)

        self.session = None
        self.running_os_version = None
        self.non_running_os_version = None
        self.running_os_name = None
        self.non_running_os_name = None
        self.reboot_required = None
        self.firmware_upgrade_required = None

    def connect(self):
        """Establish a connection to the device."""
        try:
            self.job.logger.info(f"{self.device} Connecting to device...")
            self.session = ConnectHandler(**self.device_info)
            self.session.enable()
            self.job.logger.info(f"{self.device} Connection established.")
        except Exception as e:
            self.job.logger.error(f"{self.device} Error connecting to device: {e}")

    def disconnect(self):
        """Disconnect from the device."""
        try:
            self.session.disconnect()
            self.job.logger.info(f"{self.device} Disconnected from device.")
        except Exception as e:
            self.job.logger.error(f"{self.device} Error disconnecting from device: {e}")

    def validate_software(self):
        """Validate if the software version is correct for the device."""
        # TODO: Check if the device type is correct for the software version
        pass

    def gather_device_info(self):
        """Gather device information to determine upgrade requirements."""
        self.job.logger.info(f"{self.device} Gathering device information...")
        self.job.logger.info(f"{self.device} Sending command: show flash")
        show_flash_output = self.session.send_command_timing("show flash")
        parsed_output = parse_command_output(
            show_flash_output, "keymile_nos_show_flash.textfsm"
        )
        # [
        #     {
        #         "AREA": "OS1(default)(running)",
        #         "TOTAL": "33554432",
        #         "USED": "28969532",
        #         "FREE": "4584900",
        #         "VERSION": "1.12p3",
        #         "SUB_VERSION": "#0099"
        #     },
        #     {
        #         "AREA": "OS2",
        #         "TOTAL": "33554432",
        #         "USED": "0",
        #         "FREE": "33554432",
        #         "VERSION": "",
        #         "SUB_VERSION": ""
        #     },
        #     {
        #         "AREA": "CONFIG",
        #         "TOTAL": "4194304",
        #         "USED": "487424",
        #         "FREE": "3706880",
        #         "VERSION": "",
        #         "SUB_VERSION": ""
        #     }
        # ]
        os_mapping = {"OS1": "os1", "OS2": "os2"}
        for os in parsed_output:
            for key, value in os_mapping.items():
                if key in os["AREA"]:
                    if "running" in os["AREA"]:
                        self.running_os_name = value
                        self.running_os_version = os["VERSION"]
                    else:
                        self.non_running_os_name = value
                        self.non_running_os_version = os["VERSION"]

    def determine_upgrade_requirements(self):
        """Determine if a firmware upgrade is required."""
        self.firmware_upgrade_required = (
            self.running_os_version != self.software_version
            and self.software_version != self.non_running_os_version
        )
        self.reboot_required = (
            self.running_os_version != self.software_version
            and self.software_version == self.non_running_os_version
        )

    def upload_firmware(self):
        """Upload the firmware image to the device."""
        timer = 180
        self.job.logger.info(
            f"{self.device} Uploading firmware image to device... {timer} seconds left"
        )
        commands = [
            f"copy ftp os download {self.non_running_os_name}",
            self.ftp_server["host"],
            self.software_image_location,
            self.ftp_server["username"],
            self.ftp_server["password"],
        ]

        for command in commands:
            self.job.logger.info(f"{self.device} Sending command: {command}")
            if not self.dry_run:
                self.session.send_command_timing(command)

        if not self.dry_run:
            time.sleep(timer)

        self.job.logger.info(f"{self.device} Firmware upload complete.")

    def apply_upgrade(self):
        """Apply the firmware upgrade on the device."""
        self.job.logger.info(f"{self.device} Applying firmware upgrade...")
        commands = [f"default-os {self.non_running_os_name}", "write memory"]

        for command in commands:
            self.job.logger.info(f"{self.device} Sending command: {command}")
            if not self.dry_run:
                self.session.send_command_timing(command)

        self.job.logger.info(f"{self.device} Firmware upgrade applied.")

    def verify_upgrade(self):
        """Verify the firmware upgrade was successful."""
        self.job.logger.info(f"{self.device} Verifying firmware upgrade status...")
        self.gather_device_info()
        self.determine_upgrade_requirements()
        self.generate_report()
        self.job.logger.info(f"{self.device} Firmware upgrade verification complete.")

    def generate_report(self):
        """Generate a report of the firmware upgrade process."""
        self.job.logger.info(f"{self.device} Running OS name: {self.running_os_name}")
        self.job.logger.info(
            f"{self.device} Running OS version: {self.running_os_version}"
        )
        self.job.logger.info(
            f"{self.device} Non-running OS name: {self.non_running_os_name}"
        )
        self.job.logger.info(
            f"{self.device} Non-running OS version: {self.non_running_os_version}"
        )
        self.job.logger.info(
            f"{self.device} Firmware upgrade required: {self.firmware_upgrade_required}"
        )
        self.job.logger.info(f"{self.device} Reboot required: {self.reboot_required}")
        self.job.logger.info(f"{self.device} ...")

    def upgrade_firmware(self):
        """Full firmware upgrade workflow."""
        try:
            self.connect()
            self.gather_device_info()
            self.determine_upgrade_requirements()
            self.generate_report()
            self.validate_software()
            if self.firmware_upgrade_required:
                self.upload_firmware()
                self.apply_upgrade()
                self.verify_upgrade()
        except Exception as e:
            self.job.logger.error(f"{self.device} Error upgrading firmware: {e}")
        finally:
            self.disconnect()
            self.job.logger.info(f"{self.device} Firmware upgrade process complete.")


"""
FSCOM Firmware Upgrade Steps:
=============================

1. Check version: 
- show version

2. SCP copy the image to the switch:
If 24S2Q in show version output:
- copy ftp://username:password@10.1.1.1/FS/S5850-24S2Q-Switches-FSOS-V7.4.5.R.bin flash:/boot
If 32S2Q in show version output:
- copy ftp://username:password@10.1.1.1/FS/S5850-32S2Q-Switches-FSOS-V7.4.5.R.bin flash:/boot

3. Validate the next time boot image: 
- show boot = output

4. Set the default boot image:
If 24S2Q:
boot system flash:/boot/S5850-24S2Q-Switches-FSOS-V7.4.5.R.bin
If 32S2Q:
boot system flash:/boot/S5850-32S2Q-Switches-FSOS-V7.4.5.R.bin

FSCOM Reboot Steps:
===================

The switches need to be rebooted to load in and use the latest firmware.

1. Reboot the switch:
- reboot

2. Wait for the switch to come back online:
- wait_for_device

3. Validate the current running image:
- show boot = output

FSCOM Reboot Process:

S5850-32S2Q# reboot
Building configuration...
Reboot system? [confirm] enter
Restarting system.
"""


class Fiberstore_FSOS:

    def __init__(self, job, device, software, software_image, ftp_server, dry_run):
        """
        Initialize the firmware upgrade process.

        :param job: Job object.
        :param device: Device object to upgrade.
        :param software: Software to upgrade to.
        :param software_image: Software image to upload.
        :param ftp_server: FTP server credentials.
        :param dry_run: Dry run mode.
        """
        self.job = job
        self.device = device
        self.software_version = software.version  # 7.4.5
        self.software_image_name = (
            software_image.image_file_name
        )  # S5850-24S2Q-Switches-FSOS-V7.4.5.R.bin
        self.software_image_location = f"/FS/{self.software_image_name}"  # /FS/S5850-24S2Q-Switches-FSOS-V7.4.5.R.bin
        self.dry_run = dry_run
        if self.dry_run:
            self.job.logger.info(f"{self.device} Dry run enabled.")
        self.job.logger.info(
            f"{self.device} Selected software version: {self.software_version}"
        )
        self.job.logger.info(
            f"{self.device} Selected software image: {self.software_image_name}"
        )

        self.device_info = get_device_connection_info(self.device)
        self.ftp_server = get_ftp_server_credentials(ftp_server)

        self.session = None
        self.model = None  # S5800
        self.hardware_type = None  # 24S2Q or 32S2Q
        self.running_image = None  # flash:/boot/S5850-24S2Q-Switches-FSOS-V7.4.5.R.bin
        self.firmware_upgrade_required = None  # True or False
        self.reboot_required = None  # True or False

    def connect(self):
        """Establish a connection to the device."""
        try:
            self.job.logger.info(f"{self.device} Connecting to device...")
            self.session = ConnectHandler(**self.device_info)
            self.session.enable()
            self.job.logger.info(f"{self.device} Connection established.")
        except Exception as e:
            self.job.logger.error(f"{self.device} Error connecting to device: {e}")

    def disconnect(self):
        """Disconnect from the device."""
        try:
            self.session.disconnect()
            self.job.logger.info(f"{self.device} Disconnected from device.")
        except Exception as e:
            self.job.logger.error(f"{self.device} Error disconnecting from device: {e}")

    def validate_software(self):
        """Validate if the software version is correct for the device."""
        pass

    def gather_device_info(self):
        """Gather device information to determine upgrade requirements."""
        self.job.logger.info(f"{self.device} Gathering device information...")
        self.job.logger.info(f"{self.device} Sending command: show version")
        show_version_output = self.session.send_command_timing("show version")
        parsed_version_output = parse_command_output(
            show_version_output, "fiberstore_fsos_show_version.textfsm"
        )
        # [
        #     {
        #         "BOOTROM_VERSION": "A.2.9",
        #         "FLASH_SIZE": "8192M",
        #         "HARDWARE_TYPE": "24S2Q",
        #         "HARDWARE_VERSION": "2.1",
        #         "HOSTNAME": "AB-BAR-NF013-1-SW01",
        #         "MODEL": "S5800",
        #         "RUNNING_IMAGE": "flash:/boot/S5850-24S2Q-Switches-FSOS-V7.4.5.R.bin",
        #         "SDRAM_SIZE": "2048M",
        #         "SERIAL_NUMBER": "PCW202010078277N0004",
        #         "UPTIME": "559 days, 17 hours, 36 minutes",
        #         "VERSION": "7.4.5",
        #         "WEB_VERSION": "7.2.5.r1",
        #     }
        # ]
        self.model = parsed_version_output[0]["MODEL"]
        self.hardware_type = parsed_version_output[0]["HARDWARE_TYPE"]

    # TODO: Retest this function
    def determine_upgrade_requirements(self):
        """Determine if a firmware upgrade is required."""
        self.job.logger.info(f"{self.device} Sending command: show boot")
        show_boot_output = self.session.send_command_timing("show boot")
        parsed_boot_output = parse_command_output(
            show_boot_output, "fiberstore_fsos_show_boot.textfsm"
        )
        # [
        #     {
        #         "CURRENT_BOOT_IMAGE_VERSION": "S5800-7.4.5",
        #         "CURRENT_RUNNING_IMAGE": "flash:/boot/S5850-24S2Q-Switches-FSOS-V7.4.5.R.bin",
        #         "NEXT_BOOT_IMAGE_VERSION": "v7.4.5",
        #         "NEXT_RUNNING_IMAGE": "flash:/boot/S5850-24S2Q-Switches-FSOS-V7.4.5.R.bin",
        #     }
        # ]
        self.running_image = parsed_boot_output[0]["CURRENT_RUNNING_IMAGE"]
        self.firmware_upgrade_required = self.running_image != self.software_image_name
        self.reboot_required = (
            self.software_image_name not in self.running_image
            and self.software_image_name in parsed_boot_output[0]["NEXT_RUNNING_IMAGE"]
        )

    def upload_firmware(self):
        """Upload the firmware image to the device."""
        timer = 400
        self.job.logger.info(
            f"{self.device} Uploading firmware image to device... {timer} seconds left"
        )

        command = f"copy ftp://{self.ftp_server['username']}:{self.ftp_server['password']}@{self.ftp_server['host']}{self.software_image_location} flash:/boot"
        self.job.logger.info(f"{self.device} Sending command: {command}")

        if not self.dry_run:
            self.session.send_command_timing(command)
            time.sleep(timer)

        self.job.logger.info(f"{self.device} Firmware upload complete.")

    def apply_upgrade(self):
        """Apply the firmware upgrade on the device."""
        self.job.logger.info(f"{self.device} Applying firmware upgrade...")

        command = f"boot system flash:/boot/{self.software_image_name}"
        self.job.logger.info(f"{self.device} Sending command: {command}")

        if not self.dry_run:
            self.session.send_command_timing(command)
            self.session.send_command_timing("write memory")

        self.job.logger.info(f"{self.device} Firmware upgrade applied.")

    def verify_upgrade(self):
        """Verify the firmware upgrade was successful."""
        self.job.logger.info(f"{self.device} Verifying firmware upgrade status...")
        self.gather_device_info()
        self.determine_upgrade_requirements()
        self.generate_report()
        self.job.logger.info(f"{self.device} Firmware upgrade verification complete.")

    def generate_report(self):
        """Generate a report of the firmware upgrade process."""
        self.job.logger.info(f"{self.device} Model: {self.model}")
        self.job.logger.info(f"{self.device} Hardware type: {self.hardware_type}")
        self.job.logger.info(f"{self.device} Running image: {self.running_image}")
        self.job.logger.info(
            f"{self.device} Firmware upgrade required: {self.firmware_upgrade_required}"
        )
        self.job.logger.info(f"{self.device} Reboot required: {self.reboot_required}")
        self.job.logger.info(f"{self.device} ...")

    def upgrade_firmware(self):
        """Full firmware upgrade workflow."""
        try:
            self.connect()
            self.gather_device_info()
            self.determine_upgrade_requirements()
            self.generate_report()
            self.validate_software()
            if self.firmware_upgrade_required:
                self.upload_firmware()
                self.apply_upgrade()
                self.verify_upgrade()
        except Exception as e:
            self.job.logger.error(f"{self.device} Error upgrading firmware: {e}")
        finally:
            self.disconnect()
            self.job.logger.info(f"{self.device} Firmware upgrade process complete.")


### https://forum.mikrotik.com/viewtopic.php?t=175781
## Copy over the image to the device:
# scp /home/user/routeros-tile-6.49.13.npk username@10.1.1.1:/routeros-tile-6.49.13.npk


# TODO: Add support for different versions of Mikrotik OS
# TODO: Check firmware-type: tilegx
class Mikrotik_RouterOS:

    def __init__(self, job, device, software, software_image, ftp_server, dry_run):
        """
        Initialize the firmware upgrade process.

        :param job: Job object.
        :param device: Device object to upgrade.
        :param software: Software to upgrade to.
        :param software_image: Software image to upload.
        :param ftp_server: FTP server credentials.
        :param dry_run: Dry run mode.
        """
        self.job = job
        self.device = device
        self.software_version = software.version  # 6.47.9
        self.software_image_name = software_image.image_file_name  # mikrotik-6.47.9.npk
        self.software_image_location = (
            f"/Mikrotik/{self.software_image_name}"  # /Mikrotik/mikrotik-6.47.9.npk
        )
        self.dry_run = dry_run
        if self.dry_run:
            self.job.logger.info(f"{self.device} Dry run enabled.")
        self.job.logger.info(
            f"{self.device} Selected software version: {self.software_version}"
        )
        self.job.logger.info(
            f"{self.device} Selected software image: {self.software_image_name}"
        )

        self.device_info = get_device_connection_info(self.device)
        self.ftp_server = get_ftp_server_credentials(ftp_server)

        self.firmware_image_path = self.download_firmware_from_ftp()

        self.session = None
        self.firmware_version = None  # 6.47.9
        self.firmware_upgrade_required = None  # True or False
        self.reboot_required = None  # True or False

    def connect(self):
        """Establish a connection to the device."""
        try:
            self.job.logger.info(f"{self.device} Connecting to device...")
            self.session = ConnectHandler(**self.device_info)
            self.session.enable()
            self.job.logger.info(f"{self.device} Connection established.")
        except Exception as e:
            self.job.logger.error(f"{self.device} Error connecting to device: {e}")

    def disconnect(self):
        """Disconnect from the device."""
        try:
            self.session.disconnect()
            self.job.logger.info(f"{self.device} Disconnected from device.")
        except Exception as e:
            self.job.logger.error(f"{self.device} Error disconnecting from device: {e}")

    def validate_software(self):
        """Validate if the software version is correct for the device."""
        pass

    def gather_device_info(self):
        """Gather device information to determine upgrade requirements."""
        self.job.logger.info(f"{self.device} Gathering device information...")
        self.job.logger.info(
            f"{self.device} Sending command: /system routerboard print"
        )
        show_package_output = self.session.send_command_timing(
            "/system routerboard print"
        )
        parsed_output = parse_command_output(
            show_package_output, "mikrotik_routeros_system_routerboard_print.textfsm"
        )
        # [
        #     {
        #         "ROUTERBOARD": "yes",
        #         "BOARD_NAME": "",
        #         "MODEL": "CCR1009-7G-1C-1S+",
        #         "REVISION": "",
        #         "SERIAL_NUMBER": "6F510667D11A",
        #         "FIRMWARE_TYPE": "tilegx",
        #         "FACTORY_FIRMWARE": "3.33",
        #         "VERSION": "6.49.3",
        #         "UPGRADE_FIRMWARE": "6.49.3"
        #     }
        # ]
        self.firmware_version = parsed_output[0]["VERSION"]

    def determine_upgrade_requirements(self):
        """Determine if a firmware upgrade is required."""
        self.firmware_upgrade_required = self.firmware_version != self.software_version
        self.reboot_required = self.firmware_upgrade_required

    def generate_report(self):
        """Generate a report of the firmware upgrade process."""
        self.job.logger.info(f"{self.device} Firmware version: {self.firmware_version}")
        self.job.logger.info(
            f"{self.device} Firmware upgrade required: {self.firmware_upgrade_required}"
        )
        self.job.logger.info(f"{self.device} Reboot required: {self.reboot_required}")
        self.job.logger.info(f"{self.device} ...")

    def download_firmware_from_ftp(self):
        PERSISTENT_DIR = "/opt/nautobot/firmware_images/"
        local_path = os.path.join(PERSISTENT_DIR, self.software_image_name)

        if not os.path.exists(local_path):
            try:
                subprocess.run(["mkdir", "-p", PERSISTENT_DIR], check=True)
                # curl -u username:password ftp://10.1.1.1:/Mikrotik/mikrotik-6.47.9.npk -o /opt/nautobot/firmware_images/mikrotik-6.47.9.npk
                self.job.logger.info(
                    f"{self.device} Downloading {self.software_image_location} to {local_path}..."
                )
                subprocess.run(
                    [
                        "curl",
                        f"-u {self.ftp_server['username']}:{self.ftp_server['password']}",
                        f"ftp://{self.ftp_server['host']}{self.software_image_location}",
                        "-o",
                        local_path,
                    ],
                    check=True,
                )
                self.job.logger.info(
                    f"{self.device} Downloaded {self.software_image_location} to {local_path}"
                )
            except subprocess.CalledProcessError as e:
                self.job.logger.error(f"{self.device} An error occurred: {e}")

        else:
            self.job.logger.info(
                f"{self.device} File {self.software_image_name} already exists in {PERSISTENT_DIR}. Skipping download."
            )
        return local_path

    def upload_firmware_to_remote(self):
        if not isinstance(self.firmware_image_path, str):
            self.job.logger.error(
                f"{self.device} Invalid local_path: expected str, got {type(self.firmware_image_path).__name__}"
            )
            return

        try:
            # sshpass -p password scp -o StrictHostKeyChecking=no /opt/nautobot/firmware_images/mikrotik-6.47.9.npk username@10.1.1.1:/mikrotik-6.47.9.npk
            self.job.logger.info(
                f"{self.device} Uploading {self.firmware_image_path} to device"
            )
            subprocess.run(
                [
                    "sshpass",
                    "-p",
                    self.device_info["password"],
                    "scp",
                    "-o",
                    "StrictHostKeyChecking=no",
                    self.firmware_image_path,
                    f"{self.device_info['username']}@{self.device_info['ip']}:/{self.software_image_name}",
                ],
                check=True,
            )
            self.job.logger.info(
                f"{self.device} Uploaded {self.firmware_image_path} to device"
            )
        except subprocess.CalledProcessError as e:
            self.job.logger.error(f"{self.device} An error occurred: {e}")

    def upgrade_firmware(self):
        """Full firmware upgrade workflow."""
        try:
            self.connect()
            self.gather_device_info()
            self.determine_upgrade_requirements()
            self.generate_report()
            self.validate_software()
            if self.firmware_upgrade_required:
                self.upload_firmware_to_remote()
                self.generate_report()
        except Exception as e:
            self.job.logger.error(f"{self.device} Error upgrading firmware: {e}")
        finally:
            self.disconnect()
            self.job.logger.info(f"{self.device} Firmware upgrade process complete.")


"""
Netonix Firmware Upgrade Steps:

1. Check version:
- show status
Grab output:
- Firmware Version: 1.5.14
- Model: WS-26-500-DC

2. SCP copy the image to the switch if the version is not 1.5.14:
scp wispswitch-1.5.12.bin admin@<switch ip>:/tmp

3. At the CLI use "cmdline" to get to the linux command prompt and run "firmware_upgrade /tmp/wispswitch-1.5.12.bin"

4. Wait for the switch to reboot and come back online, then validate the current running image:
- show status
If the model is WS and the firmware version is 1.5.14, then the upgrade was successful.


Netonix Firmware Upgrade Process:

admin@Netonix_Switch:/tmp# firmware_upgrade wispswitch-1.5.14.bin 
Unpacking firmware ...
Running preflash script ...
Unlocking /dev/mtd8 ...
Writing from redboot_recovery_config to /dev/mtd8 ... 
Unlocking linux ...
Writing from kernel.img to linux ... 
23%
46%
69%
93%
100%

Updating FIS entry linux
Unlocking FIS directory ...
Writing from fisdir to FIS directory ... 
Unlocking Redundant FIS ...
Writing from fisdir to Redundant FIS ... 
Unlocking rootfs ...
Writing from rootfs.img to rootfs ... 
4%
8%
12%
16%
21%
25%
29%
33%
37%
42%
46%
50%
54%
58%
63%
67%
71%
75%
79%
84%
88%
92%
96%
100%

Unlocking /dev/mtd8 ...
Writing from redboot_config to /dev/mtd8 ... 
Running postflash script ...
Done!
admin@Netonix_Switch:/tmp# 
"""


class Netonix_OS:

    def __init__(self, job, device, software, software_image, ftp_server, dry_run):
        """
        Initialize the firmware upgrade process.

        :param job: Job object.
        :param device: Device object to upgrade.
        :param software: Software to upgrade to.
        :param software_image: Software image to upload.
        :param ftp_server: FTP server credentials.
        :param dry_run: Dry run mode.
        """
        self.job = job
        self.device = device
        self.software_version = software.version  # 1.5.14
        self.software_image_name = (
            software_image.image_file_name
        )  # wispswitch-1.5.14.bin
        self.software_image_location = (
            f"/Netonix/{self.software_image_name}"  # /Netonix/wispswitch-1.5.14.bin
        )
        self.dry_run = dry_run
        if self.dry_run:
            self.job.logger.info(f"{self.device} Dry run enabled.")
        self.job.logger.info(
            f"{self.device} Selected software version: {self.software_version}"
        )
        self.job.logger.info(
            f"{self.device} Selected software image: {self.software_image_name}"
        )

        self.device_info = get_device_connection_info(self.device)
        self.ftp_server = get_ftp_server_credentials(ftp_server)

        self.firmware_image_path = self.download_firmware_from_ftp()

        self.session = None
        self.model = None  # WS-26-500-DC
        self.firmware_version = None  # 1.5.14
        self.firmware_upgrade_required = None  # True or False

    def connect(self):
        """Establish a connection to the device."""
        try:
            self.job.logger.info(f"{self.device} Connecting to device...")
            self.session = ConnectHandler(**self.device_info)
            self.session.enable()
            self.job.logger.info(f"{self.device} Connection established.")
        except Exception as e:
            self.job.logger.error(f"{self.device} Error connecting to device: {e}")

    def disconnect(self):
        """Disconnect from the device."""
        try:
            self.session.disconnect()
            self.job.logger.info(f"{self.device} Disconnected from device.")
        except Exception as e:
            self.job.logger.error(f"{self.device} Error disconnecting from device: {e}")

    def download_firmware_from_ftp(self):
        PERSISTENT_DIR = "/opt/nautobot/firmware_images/"
        local_path = os.path.join(PERSISTENT_DIR, self.software_image_name)

        if not os.path.exists(local_path):
            try:
                subprocess.run(["mkdir", "-p", PERSISTENT_DIR], check=True)
                # curl -u username:password ftp://10.1.1.1:/Netonix/wispswitch-1.5.17rc2.bin -o /opt/nautobot/firmware_images/wispswitch-1.5.17rc2.bin
                self.job.logger.info(
                    f"{self.device} Downloading {self.software_image_location} to {local_path}..."
                )
                subprocess.run(
                    [
                        "curl",
                        f"-u {self.ftp_server['username']}:{self.ftp_server['password']}",
                        f"ftp://{self.ftp_server['host']}{self.software_image_location}",
                        "-o",
                        local_path,
                    ],
                    check=True,
                )
                self.job.logger.info(
                    f"{self.device} Downloaded {self.software_image_location} to {local_path}"
                )
            except subprocess.CalledProcessError as e:
                self.job.logger.error(f"{self.device} An error occurred: {e}")
        else:
            self.job.logger.info(
                f"{self.device} File {self.software_image_name} already exists in {PERSISTENT_DIR}. Skipping download."
            )
        return local_path

    def upload_firmware_to_remote(self):
        if not isinstance(self.firmware_image_path, str):
            self.job.logger.error(
                f"{self.device} Invalid local_path: expected str, got {type(self.firmware_image_path).__name__}"
            )
            return

        try:
            # sshpass -p 0IoRD2uPdW scp -o StrictHostKeyChecking=no /opt/nautobot/firmware_images/wispswitch-1.5.17rc2.bin admin@172.24.18.30:/tmp
            self.job.logger.info(
                f"{self.device} Uploading {self.firmware_image_path} to device path: /tmp..."
            )
            subprocess.run(
                [
                    "sshpass",
                    "-p",
                    self.device_info["password"],
                    "scp",
                    "-o",
                    "StrictHostKeyChecking=no",
                    self.firmware_image_path,
                    f"{self.device_info['username']}@{self.device_info['ip']}:/tmp",
                ],
                check=True,
            )
            self.job.logger.info(
                f"{self.device} Uploaded {self.firmware_image_path} to device path: /tmp"
            )
        except subprocess.CalledProcessError as e:
            self.job.logger.error(
                f"{self.device} An error occurred during the upload: {e}"
            )

    def validate_software(self):
        """Validate if the software version is correct for the device."""
        pass

    def gather_device_info(self):
        """Gather device information to determine upgrade requirements."""
        self.job.logger.info(f"{self.device} Gathering device information...")
        self.job.logger.info(f"{self.device} Sending command: show status")
        show_status_output = self.session.send_command_timing("show status")
        parsed_status_output = parse_command_output(
            show_status_output, "netonix_os_show_status.textfsm"
        )
        # [
        #     {
        #         "BOARD_REV": "B",
        #         "CPU_USAGE": "22%",
        #         "IPV6_ADDRESS": "fe80::ee13:b2ff:fe12:32ec",
        #         "IP_ADDRESS": "172.26.131.30",
        #         "MAC_ADDRESS": "ec:13:b2:12:32:ec",
        #         "MEMORY_USAGE": "52.11 MB",
        #         "MODEL": "WS-26-400-IDC",
        #         "UPTIME": "11423717.000000",
        #         "VERSION": "1.5.14",
        #     }
        # ]
        self.model = parsed_status_output[0]["MODEL"]
        self.firmware_version = parsed_status_output[0]["VERSION"]

    def determine_upgrade_requirements(self):
        """Determine if a firmware upgrade is required."""
        self.firmware_upgrade_required = self.firmware_version != self.software_version

    def upload_firmware(self):
        """Upload the firmware image to the device."""
        timer = 10
        self.job.logger.info(
            f"{self.device} Uploading firmware image to device... {timer} seconds left"
        )

        if not self.dry_run:
            self.upload_firmware_to_remote()
            time.sleep(timer)

        self.job.logger.info(f"{self.device} Firmware upload complete.")

    def apply_upgrade(self):
        """Apply the firmware upgrade on the device."""
        timer = 600
        self.job.logger.info(f"{self.device} Applying firmware upgrade...")

        cmdline_command = f"cmdline"
        self.job.logger.info(f"{self.device} Sending command: {cmdline_command}")

        if not self.dry_run:
            self.session.send_command_timing(cmdline_command)

        firmware_upgrade_command = f"firmware_upgrade /tmp/{self.software_image_name}"
        self.job.logger.info(
            f"{self.device} Sending command: {firmware_upgrade_command}... {timer} seconds left"
        )

        if not self.dry_run:
            self.session.send_command_timing(firmware_upgrade_command)
            time.sleep(timer)
            self.disconnect()

        self.job.logger.info(f"{self.device} Firmware upgrade applied.")

    def verify_upgrade(self):
        """Verify the firmware upgrade was successful."""
        self.job.logger.info(f"{self.device} Verifying firmware upgrade status...")
        self.connect()
        self.gather_device_info()
        self.determine_upgrade_requirements()
        self.generate_report()
        self.job.logger.info(f"{self.device} Firmware upgrade verification complete.")

    def generate_report(self):
        """Generate a report of the firmware upgrade process."""
        self.job.logger.info(f"{self.device} Model: {self.model}")
        self.job.logger.info(f"{self.device} Firmware version: {self.firmware_version}")
        self.job.logger.info(
            f"{self.device} Firmware upgrade required: {self.firmware_upgrade_required}"
        )
        self.job.logger.info(f"{self.device} ...")

    def upgrade_firmware(self):
        """Full firmware upgrade workflow."""
        try:
            self.connect()
            self.gather_device_info()
            self.determine_upgrade_requirements()
            self.generate_report()
            self.validate_software()
            if self.firmware_upgrade_required:
                self.upload_firmware()
                self.apply_upgrade()
                self.verify_upgrade()
        except Exception as e:
            self.job.logger.error(f"{self.device} Error upgrading firmware: {e}")
        finally:
            self.disconnect()
            self.job.logger.info(f"{self.device} Firmware upgrade process complete.")


class Ubiquiti_AirOS:
    # TODO: Get guide for AirOS 4, 6, and 8
    """
        AirOS Firmware Upgrade Steps:
        https://community.ui.com/questions/CLI-saving-and-restoring-config-and-updating-firmware-/143a9726-1de3-4b44-beb3-6fe14757ae19

    so, AirOS 5 for dummies:

    Upgrading firmware will take three steps:
    1. put new firmware image into /tmp/fwupdate.bin with help of http or tftp client
    2. validate that image with /sbin/fwupdate -c
    3. upgrade with /sbin/fwupdate -m
    the last step will write image to flash, and reboot the device.
    """

    # AirOS Config Management
    """
    for configuration, there are two files:
    /tmp/running.cfg is backup of the running configuration, and /tmp/system.cfg is basically file with "future" config, before "apply".
    How to change config:
    1. make a copy of config file just in case /bin/cp /tmp/system.cfg /tmp/system.cfg.old
    2. change for example SSID to testing123 /bin/sed "s/^wireless.1.ssid=.*$/wireless.1.ssid=testing123/" /tmp/system.cfg.old > /tmp/system.cfg
    3. save all config with /sbin/cfgmtd -p /etc/ -w
    4. wait for few seconds /bin/sleep 5
    5. commit changes /usr/etc/rc.d/rc.softrestart save
    in one line, pasted to admin.cgi, it should look like this:
    /bin/cp /tmp/system.cfg /tmp/system.cfg.old && /bin/sed "s/^wireless.1.ssid=.*$/wireless.1.ssid=testing123/" /tmp/system.cfg.old > /tmp/system.cfg && /sbin/cfgmtd -p /etc/ -w && /bin/sleep 5 && /usr/etc/rc.d/rc.softrestart save
    with basic linux knowledge, all this can be extracted from the SDK. I am strongly suggesting do not use these methods, unless you are absolutely sure, what are you doing.
    I do all my backups, statistics and other things via admin.cgi with PHP scripts. No need to run SSH on every device, when HTTP or HTTPS is running anyway. Both of these protocols is much easy to implement with scripts, than SSH.
    """


register_jobs(FirmwareUpgrade)
