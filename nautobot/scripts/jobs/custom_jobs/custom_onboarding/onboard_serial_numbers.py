"""
Purpose: Onboard serial numbers from actual devices to Nautobot.
"""

from django.conf import settings
from netmiko import ConnectHandler
import json

from nautobot.apps.jobs import Job, BooleanVar, register_jobs, IntegerVar

from custom_jobs.modules.tools import get_device_connection_info
from custom_jobs.modules.tools import parse_command_output
from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import convert_flat_config_to_dict
from custom_jobs.modules.tools import parallel_execution

name = "Custom Onboarding"

SUPPORTED_PLATFORMS = [
    "keymile_nos",
    "fiberstore_fsos",
    "mikrotik_routeros",
    # "netonix_os", # No serial number
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
    "arista_eos",
]


class OnboardSerialNumbers(Job, DeviceFormEntry):
    skip_devices_with_serial = BooleanVar(default=False, required=False)
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
        name = "Onboard Device Serial Numbers"
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
        skip_devices_with_serial=None,
        parallel_task=None,
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
                        
        if skip_devices_with_serial:
            all_devices = {device for device in all_devices if not device.serial}

        def onboard_serial(device):
            try:
                if device.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"Platform {device.platform.network_driver} is not supported. Skipping..."
                    )
                    return
                self.logger.info(f"Processing device: {device}")
                task = OnboardSerial(self, device)
                task.onboard()
            except Exception as e:
                self.logger.error(f"Error processing device {device}: {e}")

        if parallel_task:
            parallel_execution(onboard_serial, all_devices, max_workers=max_workers)
        else:
            for device in all_devices:
                onboard_serial(device)


class OnboardSerial:
    def __init__(self, job, device):
        self.job = job
        self.device = device
        self.serial_number = None

    def _get_serial_number(self, session, command, template):
        output = session.send_command_timing(command)
        if self.device.platform.network_driver == "cambium_cnmatrix":
            session.send_command_timing("q")
        parsed_output = parse_command_output(output, template)

        if not parsed_output:
            self.job.logger.warning(
                f"{self.device} No serial number data found in command output. Raw output: {output[:500]}"
            )
            return None

        if "SERIAL_NUMBER" not in parsed_output[0]:
            self.job.logger.warning(
                f"{self.device} SERIAL_NUMBER field not found in parsed output: {parsed_output[0]}"
            )
            return None

        if isinstance(parsed_output[0]["SERIAL_NUMBER"], list):
            if len(parsed_output[0]["SERIAL_NUMBER"]) > 0:
                return parsed_output[0]["SERIAL_NUMBER"][0]
            else:
                self.job.logger.warning(f"{self.device} SERIAL_NUMBER list is empty")
                return None
        return parsed_output[0]["SERIAL_NUMBER"]

    def _ubiquiti_airos(self, session):
        output = session.send_command_timing("cat /etc/board.info")
        parsed_output = convert_flat_config_to_dict(output)

        if "board.hwaddr" not in parsed_output:
            self.job.logger.warning(
                f"{self.device} board.hwaddr not found in parsed output: {parsed_output}"
            )
            return None

        return parsed_output["board.hwaddr"]

    def onboard(self):
        platform_commands = {
            "keymile_nos": ("show system", "keymile_nos_show_system.textfsm"),
            "fiberstore_fsos": ("show version", "fiberstore_fsos_show_version.textfsm"),
            "mikrotik_routeros": (
                "/system routerboard print",
                "mikrotik_routeros_system_routerboard_print.textfsm",
            ),
            # "netonix_os": ("show status", "netonix_os_show_status.textfsm"), # No serial number
            "cisco_ios": ("show version", "cisco_ios_show_version.textfsm"),
            "cisco_xr": ("show inventory", "cisco_xr_show_inventory.textfsm"),
            "cisco_xe": ("show inventory", "cisco_xe_show_inventory.textfsm"),
            "cisco_nxos": ("show inventory", "cisco_nxos_show_inventory.textfsm"),
            "cisco_s300": ("show inventory", "cisco_s300_show_inventory.textfsm"),
            "ubiquiti_edge": (
                "show version",
                "ubiquiti_edge_show_version.textfsm",
            ),
            "ubiquiti_edgeswitch": (
                "show version",
                "ubiquiti_edgeswitch_show_version.textfsm",
            ),
            "ceragon_os": (
                "platform management inventory show info",
                "ceragon_os_show_info.textfsm",
            ),
            "siklu_os": (
                "show inventory component 1 serial-num",
                "siklu_os_show_serial.textfsm",
            ),
            "cambium_cnmatrix": (
                "show system information",
                "cambium_cnmatrix_show_system.textfsm",
            ),
            "arista_eos": ("show version", "arista_eos_show_version.textfsm"),
        }

        try:
            device_info = get_device_connection_info(self.device)
            with ConnectHandler(**device_info) as session:
                self.job.logger.info(f"{self.device} Device info: {device_info}")
                session.enable()
                platform = self.device.platform.network_driver
                if platform == "ubiquiti_airos":
                    self.serial_number = self._ubiquiti_airos(session)
                elif platform in platform_commands:
                    command, template = platform_commands[platform]
                    self.serial_number = self._get_serial_number(
                        session, command, template
                    )
                else:
                    raise Exception(f"Platform {platform} is not supported")
            if self.serial_number:
                self.job.logger.info(
                    f"{self.device} Extracted serial number from device: {self.serial_number}"
                )
                self.assign_serial_number()
            else:
                self.job.logger.info(
                    f"{self.device} Serial number was not extracted from device"
                )
        except Exception as e:
            self.job.logger.error(f"{self.device} Error processing device: {e}")

    def assign_serial_number(self):
        self.device.serial = self.serial_number
        self.device.validated_save()
        self.job.logger.info(
            f"{self.device} Assigned serial number to device on Nautobot: {self.serial_number}"
        )


register_jobs(OnboardSerialNumbers)
