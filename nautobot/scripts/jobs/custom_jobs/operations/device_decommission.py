"""Purpose: Safely decommission selected devices in Nautobot with optional backup and remote interface shutdown."""

import re
from datetime import datetime

from django.conf import settings
from netmiko import ConnectHandler

from nautobot.apps.jobs import (
    BooleanVar,
    IntegerVar,
    Job,
    TextVar,
    register_jobs,
)
from nautobot.dcim.models import Device
from nautobot.extras.models import Status

from custom_jobs.modules.tools import (
    DeviceFormEntry,
    JobLogBuffer,
    JobProxy,
    apply_device_filters,
    get_device_connection_info,
    parallel_execution,
)

from ..configuration.backup_configurations import DeviceBackup

name = "Operations"

DECOMMISSION_STATUS_NAME = "Decommissioning"
ARCHIVED_STATUS_NAME = "Offline"
SHUTDOWN_SUPPORTED_PLATFORMS = {
    "cisco_ios": "show interfaces status",
    "cisco_xe": "show interfaces status",
    "cisco_nxos": "show interface status",
    "arista_eos": "show interfaces status",
}
PROTECTED_INTERFACE_PREFIXES = (
    "lo",
    "loopback",
    "mgmt",
    "management",
    "vlan",
    "tunnel",
    "port-channel",
    "po",
)


