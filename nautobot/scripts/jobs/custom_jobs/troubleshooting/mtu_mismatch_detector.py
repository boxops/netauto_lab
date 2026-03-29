"""Purpose: Detect MTU mismatches across point-to-point links using LLDP neighbor data."""

import threading
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


class MTUMismatchDetector(Job, DeviceFormEntry):
    """
    Collect MTU values from device interfaces, then cross-check against LLDP
    neighbor data to detect mismatched MTU values across point-to-point links.
    Exports a CSV of all detected mismatches.
    """

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
        name = "MTU Mismatch Detector"
        description = (
            "Detect MTU inconsistencies across point-to-point links using LLDP neighbor data. "
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

        # device_name -> interface_name -> mtu
        mtu_map = {}
        # device_name -> list of (local_intf, remote_dev, remote_intf)
        lldp_map = {}
        maps_lock = threading.Lock()

        def collect_mtu(dev):
            try:
                if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    return
                task = MTUCollector(job=self, device=dev)
                # Collect both MTU and LLDP in a single SSH session
                mtu_data = task.run()
                lldp_data = task.get_lldp_neighbors()
                with maps_lock:
                    mtu_map[dev.name] = mtu_data
                    lldp_map[dev.name] = lldp_data
            except Exception as exc:
                self.logger.error(f"{dev} Error collecting MTU/LLDP: {exc}")

        if parallel_task:
            parallel_execution(collect_mtu, all_devices, max_workers=max_workers)
        else:
            for dev in all_devices:
                collect_mtu(dev)

        mismatches = []

        for dev in all_devices:
            if dev.name not in mtu_map:
                continue
            try:
                neighbors = lldp_map.get(dev.name, [])
                for local_intf, remote_dev, remote_intf in neighbors:
                    local_mtu = mtu_map.get(dev.name, {}).get(local_intf)
                    remote_mtu = mtu_map.get(remote_dev, {}).get(remote_intf)
                    if local_mtu and remote_mtu and local_mtu != remote_mtu:
                        self.logger.warning(
                            f"MTU MISMATCH: {dev.name}:{local_intf} MTU={local_mtu} <-> "
                            f"{remote_dev}:{remote_intf} MTU={remote_mtu}"
                        )
                        mismatches.append({
                            "local_device": dev.name,
                            "local_interface": local_intf,
                            "local_mtu": local_mtu,
                            "remote_device": remote_dev,
                            "remote_interface": remote_intf,
                            "remote_mtu": remote_mtu,
                        })
            except Exception as exc:
                self.logger.error(f"{dev} Error checking mismatches: {exc}")

        if mismatches:
            import csv
            import io

            output = io.StringIO()
            writer = csv.DictWriter(
                output,
                fieldnames=[
                    "local_device", "local_interface", "local_mtu",
                    "remote_device", "remote_interface", "remote_mtu",
                ],
            )
            writer.writeheader()
            writer.writerows(mismatches)
            self.create_file("mtu_mismatch_report.csv", output.getvalue())
            self.logger.info(f"Found {len(mismatches)} MTU mismatch(es).")
        else:
            self.logger.info("No MTU mismatches detected.")


class MTUCollector:
    MTU_COMMANDS = {
        "cisco_ios": "show interfaces",
        "cisco_xe": "show interfaces",
        "cisco_xr": "show interfaces",
        "cisco_nxos": "show interface",
        "arista_eos": "show interfaces",
    }
    LLDP_COMMANDS = {
        "cisco_ios": "show lldp neighbors detail",
        "cisco_xe": "show lldp neighbors detail",
        "cisco_xr": "show lldp neighbors detail",
        "cisco_nxos": "show lldp neighbors detail",
        "arista_eos": "show lldp neighbors detail",
    }

    def __init__(self, job, device):
        self.job = job
        self.device = device
        self._session_output = {}

    def _connect_and_run(self, commands):
        device_info = get_device_connection_info(self.device)
        results = {}
        with ConnectHandler(**device_info) as session:
            session.enable()
            for cmd in commands:
                results[cmd] = session.send_command(cmd)
        return results

    def _fetch_both(self):
        """Open a single SSH session and run both the MTU and LLDP commands.
        Results are cached in self._session_output to avoid a second connection."""
        if self._session_output:
            return
        platform = self.device.platform.network_driver
        commands = []
        mtu_cmd = self.MTU_COMMANDS.get(platform)
        lldp_cmd = self.LLDP_COMMANDS.get(platform)
        if mtu_cmd:
            commands.append(mtu_cmd)
        if lldp_cmd:
            commands.append(lldp_cmd)
        if commands:
            self._session_output = self._connect_and_run(commands)

    def run(self):
        """Returns dict: interface_name -> MTU value (int)."""
        import re
        platform = self.device.platform.network_driver
        command = self.MTU_COMMANDS.get(platform)
        if not command:
            return {}

        try:
            self._fetch_both()
            output = self._session_output.get(command, "")
        except Exception as exc:
            self.job.logger.error(f"{self.device} MTU collection error: {exc}")
            return {}

        mtu_data = {}
        current_intf = None
        for line in output.splitlines():
            intf_match = re.match(r"^(\S+)\s+is\s+", line)
            if intf_match:
                current_intf = intf_match.group(1)
            mtu_match = re.search(r"MTU\s+(\d+)", line, re.IGNORECASE)
            if mtu_match and current_intf:
                mtu_data[current_intf] = int(mtu_match.group(1))
        return mtu_data

    def get_lldp_neighbors(self):
        """Returns list of (local_intf, remote_device, remote_intf) tuples."""
        import re
        platform = self.device.platform.network_driver
        command = self.LLDP_COMMANDS.get(platform)
        if not command:
            return []

        try:
            self._fetch_both()
            output = self._session_output.get(command, "")
        except Exception as exc:
            self.job.logger.error(f"{self.device} LLDP collection error: {exc}")
            return []

        neighbors = []
        local_intf = remote_dev = remote_intf = None
        for line in output.splitlines():
            local_match = re.search(r"Local Intf:\s+(\S+)", line, re.IGNORECASE)
            if local_match:
                local_intf = local_match.group(1)
            remote_dev_match = re.search(r"System Name:\s+(\S+)", line, re.IGNORECASE)
            if remote_dev_match:
                remote_dev = remote_dev_match.group(1)
            remote_intf_match = re.search(r"Port id:\s+(\S+)", line, re.IGNORECASE)
            if remote_intf_match:
                remote_intf = remote_intf_match.group(1)
            if local_intf and remote_dev and remote_intf:
                neighbors.append((local_intf, remote_dev, remote_intf))
                local_intf = remote_dev = remote_intf = None
        return neighbors


register_jobs(MTUMismatchDetector)
