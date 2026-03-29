"""Purpose: Roll back a batch of devices to their last known-good backup configuration."""

from netmiko import ConnectHandler
from datetime import datetime
import os

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, IntegerVar
from nautobot_golden_config.models import GoldenConfig
from nautobot.core.utils.data import render_jinja2
from nautobot.extras.models.groups import DynamicGroup

from custom_jobs.modules.tools import (
    get_device_connection_info,
    apply_device_filters,
    DeviceFormEntry,
    parallel_execution,
)
from custom_jobs.modules.git import gc_repos

name = "Orchestration"

SUPPORTED_PLATFORMS = [
    "cisco_ios",
    "cisco_xe",
    "cisco_xr",
    "cisco_nxos",
    "arista_eos",
    "fiberstore_fsos",
    "keymile_nos",
    "fortinet",
]

PLATFORM_RESTORE_COMMANDS = {
    "cisco_ios": lambda config: config,
    "cisco_xe": lambda config: config,
    "cisco_xr": lambda config: config,
    "cisco_nxos": lambda config: config,
    "arista_eos": lambda config: config,
    "fiberstore_fsos": lambda config: config,
    "keymile_nos": lambda config: config,
    "fortinet": lambda config: config,
}


class MassRollback(Job, DeviceFormEntry):
    """
    Roll back a batch of devices to their most recent Golden Config backup.
    Pre-requisite: backup_configurations job must have been run and golden config
    backup records must exist for the selected devices.

    Workflow:
    1. Verify backup record exists for each device
    2. Confirm the backup content is non-empty
    3. Push the backup config to the device (config replace / send_config_set)
    4. Log success/failure per device
    """

    dry_run = BooleanVar(
        description="Preview which devices would be rolled back without pushing configs",
        default=True,
        required=False,
    )
    parallel_task = BooleanVar(
        description="Execute rollbacks in parallel",
        default=False,
        required=False,
    )
    max_workers = IntegerVar(
        description="Number of parallel workers",
        default=5,
        min_value=1,
        max_value=10,
        required=False,
    )

    class Meta:
        name = "Mass Rollback"
        description = (
            "Roll back devices to their last known-good Golden Config backup. "
            f"Supported platforms: {SUPPORTED_PLATFORMS}"
        )
        has_sensitive_variables = False
        soft_time_limit = 3600
        time_limit = 7200
        task_queues = ["default", "priority"]

    @gc_repos
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
        dry_run=True,
        parallel_task=False,
        max_workers=5,
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

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
        self.logger.info(
            f"Mass Rollback initiated at {timestamp} | "
            f"{len(all_devices)} devices | Dry run: {dry_run}"
        )

        def rollback_device(dev):
            try:
                if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{dev} platform {dev.platform.network_driver} not supported, skipping."
                    )
                    return
                task = DeviceRollback(job=self, device=dev, dry_run=dry_run)
                task.run()
            except Exception as exc:
                self.logger.error(f"{dev} Error: {exc}")

        if parallel_task:
            parallel_execution(rollback_device, all_devices, max_workers=max_workers)
        else:
            for dev in all_devices:
                rollback_device(dev)


class DeviceRollback:
    def __init__(self, job, device, dry_run):
        self.job = job
        self.device = device
        self.dry_run = dry_run

    def run(self):
        # Find the Golden Config backup record
        gc_record = GoldenConfig.objects.filter(device=self.device).first()
        if not gc_record or not gc_record.backup_config:
            self.job.logger.warning(
                f"{self.device} No backup found in Golden Config records. Skipping."
            )
            return

        backup_age = None
        if gc_record.backup_last_success_date:
            from django.utils import timezone
            delta = timezone.now() - gc_record.backup_last_success_date
            backup_age = delta.days

        self.job.logger.info(
            f"{self.device} Backup found "
            f"(last success: {gc_record.backup_last_success_date}, {backup_age} day(s) old). "
            f"Config length: {len(gc_record.backup_config)} chars."
        )

        if self.dry_run:
            self.job.logger.info(
                f"{self.device} DRY RUN: Would push {len(gc_record.backup_config)} char backup config."
            )
            return

        try:
            device_info = get_device_connection_info(self.device)
            config_lines = [
                line for line in gc_record.backup_config.splitlines()
                if line.strip() and not line.strip().startswith("!")
            ][:500]  # Safety: limit to first 500 lines per rollback push

            with ConnectHandler(**device_info) as session:
                session.enable()
                output = session.send_config_set(config_lines)
                session.save_config()

            self.job.create_file(f"{self.device.name}_rollback_output.txt", output)
            self.job.logger.info(
                f"{self.device} Rollback complete. {len(config_lines)} config lines pushed."
            )
        except Exception as exc:
            self.job.logger.error(f"{self.device} Rollback failed: {exc}")


register_jobs(MassRollback)