class DeviceDecommission(Job, DeviceFormEntry):
    """Decommission selected devices with dry-run-first cleanup and status updates."""

    reason = TextVar(
        description="Why the devices are being decommissioned (ticket, replacement, circuit turn-down, etc.)",
        required=True,
    )
    dry_run = BooleanVar(
        description="Preview all decommission actions without making changes",
        default=True,
        required=False,
    )
    capture_final_backup = BooleanVar(
        description="Capture a final configuration backup before cleanup",
        default=True,
        required=False,
    )
    shutdown_interfaces = BooleanVar(
        description="Remotely shut down non-management interfaces on supported platforms",
        default=False,
        required=False,
    )
    disconnect_cables = BooleanVar(
        description="Delete Nautobot cable records attached to the selected devices",
        default=True,
        required=False,
    )
    clear_interface_ip_assignments = BooleanVar(
        description="Remove IP address assignments from device interfaces in Nautobot",
        default=True,
        required=False,
    )
    clear_primary_ips = BooleanVar(
        description="Clear primary IPv4/IPv6 assignments on the device record",
        default=True,
        required=False,
    )
    remove_from_rack = BooleanVar(
        description="Remove rack and position assignment from the device record",
        default=True,
        required=False,
    )
    update_status = BooleanVar(
        description="Update device status and append a decommission note in comments",
        default=True,
        required=False,
    )
    parallel_task = BooleanVar(
        description="Execute device workflows in parallel",
        default=False,
        required=False,
    )
    max_workers = IntegerVar(
        description="Maximum worker threads when parallel execution is enabled",
        default=10,
        min_value=1,
        max_value=20,
        required=False,
    )

    class Meta:
        name = "Device Decommission"
        description = (
            "Bulk decommission workflow for selected devices: optional final backup, optional "
            "remote interface shutdown, Nautobot cable/IP/rack cleanup, and status/comment updates."
        )
        has_sensitive_variables = False
        soft_time_limit = 1800
        time_limit = 2400
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
        reason="",
        dry_run=True,
        capture_final_backup=True,
        shutdown_interfaces=False,
        disconnect_cables=True,
        clear_interface_ip_assignments=True,
        clear_primary_ips=True,
        remove_from_rack=True,
        update_status=True,
        parallel_task=False,
        max_workers=10,
    ):
        all_devices = apply_device_filters(
            set(),
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
            self.logger.warning("No devices matched the filter criteria.")
            return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
        options = {
            "dry_run": dry_run,
            "capture_final_backup": capture_final_backup,
            "shutdown_interfaces": shutdown_interfaces,
            "disconnect_cables": disconnect_cables,
            "clear_interface_ip_assignments": clear_interface_ip_assignments,
            "clear_primary_ips": clear_primary_ips,
            "remove_from_rack": remove_from_rack,
            "update_status": update_status,
        }

        enabled_actions = [
            label
            for label, enabled in [
                ("backup", capture_final_backup),
                ("remote_shutdown", shutdown_interfaces),
                ("disconnect_cables", disconnect_cables),
                ("clear_interface_ips", clear_interface_ip_assignments),
                ("clear_primary_ips", clear_primary_ips),
                ("remove_from_rack", remove_from_rack),
                ("update_status", update_status),
            ]
            if enabled
        ]
        self.logger.info(
            f"Decommission workflow starting for {len(all_devices)} device(s). Dry run: {dry_run}. "
            f"Actions: {', '.join(enabled_actions) if enabled_actions else 'none'}"
        )

        def _run_device(dev):
            buf = JobLogBuffer()
            try:
                DeviceDecommissionHelper(
                    job=JobProxy(buf),
                    device=dev,
                    reason=reason,
                    timestamp=timestamp,
                    options=options,
                ).run()
            except Exception as exc:
                buf.error(f"{dev} Unhandled decommission error: {exc}")
            return buf

        if parallel_task:
            parallel_execution(
                _run_device,
                all_devices,
                max_workers=max_workers,
                job_logger=self.logger,
            )
        else:
            for dev in all_devices:
                _run_device(dev).drain_to(self.logger)


class DeviceDecommissionHelper:
    """Per-device decommission workflow used by DeviceDecommission."""

    def __init__(self, job, device, reason, timestamp, options):
        self.job = job
        self.device = device
        self.reason = reason.strip()
        self.timestamp = timestamp
        self.options = options

    def run(self):
        self.job.logger.info(f"{self.device} Starting decommission workflow.")

        if self.options["capture_final_backup"]:
            self._capture_backup()

        if self.options["shutdown_interfaces"]:
            self._shutdown_interfaces()

        self._apply_nautobot_cleanup()
        self.job.logger.info(f"{self.device} Decommission workflow complete.")

    def _capture_backup(self):
        if self.options["dry_run"]:
            self.job.logger.info(f"{self.device} DRY RUN: Would capture final backup.")
            return

        self.job.logger.info(f"{self.device} Capturing final backup before cleanup.")
        DeviceBackup(job=self.job, device=self.device).backup_config()

    def _shutdown_interfaces(self):
        platform = getattr(getattr(self.device, "platform", None), "network_driver", None)
        command = SHUTDOWN_SUPPORTED_PLATFORMS.get(platform)
        if not command:
            self.job.logger.info(
                f"{self.device} Remote shutdown not supported for platform '{platform}'. Skipping."
            )
            return

        device_info = get_device_connection_info(self.device)
        if not device_info:
            self.job.logger.warning(
                f"{self.device} No connection information available for remote shutdown."
            )
            return
        device_info["disable_sha2_fix"] = True

        try:
            with ConnectHandler(**device_info) as session:
                session.enable()
                output = session.send_command(command)
                candidates = self._extract_shutdown_candidates(output)

                if not candidates:
                    self.job.logger.info(
                        f"{self.device} No shutdown candidates found on remote device."
                    )
                    return

                self.job.logger.info(
                    f"{self.device} {'DRY RUN: Would shut down' if self.options['dry_run'] else 'Shutting down'} "
                    f"{len(candidates)} interface(s): {candidates[:10]}"
                )

                if self.options["dry_run"]:
                    return

                shutdown_cmds = []
                for interface_name in candidates:
                    shutdown_cmds.extend([f"interface {interface_name}", "shutdown"])
                session.send_config_set(shutdown_cmds)
                self.job.logger.info(
                    f"{self.device} Remote interface shutdown complete for {len(candidates)} interface(s)."
                )
        except Exception as exc:
            self.job.logger.warning(f"{self.device} Remote shutdown failed: {exc}")

    def _extract_shutdown_candidates(self, output):
        candidates = []
        seen = set()
        for name in re.findall(r"^(\S+)\s+", output, re.MULTILINE):
            lowered = name.lower()
            if lowered in {"port", "interface", "name"}:
                continue
            if any(lowered.startswith(prefix) for prefix in PROTECTED_INTERFACE_PREFIXES):
                continue
            if name in seen:
                continue
            seen.add(name)
            candidates.append(name)
        return candidates

    def _apply_nautobot_cleanup(self):
        interfaces = list(self.device.interfaces.prefetch_related("ip_addresses").all())

        cable_count = 0
        ip_assignment_count = 0
        primary_ip_count = 0
        status_name = None
        rack_removed = False

        if self.options["disconnect_cables"]:
            cable_count = self._disconnect_cables(interfaces)

        if self.options["clear_primary_ips"]:
            primary_ip_count = self._clear_primary_ips()

        if self.options["clear_interface_ip_assignments"]:
            ip_assignment_count = self._clear_interface_ip_assignments(interfaces)

        if self.options["remove_from_rack"]:
            rack_removed = self._remove_from_rack()

        if self.options["update_status"]:
            status_name = self._update_status_and_comments()

        if self.options["dry_run"]:
            self.job.logger.info(
                f"{self.device} DRY RUN summary: cables={cable_count}, interface_ip_assignments={ip_assignment_count}, "
                f"primary_ips={primary_ip_count}, rack_removed={rack_removed}, status={status_name or 'unchanged'}"
            )
            return

        self.device.validated_save()
        self.job.logger.info(
            f"{self.device} Nautobot cleanup complete: cables={cable_count}, interface_ip_assignments={ip_assignment_count}, "
            f"primary_ips={primary_ip_count}, rack_removed={rack_removed}, status={status_name or 'unchanged'}"
        )

    def _disconnect_cables(self, interfaces):
        seen = set()
        cables = []
        for interface in interfaces:
            cable = getattr(interface, "cable", None)
            if cable is None or cable.pk in seen:
                continue
            seen.add(cable.pk)
            cables.append(cable)

        if not cables:
            return 0

        if self.options["dry_run"]:
            self.job.logger.info(
                f"{self.device} DRY RUN: Would delete {len(cables)} cable record(s)."
            )
            return len(cables)

        for cable in cables:
            cable.delete()
        self.job.logger.info(f"{self.device} Deleted {len(cables)} cable record(s).")
        return len(cables)

    def _clear_primary_ips(self):
        count = 0
        if self.device.primary_ip4:
            count += 1
        if self.device.primary_ip6:
            count += 1
        if count == 0:
            return 0

        if self.options["dry_run"]:
            self.job.logger.info(
                f"{self.device} DRY RUN: Would clear {count} primary IP assignment(s)."
            )
            return count

        self.device.primary_ip4 = None
        self.device.primary_ip6 = None
        self.job.logger.info(f"{self.device} Cleared {count} primary IP assignment(s).")
        return count

    def _clear_interface_ip_assignments(self, interfaces):
        assignments = []
        for interface in interfaces:
            for ip_address in interface.ip_addresses.all():
                assignments.append((interface, ip_address))

        if not assignments:
            return 0

        if self.options["dry_run"]:
            self.job.logger.info(
                f"{self.device} DRY RUN: Would remove {len(assignments)} interface IP assignment(s)."
            )
            return len(assignments)

        for interface, ip_address in assignments:
            interface.ip_addresses.remove(ip_address)
        self.job.logger.info(
            f"{self.device} Removed {len(assignments)} interface IP assignment(s)."
        )
        return len(assignments)

    def _remove_from_rack(self):
        has_rack_assignment = self.device.rack is not None or self.device.position is not None
        if not has_rack_assignment:
            return False

        if self.options["dry_run"]:
            self.job.logger.info(
                f"{self.device} DRY RUN: Would remove rack assignment {self.device.rack} at position {self.device.position}."
            )
            return True

        self.device.rack = None
        self.device.position = None
        self.job.logger.info(f"{self.device} Removed rack and position assignment.")
        return True

    def _update_status_and_comments(self):
        device_statuses = Status.objects.get_for_model(Device)
        target_status = (
            device_statuses.filter(name=ARCHIVED_STATUS_NAME).first()
            or device_statuses.filter(name=DECOMMISSION_STATUS_NAME).first()
        )

        status_name = None
        if target_status is not None:
            status_name = target_status.name
            if self.options["dry_run"]:
                self.job.logger.info(
                    f"{self.device} DRY RUN: Would update status to '{target_status.name}'."
                )
            else:
                self.device.status = target_status
                self.job.logger.info(
                    f"{self.device} Status set to '{target_status.name}'."
                )
        else:
            self.job.logger.warning(
                f"{self.device} Neither '{ARCHIVED_STATUS_NAME}' nor '{DECOMMISSION_STATUS_NAME}' exists for Device status."
            )

        note = f"[{self.timestamp}] DECOMMISSIONED: {self.reason}"
        if self.options["dry_run"]:
            self.job.logger.info(
                f"{self.device} DRY RUN: Would append decommission note to comments."
            )
            return status_name

        current_comments = self.device.comments or ""
        self.device.comments = f"{current_comments}\n{note}".strip()
        self.job.logger.info(f"{self.device} Decommission note appended to comments.")
        return status_name


register_jobs(DeviceDecommission)