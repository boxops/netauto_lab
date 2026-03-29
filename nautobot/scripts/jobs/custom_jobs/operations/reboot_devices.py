"""
Purpose: Capture device configurations and save them as a Git controlled backup in Nautobot.
"""

from netmiko import ConnectHandler
from django.conf import settings

from nautobot.apps.jobs import register_jobs, Job, ChoiceVar, ObjectVar, BooleanVar

from custom_jobs.modules.tools import get_device_connection_info
from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry

name = "Operations"

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
    "arista_eos",
]


class CustomDeviceReboot(Job, DeviceFormEntry):

    # # TODO: Implement save_config
    # save_config = BooleanVar(
    #     description="Save the device configuration before rebooting?",
    #     default=True,
    # )

    class Meta:
        name = "Reboot Devices"
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
        # save_config=True,  # TODO: Implement save_config
    ):
        # all_devices = set()

        # all_devices = apply_device_filters(
        #     tenant_group=tenant_group,
        #     tenant=tenant,
        #     location=location,
        #     rack_group=rack_group,
        #     rack=rack,
        #     role=role,
        #     manufacturer=manufacturer,
        #     platform=platform,
        #     device_type=device_type,
        #     tags=tags,
        #     status=status,
        # )
        # if device:
        #     all_devices.update(device)

        all_devices = set()

        # Apply additional filters if any are provided
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

        self.logger.info(f"Found {len(all_devices)} devices after filtering")

        if not all_devices:
            self.logger.warning("No devices found matching the criteria")
            return

        for device in all_devices:
            try:
                if device.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"Platform {device.platform.network_driver} is not supported. Skipping..."
                    )
                    continue
                self.logger.info(f"Processing device: {device}")
                task = DeviceReboot(self, device)
                task.reboot()
            except Exception as e:
                self.logger.error(f"Error processing device {device}: {e}")


class DeviceReboot:
    def __init__(self, job, device):
        self.job = job
        self.device = device

        self.device_info = get_device_connection_info(self.device)
        self.session = None

    def connect(self):
        """Establish a connection to the device."""
        self.job.logger.info(f"{self.device} Connecting to device...")
        self.session = ConnectHandler(**self.device_info)
        self.session.enable()
        self.job.logger.info(f"{self.device} Connection established.")

    def disconnect(self):
        """Disconnect from the device."""
        self.session.disconnect()
        self.job.logger.info(f"{self.device} Disconnected from device.")

    def _keymile_nos(self):
        """Reboot a Keymile NOS device."""
        self.job.logger.info(f"{self.device} Sending command: reload")
        self.session.send_command_timing("reload")
        self.session.send_command_timing("y")
        self.session.send_command_timing("y")

    def _fiberstore_fsos(self):
        """Reboot a Fiberstore FSOS device."""
        self.job.logger.info(f"{self.device} Sending command: reboot")
        self.session.send_command_timing("reboot")
        self.session.send_command_timing("\n")

    def _mikrotik_routeros(self):
        """Reboot a Mikrotik RouterOS device."""
        self.job.logger.info(f"{self.device} Sending command: /system reboot")
        self.session.send_command_timing("/system reboot")
        self.session.send_command_timing("\n")

    def _netonix_os(self):
        """Reboot a Netonix OS device."""
        self.job.logger.info(f"{self.device} Sending command: reload cold")
        self.session.send_command_timing("reload cold")
        self.session.send_command_timing("\n")

    def _cisco_ios(self):
        """Reboot a Cisco IOS device."""
        self.job.logger.info(f"{self.device} Sending command: reload")
        self.session.send_command_timing("reload")
        self.session.send_command_timing("\n")

    def _cisco_xr(self):
        """Reboot a Cisco XR device."""
        self.job.logger.info(f"{self.device} Sending command: admin reload")
        self.session.send_command_timing("admin reload")
        self.session.send_command_timing("yes")

    def _cisco_xe(self):
        """Reboot a Cisco XE device."""
        self.job.logger.info(f"{self.device} Sending command: reload")
        self.session.send_command_timing("reload")
        self.session.send_command_timing("\n")
        self.session.send_command_timing("\n")

    def _cisco_nxos(self):
        """Reboot a Cisco NX-OS device."""
        self.job.logger.info(f"{self.device} Sending command: reload")
        self.session.send_command_timing("reload")
        self.session.send_command_timing("y")
        self.session.send_command_timing("y")

    def _cisco_s300(self):
        """Reboot a Cisco Small Business device."""
        self.job.logger.info(f"{self.device} Sending command: reload")
        self.session.send_command_timing("reload")
        self.session.send_command_timing("y")

    def _ubiquiti_airos(self):
        """Reboot a Ubiquiti AirOS device."""
        self.job.logger.info(f"{self.device} Sending command: reboot")
        self.session.send_command_timing("reboot")

    def reboot(self):
        """Reboot the device."""
        try:
            self.connect()
            platform = self.device.platform.network_driver
            if platform == "keymile_nos":
                self._keymile_nos()
            elif platform == "fiberstore_fsos":
                self._fiberstore_fsos()
            elif platform == "mikrotik_routeros":
                self._mikrotik_routeros()
            elif platform == "netonix_os":
                self._netonix_os()
            elif platform == "cisco_ios":
                self._cisco_ios()
            elif platform == "cisco_xr":
                self._cisco_xr()
            elif platform == "cisco_xe":
                self._cisco_xe()
            elif platform == "cisco_nxos":
                self._cisco_nxos()
            elif platform == "cisco_s300":
                self._cisco_s300()
            elif platform == "ubiquiti_airos":
                self._ubiquiti_airos()
            else:
                self.job.logger.error(
                    f"{self.device} Platform {platform} is not supported."
                )
            self.job.logger.info(f"{self.device} Rebooted device...")
        except Exception as e:
            self.job.logger.error(f"{self.device} Failed to connect to device: {e}")
        finally:
            self.disconnect()


register_jobs(CustomDeviceReboot)
