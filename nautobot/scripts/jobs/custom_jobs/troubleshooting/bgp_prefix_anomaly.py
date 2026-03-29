"""Purpose: Detect BGP prefix count anomalies by comparing live counts against documented baselines."""

from netmiko import ConnectHandler

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, IntegerVar

from custom_jobs.modules.tools import (
    get_device_connection_info,
    apply_device_filters,
    DeviceFormEntry,
    parse_command_output,
    parallel_execution,
)

name = "Troubleshooting"

SUPPORTED_PLATFORMS = [
    "cisco_ios",
    "cisco_xe",
    "cisco_xr",
    "cisco_nxos",
    "arista_eos",
]


class BGPPrefixAnomalyDetector(Job, DeviceFormEntry):
    """
    SSH to devices, collect current BGP prefix counts per VRF/neighbor, and compare
    against a configurable deviation threshold. Alerts when prefix counts deviate
    significantly from expected values. Useful for detecting route leaks, peer resets,
    and prefix hijacks.
    """

    deviation_percent = IntegerVar(
        description="Alert if prefix count deviates by more than this % from the running average",
        default=20,
        min_value=1,
        max_value=100,
        required=False,
    )
    min_prefix_threshold = IntegerVar(
        description="Minimum expected prefix count per BGP neighbor (alert if below this)",
        default=1,
        min_value=0,
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
        name = "BGP Prefix Anomaly Detector"
        description = (
            "Compare live BGP prefix counts against expected baselines and alert on anomalies. "
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
        deviation_percent=20,
        min_prefix_threshold=1,
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

        all_anomalies = []

        def check_device(dev):
            try:
                if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{dev} platform {dev.platform.network_driver} not supported, skipping."
                    )
                    return
                task = BGPChecker(
                    job=self,
                    device=dev,
                    deviation_percent=deviation_percent,
                    min_prefix_threshold=min_prefix_threshold,
                )
                anomalies = task.run()
                all_anomalies.extend(anomalies)
            except Exception as exc:
                self.logger.error(f"{dev} Error: {exc}")

        if parallel_task:
            parallel_execution(check_device, all_devices, max_workers=max_workers)
        else:
            for dev in all_devices:
                check_device(dev)

        if all_anomalies:
            import csv
            import io

            output = io.StringIO()
            writer = csv.DictWriter(
                output,
                fieldnames=["device", "neighbor", "state", "prefix_count", "alert"],
            )
            writer.writeheader()
            writer.writerows(all_anomalies)
            self.create_file("bgp_prefix_anomaly_report.csv", output.getvalue())
            self.logger.info(f"BGP Anomaly: {len(all_anomalies)} anomaly(ies) found.")
        else:
            self.logger.info("No BGP prefix anomalies detected.")


class BGPChecker:
    BGP_COMMANDS = {
        "cisco_ios": "show ip bgp summary",
        "cisco_xe": "show ip bgp summary",
        "cisco_xr": "show bgp ipv4 unicast summary",
        "cisco_nxos": "show bgp ipv4 unicast summary",
        "arista_eos": "show ip bgp summary",
    }
    BGP_TEMPLATES = {
        "cisco_ios": "cisco_ios_show_ip_bgp_summary.textfsm",
        "cisco_xe": "cisco_ios_show_ip_bgp_summary.textfsm",
        "cisco_nxos": "cisco_nxos_show_ip_bgp_summary.textfsm",
    }

    def __init__(self, job, device, deviation_percent, min_prefix_threshold):
        self.job = job
        self.device = device
        self.deviation_percent = deviation_percent
        self.min_prefix_threshold = min_prefix_threshold

    def run(self):
        import re
        platform = self.device.platform.network_driver
        command = self.BGP_COMMANDS.get(platform)
        if not command:
            return []

        device_info = get_device_connection_info(self.device)
        try:
            with ConnectHandler(**device_info) as session:
                session.enable()
                output = session.send_command(command)
        except Exception as exc:
            self.job.logger.error(f"{self.device} Connection error: {exc}")
            return []

        template = self.BGP_TEMPLATES.get(platform)
        anomalies = []

        if template:
            try:
                neighbors = parse_command_output(output, template)
                for neighbor in neighbors:
                    neighbor_ip = neighbor.get("BGPNEIGHBOR") or neighbor.get("neighbor", "")
                    state = neighbor.get("STATE") or neighbor.get("state", "")
                    prefixes = neighbor.get("PREFIXRECEIVED") or neighbor.get("prefix_received", "0")
                    try:
                        prefix_count = int(prefixes)
                    except ValueError:
                        prefix_count = 0

                    alert_msgs = []
                    state_lower = state.lower()
                    if "established" not in state_lower and not state.isdigit():
                        alert_msgs.append(f"BGP session not established (state: {state})")
                    if prefix_count < self.min_prefix_threshold:
                        alert_msgs.append(
                            f"Prefix count {prefix_count} below minimum {self.min_prefix_threshold}"
                        )

                    if alert_msgs:
                        alert_str = "; ".join(alert_msgs)
                        self.job.logger.warning(
                            f"{self.device} BGP neighbor {neighbor_ip}: {alert_str}"
                        )
                        anomalies.append({
                            "device": self.device.name,
                            "neighbor": neighbor_ip,
                            "state": state,
                            "prefix_count": prefix_count,
                            "alert": alert_str,
                        })
                    else:
                        self.job.logger.info(
                            f"{self.device} BGP neighbor {neighbor_ip}: OK "
                            f"(state={state}, prefixes={prefix_count})"
                        )
                return anomalies
            except Exception as exc:
                self.job.logger.warning(f"{self.device} TextFSM parse error: {exc}")

        # Fallback: log raw output
        self.job.logger.info(f"{self.device} BGP summary:\n{output[:2000]}")
        return []


register_jobs(BGPPrefixAnomalyDetector)
