"""Purpose: ICMP + SNMP reachability sweep across all devices; update device status in Nautobot."""

import shutil
import subprocess
import threading

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, IntegerVar
from nautobot.extras.models import Status
from nautobot.dcim.models import Device

from custom_jobs.modules.tools import apply_device_filters, DeviceFormEntry, parallel_execution, JobLogBuffer

name = "Monitoring"


class ReachabilitySweep(Job, DeviceFormEntry):
    """
    ICMP ping all selected devices. Devices that fail ICMP are additionally marked
    in the job log. Optionally update the Nautobot device Status field to reflect
    reachability (Active / Offline).
    """

    update_status = BooleanVar(
        description="Update device status in Nautobot based on reachability result",
        default=False,
        required=False,
    )
    ping_count = IntegerVar(
        description="Number of ICMP echo requests per device",
        default=3,
        min_value=1,
        max_value=10,
        required=False,
    )
    parallel_task = BooleanVar(
        description="Execute tasks in parallel",
        default=True,
        required=False,
    )
    max_workers = IntegerVar(
        description="Number of parallel workers",
        default=20,
        min_value=1,
        max_value=50,
        required=False,
    )

    class Meta:
        name = "Reachability Sweep"
        description = (
            "ICMP ping sweep across selected devices and optionally update Nautobot status. "
            "Use to quickly identify unreachable devices before a maintenance window."
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
        update_status=False,
        ping_count=3,
        parallel_task=True,
        max_workers=20,
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
            # Fall back to all devices with a primary IP
            all_devices = list(Device.objects.filter(primary_ip4__isnull=False).iterator(chunk_size=200))

        reachable = []
        unreachable = []
        results_lock = threading.Lock()

        active_status = Status.objects.filter(name="Active").first()
        offline_status = Status.objects.filter(name="Offline").first()

        def sweep_device(dev):
            buf = JobLogBuffer()
            if not dev.primary_ip4:
                buf.warning(f"{dev} No primary IP, skipping.")
                return buf
            ip = dev.primary_ip4.host
            ping_bin = shutil.which("ping") or "/bin/ping"
            try:
                result = subprocess.run(
                    [ping_bin, "-c", str(ping_count), "-W", "2", ip],
                    capture_output=True,
                    timeout=ping_count * 3,
                )
                is_up = result.returncode == 0
            except subprocess.TimeoutExpired:
                is_up = False

            if is_up:
                buf.info(f"{dev} ({ip}) REACHABLE")
                with results_lock:
                    reachable.append(dev.name)
                if update_status and active_status and dev.status != active_status:
                    dev.status = active_status
                    dev.validated_save()
            else:
                buf.warning(f"{dev} ({ip}) UNREACHABLE")
                with results_lock:
                    unreachable.append(dev.name)
                if update_status and offline_status:
                    dev.status = offline_status
                    dev.validated_save()
            return buf

        if parallel_task:
            parallel_execution(sweep_device, all_devices, max_workers=max_workers, job_logger=self.logger)
        else:
            for dev in all_devices:
                sweep_device(dev)

        self.logger.info(
            f"Reachability Sweep complete: {len(reachable)} reachable, {len(unreachable)} unreachable "
            f"out of {len(all_devices)} devices."
        )

        summary = (
            f"Reachable ({len(reachable)}):\n" + "\n".join(sorted(reachable)) +
            f"\n\nUnreachable ({len(unreachable)}):\n" + "\n".join(sorted(unreachable))
        )
        self.create_file("reachability_sweep_report.txt", summary)


register_jobs(ReachabilitySweep)
