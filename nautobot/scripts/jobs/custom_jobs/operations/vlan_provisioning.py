"""Purpose: Provision VLANs across a switch domain, push configuration to devices, and record them in Nautobot."""

from netmiko import ConnectHandler

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, IntegerVar, StringVar, ObjectVar
from nautobot.ipam.models import VLAN, VLANGroup
from nautobot.dcim.models import Location
from nautobot.extras.models import Status

from custom_jobs.modules.tools import (
    get_device_connection_info,
    apply_device_filters,
    DeviceFormEntry,
    parallel_execution,
)

name = "Operations"

SUPPORTED_PLATFORMS = [
    "cisco_ios",
    "cisco_xe",
    "cisco_nxos",
    "arista_eos",
    "fiberstore_fsos",
]


class VLANProvisioning(Job, DeviceFormEntry):
    """
    Create a VLAN in Nautobot and push its configuration to selected access/distribution
    switches. Supports dry-run mode to preview the change before committing.
    """

    vlan_id = IntegerVar(
        description="VLAN ID to provision (1-4094)",
        min_value=1,
        max_value=4094,
        required=True,
    )
    vlan_name = StringVar(
        description="VLAN name / description",
        required=True,
    )
    dry_run = BooleanVar(
        description="Preview configuration without pushing to devices or saving to Nautobot",
        default=True,
        required=False,
    )
    parallel_task = BooleanVar(
        description="Execute tasks in parallel",
        default=False,
        required=False,
    )
    max_workers = IntegerVar(
        description="Number of parallel workers",
        default=10,
        min_value=1,
        max_value=20,
        required=False,
    )

    class Meta:
        name = "VLAN Provisioning"
        description = (
            "Create a VLAN in Nautobot and push its configuration to selected switches. "
            f"Supported platforms: {SUPPORTED_PLATFORMS}"
        )
        has_sensitive_variables = False
        soft_time_limit = 1800
        time_limit = 2400
        task_queues = ["default", "priority", "bulk"]

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
        vlan_id=None,
        vlan_name="",
        dry_run=True,
        parallel_task=False,
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

        if not all_devices:
            self.logger.error("No devices selected.")
            return

        # Create VLAN record in Nautobot
        if not dry_run:
            self._create_nautobot_vlan(vlan_id, vlan_name, location)

        def push_vlan(dev):
            try:
                if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{dev} platform {dev.platform.network_driver} not supported, skipping."
                    )
                    return
                task = VLANPusher(job=self, device=dev, vlan_id=vlan_id, vlan_name=vlan_name, dry_run=dry_run)
                task.run()
            except Exception as exc:
                self.logger.error(f"{dev} Error: {exc}")

        if parallel_task:
            parallel_execution(push_vlan, all_devices, max_workers=max_workers)
        else:
            for dev in all_devices:
                push_vlan(dev)

    def _create_nautobot_vlan(self, vlan_id, vlan_name, location):
        try:
            active_status = Status.objects.get(name="Active")
            existing = VLAN.objects.filter(vid=vlan_id).first()
            if existing:
                self.logger.info(f"VLAN {vlan_id} ({vlan_name}) already exists in Nautobot.")
                return
            vlan = VLAN(
                vid=vlan_id,
                name=vlan_name,
                status=active_status,
            )
            if location:
                vlan.location = location
            vlan.validated_save()
            self.logger.info(f"Created VLAN {vlan_id} ({vlan_name}) in Nautobot.")
        except Exception as exc:
            self.logger.error(f"Failed to create VLAN in Nautobot: {exc}")


class VLANPusher:
    VLAN_COMMANDS = {
        "cisco_ios": lambda vid, name: [f"vlan {vid}", f"name {name}", "exit"],
        "cisco_xe": lambda vid, name: [f"vlan {vid}", f"name {name}", "exit"],
        "cisco_nxos": lambda vid, name: [f"vlan {vid}", f"name {name}", "exit"],
        "arista_eos": lambda vid, name: [f"vlan {vid}", f"name {name}", "exit"],
        "fiberstore_fsos": lambda vid, name: [f"vlan {vid}", f"name {name}", "exit"],
    }

    def __init__(self, job, device, vlan_id, vlan_name, dry_run):
        self.job = job
        self.device = device
        self.vlan_id = vlan_id
        self.vlan_name = vlan_name
        self.dry_run = dry_run

    def run(self):
        platform = self.device.platform.network_driver
        cmd_factory = self.VLAN_COMMANDS.get(platform)
        if not cmd_factory:
            self.job.logger.warning(
                f"{self.device} No VLAN command template for {platform}."
            )
            return

        commands = cmd_factory(self.vlan_id, self.vlan_name)
        self.job.logger.info(
            f"{self.device} {'[DRY RUN] Would push' if self.dry_run else 'Pushing'} "
            f"VLAN {self.vlan_id} ({self.vlan_name}). Commands: {commands}"
        )

        if self.dry_run:
            return

        device_info = get_device_connection_info(self.device)
        try:
            with ConnectHandler(**device_info) as session:
                session.enable()
                output = session.send_config_set(commands)
                session.save_config()
            self.job.logger.info(
                f"{self.device} VLAN {self.vlan_id} provisioned successfully. Output: {output}"
            )
        except Exception as exc:
            self.job.logger.error(f"{self.device} Failed to push VLAN: {exc}")


register_jobs(VLANProvisioning)
