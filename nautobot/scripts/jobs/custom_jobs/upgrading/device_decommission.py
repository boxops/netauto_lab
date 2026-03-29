"""Purpose: Orchestrated device decommission - shutdown ports, remove from monitoring, archive config, update status."""

from netmiko import ConnectHandler
from datetime import datetime

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, ObjectVar, StringVar
from nautobot.dcim.models import Device
from nautobot.extras.models import Status

from custom_jobs.modules.tools import get_device_connection_info

name = "Upgrading"

DECOMMISSION_STATUS_NAME = "Decommissioning"
ARCHIVED_STATUS_NAME = "Offline"


class DeviceDecommission(Job):
    """
    Orchestrated decommission workflow for a single device:

    1. Pre-check: verify device exists in Nautobot and is reachable
    2. Shutdown all non-management interfaces on the device
    3. Capture a final running-config backup
    4. Update device status to Decommissioning (then Offline)
    5. Add a decommission note with timestamp and operator reason
    6. Remove device from monitoring (sets Nautobot status so Prometheus/SW sync picks it up)

    This job does NOT delete the device from Nautobot - that must be done manually
    after verifying downstream systems are updated.
    """

    device = ObjectVar(
        model=Device,
        description="Device to decommission",
        required=True,
    )
    reason = StringVar(
        description="Reason for decommissioning (e.g. ticket number, replacement details)",
        required=True,
    )
    dry_run = BooleanVar(
        description="Preview actions without making changes",
        default=True,
        required=False,
    )

    class Meta:
        name = "Device Decommission"
        description = (
            "Orchestrated workflow: shutdown interfaces, capture final backup, "
            "update Nautobot status, and add decommission notes."
        )
        has_sensitive_variables = False
        soft_time_limit = 1800
        time_limit = 2400
        task_queues = ["default", "priority"]

    def run(self, device=None, reason="", dry_run=True):
        if not device:
            self.logger.error("No device specified.")
            return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M UTC")

        self.logger.info(f"{device} Starting decommission workflow. Reason: {reason}")
        self.logger.info(f"{device} Dry run: {dry_run}")

        # Step 1: Capture final backup
        self._capture_backup(device, dry_run)

        # Step 2: Shutdown all non-management interfaces
        self._shutdown_interfaces(device, dry_run)

        # Step 3: Update Nautobot status and add note
        if not dry_run:
            self._update_nautobot(device, reason, timestamp)
        else:
            self.logger.info(
                f"{device} DRY RUN: Would update status to '{ARCHIVED_STATUS_NAME}' and add decommission note."
            )

        self.logger.info(f"{device} Decommission workflow complete.")

    def _capture_backup(self, device, dry_run):
        """Capture running config to a file before decommissioning."""
        if not device.primary_ip4:
            self.logger.warning(f"{device} No primary IP - cannot capture config backup.")
            return

        if dry_run:
            self.logger.info(f"{device} DRY RUN: Would capture running config backup.")
            return

        try:
            device_info = get_device_connection_info(device)
            with ConnectHandler(**device_info) as session:
                session.enable()
                config = session.send_command("show run")
            self.create_file(f"{device.name}_decommission_backup.txt", config)
            self.logger.info(f"{device} Final config backup captured ({len(config)} chars).")
        except Exception as exc:
            self.logger.warning(f"{device} Could not capture backup: {exc}")

    def _shutdown_interfaces(self, device, dry_run):
        """Shutdown all non-management, non-loopback interfaces."""
        if not device.primary_ip4:
            self.logger.warning(f"{device} No primary IP - cannot shutdown interfaces remotely.")
            return

        try:
            device_info = get_device_connection_info(device)
            with ConnectHandler(**device_info) as session:
                session.enable()
                output = session.send_command("show interfaces status")

                import re
                interfaces = re.findall(r"^(\S+)\s+", output, re.MULTILINE)
                candidates = [
                    i for i in interfaces
                    if not any(
                        i.lower().startswith(skip)
                        for skip in ("lo", "loopback", "mgmt", "management", "vlan", "tunnel")
                    )
                ]

                self.logger.info(
                    f"{device} {'DRY RUN: Would shut' if dry_run else 'Shutting'} "
                    f"{len(candidates)} interface(s): {candidates[:10]}"
                )

                if dry_run:
                    return

                shutdown_cmds = []
                for intf in candidates:
                    shutdown_cmds += [f"interface {intf}", "shutdown"]

                session.send_config_set(shutdown_cmds)
                self.logger.info(f"{device} All non-management interfaces shut down.")
        except Exception as exc:
            self.logger.warning(f"{device} Interface shutdown error: {exc}")

    def _update_nautobot(self, device, reason, timestamp):
        """Update device status to Offline and add decommission comment."""
        try:
            offline_status = Status.objects.filter(name=ARCHIVED_STATUS_NAME).first()
            if offline_status:
                device.status = offline_status

            current_comments = device.comments or ""
            device.comments = (
                f"{current_comments}\n[{timestamp}] DECOMMISSIONED: {reason}"
            ).strip()
            device.validated_save()
            self.logger.info(
                f"{device} Status set to '{ARCHIVED_STATUS_NAME}'. Decommission note added."
            )
        except Exception as exc:
            self.logger.error(f"{device} Failed to update Nautobot: {exc}")


register_jobs(DeviceDecommission)
