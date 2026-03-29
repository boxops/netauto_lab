"""Purpose: Set devices to maintenance state in Nautobot, suppress monitoring alerts, and restore after window."""

from datetime import datetime

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, IntegerVar, StringVar, ObjectVar
from nautobot.extras.models import Status
from nautobot.dcim.models import Device

from custom_jobs.modules.tools import apply_device_filters, DeviceFormEntry

name = "Operations"

MAINTENANCE_STATUS_NAME = "Maintenance"
ACTIVE_STATUS_NAME = "Active"


class MaintenanceWindow(Job, DeviceFormEntry):
    """
    Place selected devices into a Maintenance status in Nautobot, optionally
    add a maintenance note, then restore them to Active status when done.
    When used with a monitoring integration (e.g. Prometheus AlertManager
    silence rules or SolarWinds), an external webhook can suppress alerts
    while devices are in Maintenance state.
    """

    action = StringVar(
        description="Action to perform: 'start' sets status to Maintenance, 'end' restores to Active",
        default="start",
        required=True,
    )
    maintenance_note = StringVar(
        description="Reason / change ticket reference for this maintenance window",
        required=False,
    )

    class Meta:
        name = "Maintenance Window"
        description = (
            "Place devices into Maintenance status in Nautobot (start) or restore them "
            "to Active (end). Use with your monitoring system to suppress alerts."
        )
        has_sensitive_variables = False
        soft_time_limit = 600
        time_limit = 900
        task_queues = ["default", "priority"]

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
        action="start",
        maintenance_note="",
    ):
        if action not in ("start", "end"):
            self.logger.error("Action must be 'start' or 'end'.")
            return

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
            self.logger.warning("No devices selected.")
            return

        target_status_name = MAINTENANCE_STATUS_NAME if action == "start" else ACTIVE_STATUS_NAME
        target_status = Status.objects.filter(name=target_status_name).first()

        if not target_status:
            self.logger.error(
                f"Status '{target_status_name}' not found in Nautobot. "
                "Create it under Extras > Statuses."
            )
            return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
        changed = 0

        for dev in all_devices:
            try:
                old_status = dev.status.name if dev.status else "Unknown"
                dev.status = target_status
                if maintenance_note:
                    current_comments = dev.comments or ""
                    dev.comments = (
                        f"{current_comments}\n[{timestamp}] Maintenance window {action}: {maintenance_note}"
                    ).strip()
                dev.validated_save()
                self.logger.info(
                    f"{dev} Status changed: {old_status} -> {target_status_name}"
                )
                changed += 1
            except Exception as exc:
                self.logger.error(f"{dev} Failed to update status: {exc}")

        self.logger.info(
            f"Maintenance window '{action}' complete: {changed}/{len(all_devices)} devices updated "
            f"to '{target_status_name}'."
        )


register_jobs(MaintenanceWindow)
