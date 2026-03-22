"""
Purpose: Onboard software versions from actual devices to Nautobot.
"""

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from netmiko import ConnectHandler

from nautobot.apps.jobs import register_jobs, Job, BooleanVar, IntegerVar
from nautobot.extras.models import Relationship, RelationshipAssociation
from nautobot_device_lifecycle_mgmt.models import SoftwareLCM
from nautobot.extras.models.secrets import (
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)

from custom_jobs.modules.tools import get_device_connection_info
from custom_jobs.modules.tools import parse_command_output
from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import parallel_execution
from custom_jobs.backends.airosapi import AirOS8API


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
    "arista_eos",
]


class GetShowVersion(Job, DeviceFormEntry):
    """Job to onboard software versions from devices to Nautobot."""

    skip_devices_with_software = BooleanVar(default=False, required=False)
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
        name = "Onboard Device Software Versions"
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
        skip_devices_with_software=False,
        parallel_task=True,
        max_workers=10,
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

        if skip_devices_with_software:
            all_devices = self.filter_skip_devices_with_software(all_devices)

        def onboard_version(device):
            try:
                if device.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"Platform {device.platform.network_driver} is not supported. Skipping..."
                    )
                    return
                self.logger.info(f"Processing device: {device}")
                task = OnboardVersion(self, device)
                task.onboard()
            except Exception as e:
                self.logger.error(f"Error processing device {device}: {e}")

        if parallel_task:
            parallel_execution(onboard_version, all_devices, max_workers=max_workers)
        else:
            for device in all_devices:
                onboard_version(device)

    def filter_skip_devices_with_software(self, devices):
        """Filter devices that already have software versions onboarded."""
        software_rel = Relationship.objects.get(label="Software on Device")
        # Get all device IDs with relationships in one query
        device_ids_with_software = set(
            RelationshipAssociation.objects.filter(
                relationship=software_rel, destination_id__in=[d.id for d in devices]
            ).values_list("destination_id", flat=True)
        )
        return {d for d in devices if d.id not in device_ids_with_software}

    # def filter_skip_devices_with_software(self, devices):
    #     """Filter devices that already have software versions onboarded."""
    #     software_rel = Relationship.objects.get(label="Software on Device")
    #     filtered_devices = set()
    #     for device in devices:
    #         if not RelationshipAssociation.objects.filter(
    #             relationship=software_rel,
    #             destination_id=device.id,
    #         ).exists():
    #             filtered_devices.add(device)
    #     return filtered_devices


class OnboardVersion:
    """Onboard software version from device to Nautobot."""

    def __init__(self, job, device):
        self.job = job
        self.device = device
        self.device_software_version = None
        self.nautobot_software = None

    def _get_software_version(self, session, command, template):
        """Get software version from device."""
        self.job.logger.info(f"{self.device} Sending command: {command}")
        output = session.send_command_timing(command)
        if self.device.platform.network_driver == "cambium_cnmatrix":
            session.send_command_timing("q")
        parsed_output = parse_command_output(output, template)

        if isinstance(parsed_output[0]["VERSION"], list):
            return parsed_output[0]["VERSION"][0]
        return parsed_output[0]["VERSION"]

    def onboard(self):
        """Onboard software version from device to Nautobot."""
        platform_commands = {
            "keymile_nos": ("show system", "keymile_nos_show_system.textfsm"),
            "fiberstore_fsos": ("show version", "fiberstore_fsos_show_version.textfsm"),
            "mikrotik_routeros": (
                "/system routerboard print",
                "mikrotik_routeros_system_routerboard_print.textfsm",
            ),
            "netonix_os": ("show status", "netonix_os_show_status.textfsm"),
            "cisco_ios": ("show version", "cisco_ios_show_version.textfsm"),
            "cisco_xr": ("show version", "cisco_xr_show_version.textfsm"),
            "cisco_xe": ("show version", "cisco_xe_show_version.textfsm"),
            "cisco_nxos": ("show version", "cisco_nxos_show_version.textfsm"),
            "cisco_s300": ("show version", "cisco_s300_show_version.textfsm"),
            "ubiquiti_airos": (
                "cat /etc/version",
                "ubiquiti_airos_show_version.textfsm",
            ),
            "ubiquiti_edge": ("show version", "ubiquiti_edge_show_version.textfsm"),
            "ubiquiti_edgeswitch": (
                "show version",
                "ubiquiti_edgeswitch_show_version.textfsm",
            ),
            "ceragon_os": (
                "platform software show versions",
                "ceragon_os_show_versions.textfsm",
            ),
            "siklu_os": (
                "show inventory component 1 software-rev",
                "siklu_os_show_version.textfsm",
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
                command, template = platform_commands[platform]
                self.device_software_version = self._get_software_version(
                    session, command, template
                )
                self.job.logger.info(
                    f"{self.device} Software version: {self.device_software_version}"
                )
            if self.device_software_version:
                self.import_to_nautobot()
                self.assign_to_device()
            else:
                self.job.logger.info(
                    f"{self.device} Software version not found. Skipping..."
                )
        except Exception as e:
            self.job.logger.error(f"{self.device} Error processing device: {e}")

    def import_to_nautobot(self):
        """Create a new software version in Nautobot."""
        self.nautobot_software, created = SoftwareLCM.objects.get_or_create(
            version=self.device_software_version,
            defaults={"device_platform": self.device.platform},
        )
        if created:
            self.job.logger.info(
                f"{self.device} Created software version {self.nautobot_software}"
            )
        else:
            self.job.logger.info(
                f"{self.device} Software version {self.nautobot_software} exists"
            )

    def assign_to_device(self):
        """Assign the software version to the device."""
        software_rel = Relationship.objects.get(label="Software on Device")

        existing_association = RelationshipAssociation.objects.filter(
            relationship=software_rel,
            destination_id=self.device.id,
        ).first()
        if existing_association:
            existing_association.delete()
            self.job.logger.info(
                f"{self.device} Removed existing relationship for device"
            )

        source_ct = ContentType.objects.get(model="softwarelcm")
        dest_ct = ContentType.objects.get(model="device")
        RelationshipAssociation.objects.create(
            relationship=software_rel,
            source_type=source_ct,
            source=self.nautobot_software,
            destination_type=dest_ct,
            destination=self.device,
        )
        self.job.logger.info(
            f"{self.device} Created relationship {self.device} <-> {self.nautobot_software}"
        )


register_jobs(GetShowVersion)
