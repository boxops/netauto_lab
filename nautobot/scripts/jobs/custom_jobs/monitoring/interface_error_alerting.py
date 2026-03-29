"""Purpose: Poll devices for interface error counters and alert when rates exceed thresholds."""

from netmiko import ConnectHandler
import threading

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, IntegerVar

from custom_jobs.modules.tools import (
    get_device_connection_info,
    apply_device_filters,
    DeviceFormEntry,
    parse_command_output,
    parallel_execution,
)

name = "Monitoring"

SUPPORTED_PLATFORMS = [
    "cisco_ios",
    "cisco_xe",
    "cisco_xr",
    "cisco_nxos",
    "arista_eos",
    "fiberstore_fsos",
]


class InterfaceErrorAlerting(Job, DeviceFormEntry):
    """
    SSH to devices, pull interface error counters (CRC, input errors, output drops),
    and flag interfaces that exceed configured thresholds. Adds a Nautobot note on
    the device and exports a CSV report.
    """

    crc_threshold = IntegerVar(
        description="Maximum allowed CRC errors before alerting",
        default=100,
        min_value=0,
        max_value=1000000,
        required=False,
    )
    input_error_threshold = IntegerVar(
        description="Maximum allowed input errors before alerting",
        default=500,
        min_value=0,
        max_value=1000000,
        required=False,
    )
    output_drop_threshold = IntegerVar(
        description="Maximum allowed output drops before alerting",
        default=500,
        min_value=0,
        max_value=1000000,
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
        name = "Interface Error Rate Alerting"
        description = (
            "Poll devices for CRC, input error, and output drop counters. "
            f"Alert when thresholds are exceeded. Supported platforms: {SUPPORTED_PLATFORMS}"
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
        crc_threshold=100,
        input_error_threshold=500,
        output_drop_threshold=500,
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

        all_alerts = []
        all_alerts_lock = threading.Lock()

        def check_device(dev):
            try:
                if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{dev} platform {dev.platform.network_driver} not supported, skipping."
                    )
                    return
                task = ErrorChecker(
                    job=self,
                    device=dev,
                    crc_threshold=crc_threshold,
                    input_error_threshold=input_error_threshold,
                    output_drop_threshold=output_drop_threshold,
                )
                alerts = task.run()
                with all_alerts_lock:
                    all_alerts.extend(alerts)
            except Exception as exc:
                self.logger.error(f"{dev} Error: {exc}")

        if parallel_task:
            parallel_execution(check_device, all_devices, max_workers=max_workers)
        else:
            for dev in all_devices:
                check_device(dev)

        self.logger.info(
            f"Interface Error Check: {len(all_alerts)} alert(s) raised across {len(all_devices)} devices."
        )

        if all_alerts:
            import csv
            import io

            output = io.StringIO()
            writer = csv.DictWriter(
                output,
                fieldnames=["device", "interface", "crc_errors", "input_errors", "output_drops", "alert"],
            )
            writer.writeheader()
            writer.writerows(all_alerts)
            self.create_file("interface_error_alerts.csv", output.getvalue())


class ErrorChecker:
    COMMANDS = {
        "cisco_ios": "show interfaces",
        "cisco_xe": "show interfaces",
        "cisco_xr": "show interfaces",
        "cisco_nxos": "show interface",
        "arista_eos": "show interfaces",
        "fiberstore_fsos": "show interface",
    }

    def __init__(self, job, device, crc_threshold, input_error_threshold, output_drop_threshold):
        self.job = job
        self.device = device
        self.crc_threshold = crc_threshold
        self.input_error_threshold = input_error_threshold
        self.output_drop_threshold = output_drop_threshold

    def _parse_counter(self, text, keyword):
        """Extract the first integer before or after a keyword in a line."""
        import re
        for line in text.splitlines():
            if keyword.lower() in line.lower():
                numbers = re.findall(r"\d+", line)
                if numbers:
                    return int(numbers[0])
        return 0

    def run(self):
        platform = self.device.platform.network_driver
        command = self.COMMANDS.get(platform, "show interfaces")

        device_info = get_device_connection_info(self.device)
        try:
            with ConnectHandler(**device_info) as session:
                session.enable()
                output = session.send_command(command)
        except Exception as exc:
            self.job.logger.error(f"{self.device} Connection error: {exc}")
            return []

        # Split output per interface block
        import re
        interface_blocks = re.split(r"(?=^\S)", output, flags=re.MULTILINE)
        alerts = []

        for block in interface_blocks:
            lines = block.strip().splitlines()
            if not lines:
                continue
            intf_name_match = re.match(r"^(\S+)\s+is\s+", lines[0])
            if not intf_name_match:
                continue
            intf_name = intf_name_match.group(1)

            crc = self._parse_counter(block, "CRC")
            input_errors = self._parse_counter(block, "input errors")
            output_drops = self._parse_counter(block, "output drops")

            alert_msgs = []
            if crc > self.crc_threshold:
                alert_msgs.append(f"CRC={crc} > threshold {self.crc_threshold}")
            if input_errors > self.input_error_threshold:
                alert_msgs.append(f"input_errors={input_errors} > threshold {self.input_error_threshold}")
            if output_drops > self.output_drop_threshold:
                alert_msgs.append(f"output_drops={output_drops} > threshold {self.output_drop_threshold}")

            if alert_msgs:
                alert_str = "; ".join(alert_msgs)
                self.job.logger.warning(
                    f"{self.device} Interface {intf_name}: {alert_str}"
                )
                alerts.append({
                    "device": self.device.name,
                    "interface": intf_name,
                    "crc_errors": crc,
                    "input_errors": input_errors,
                    "output_drops": output_drops,
                    "alert": alert_str,
                })

        return alerts


register_jobs(InterfaceErrorAlerting)
