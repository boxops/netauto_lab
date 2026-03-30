"""Purpose: SSH to network devices and run a timed packet capture via the device Linux shell."""

import threading
from datetime import datetime

from netmiko import ConnectHandler

from nautobot.apps.jobs import Job, register_jobs, StringVar, IntegerVar, BooleanVar

from custom_jobs.modules.tools import (
    get_device_connection_info,
    apply_device_filters,
    DeviceFormEntry,
    parallel_execution,
    JobLogBuffer,
)

name = "Troubleshooting"

SUPPORTED_PLATFORMS = ["arista_eos", "cisco_xr"]

# {interface}, {duration}, and {filter} are substituted at runtime.
# {filter} is either empty or starts with a space (e.g. " tcp port 22").
CAPTURE_COMMANDS = {
    # Arista EOS: enter bash and run tcpdump for exactly {duration} seconds.
    "arista_eos": "bash timeout {duration} tcpdump -i {interface} -n{filter} -c 50000",
    # Cisco IOS-XR: run tcpdump via the embedded Linux shell.
    "cisco_xr": "run timeout {duration} tcpdump -i {interface} -n{filter}",
}


class PacketCapture(Job, DeviceFormEntry):
    """
    SSH to selected devices and run a timed packet capture via the device's Linux shell.

    Each device produces a text file attachment containing the captured packet
    summaries (tcpdump -n output).

    Supported platforms: arista_eos, cisco_xr
    """

    interface = StringVar(
        description=(
            "Linux kernel interface name to capture on. "
            "For Arista cEOS use 'eth0' (management) or 'eth1'/'eth2' (data). "
            "For Cisco XR use the Linux interface name (e.g. 'MgmtEth0_RP0_CPU0_0')."
        ),
        label="Interface",
        default="eth0",
    )
    duration = IntegerVar(
        description="Capture duration in seconds",
        label="Duration (seconds)",
        default=10,
        min_value=1,
        max_value=120,
    )
    capture_filter = StringVar(
        description="Optional tcpdump BPF filter (e.g. 'tcp port 22'). Leave blank to capture all traffic.",
        label="BPF Capture Filter",
        required=False,
        default="",
    )
    parallel_task = BooleanVar(
        description="Run captures on all devices in parallel",
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
        name = "Packet Capture"
        description = (
            "SSH to network devices and run a timed tcpdump packet capture via the "
            f"device's Linux shell. Supported platforms: {SUPPORTED_PLATFORMS}"
        )
        has_sensitive_variables = False
        soft_time_limit = 3600
        time_limit = 4800
        task_queues = ["priority", "bulk"]

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
        interface="eth0",
        duration=10,
        capture_filter="",
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

        if not all_devices:
            self.logger.warning("No devices matched the selected filters.")
            return

        captures = {}
        captures_lock = threading.Lock()
        filter_str = f" {capture_filter}" if capture_filter else ""

        def capture_device(dev):
            buf = JobLogBuffer()
            drv = (dev.platform.network_driver if dev.platform else "").lower()
            if drv not in SUPPORTED_PLATFORMS:
                buf.warning(f"{dev} platform '{drv}' not supported, skipping.")
                return buf

            cmd = CAPTURE_COMMANDS[drv].format(
                interface=interface,
                duration=duration,
                filter=filter_str,
            )
            buf.info(f"{dev} Starting {duration}s capture on {interface}" +
                     (f" [filter: {capture_filter}]" if capture_filter else "") + " ...")
            try:
                device_info = get_device_connection_info(dev)
                with ConnectHandler(**device_info) as session:
                    session.enable()
                    output = session.send_command(
                        cmd,
                        read_timeout=duration + 60,
                    )
                with captures_lock:
                    captures[dev.name] = output
                buf.info(f"{dev} Capture complete — {len(output.splitlines())} lines.")
            except Exception as exc:
                buf.error(f"{dev} Capture failed: {exc}")
            return buf

        if parallel_task:
            parallel_execution(
                capture_device, all_devices,
                max_workers=max_workers,
                job_logger=self.logger,
            )
        else:
            for dev in all_devices:
                buf = capture_device(dev)
                if buf:
                    buf.drain_to(self.logger)

        for dev_name, output in captures.items():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"capture_{dev_name}_{interface.replace('/', '_')}_{ts}.txt"
            self.create_file(filename, output)
            self.logger.info(f"Attached capture file: {filename}")

        self.logger.info(
            f"Packet capture complete. {len(captures)}/{len(all_devices)} device(s) captured."
        )


register_jobs(PacketCapture)
