"""Purpose: Validate NTP server configuration on devices against a policy defined in Nautobot."""

from netmiko import ConnectHandler

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, IntegerVar, StringVar

from custom_jobs.modules.tools import (
    get_device_connection_info,
    apply_device_filters,
    DeviceFormEntry,
    parallel_execution,
)

name = "Configuration"

SUPPORTED_PLATFORMS = [
    "cisco_ios",
    "cisco_xe",
    "cisco_xr",
    "cisco_nxos",
    "arista_eos",
    "fiberstore_fsos",
    "keymile_nos",
    "mikrotik_routeros",
    "fortinet",
]


class NTPComplianceCheck(Job, DeviceFormEntry):
    """
    SSH to devices, extract configured NTP servers, and compare them against
    required NTP servers supplied at run time. Non-compliant devices are logged
    with details and a CSV summary is exported.
    """

    required_ntp_servers = StringVar(
        description="Comma-separated list of required NTP server IPs or hostnames",
        required=True,
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
        name = "NTP Compliance Check"
        description = (
            "Verify NTP servers are correctly configured across all devices. "
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
        required_ntp_servers="",
        parallel_task=False,
        max_workers=10,
    ):
        required = {s.strip() for s in required_ntp_servers.split(",") if s.strip()}
        if not required:
            self.logger.error("No required NTP servers provided.")
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

        results = []

        def check_device(dev):
            try:
                if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{dev} platform {dev.platform.network_driver} not supported, skipping."
                    )
                    return
                task = NTPChecker(job=self, device=dev, required_servers=required)
                result = task.run()
                results.append(result)
            except Exception as exc:
                self.logger.error(f"{dev} Error: {exc}")

        if parallel_task:
            parallel_execution(check_device, all_devices, max_workers=max_workers)
        else:
            for dev in all_devices:
                check_device(dev)

        compliant = sum(1 for r in results if r.get("compliant"))
        non_compliant = len(results) - compliant
        self.logger.info(
            f"NTP Compliance Summary: {compliant} compliant, {non_compliant} non-compliant "
            f"out of {len(results)} devices checked."
        )

        import csv
        import io

        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=["device", "compliant", "configured_servers", "missing_servers"],
        )
        writer.writeheader()
        writer.writerows(results)
        self.create_file("ntp_compliance_report.csv", output.getvalue())


class NTPChecker:
    NTP_COMMANDS = {
        "cisco_ios": "show run | include ntp server",
        "cisco_xe": "show run | include ntp server",
        "cisco_xr": "show run | include ntp server",
        "cisco_nxos": "show run | include ntp server",
        "arista_eos": "show run | grep ntp",
        "fiberstore_fsos": "show run | include ntp",
        "keymile_nos": "show run | include ntp",
        "mikrotik_routeros": "/system ntp client print",
        "fortinet": "show system ntp",
    }

    def __init__(self, job, device, required_servers):
        self.job = job
        self.device = device
        self.required_servers = required_servers

    def run(self):
        platform = self.device.platform.network_driver
        command = self.NTP_COMMANDS.get(platform, "show run | include ntp")

        device_info = get_device_connection_info(self.device)
        try:
            with ConnectHandler(**device_info) as session:
                session.enable()
                output = session.send_command(command)
        except Exception as exc:
            self.job.logger.error(f"{self.device} Connection error: {exc}")
            return {
                "device": self.device.name,
                "compliant": False,
                "configured_servers": "",
                "missing_servers": str(self.required_servers),
            }

        configured = set()
        for line in output.splitlines():
            line = line.strip()
            parts = line.split()
            # Extract IP / hostname from lines like "ntp server 10.0.0.1" etc.
            for part in parts:
                if part.replace(".", "").isdigit() or (
                    "." in part and not part.startswith("ntp")
                ):
                    configured.add(part)

        missing = self.required_servers - configured
        compliant = not missing

        if compliant:
            self.job.logger.info(f"{self.device} NTP COMPLIANT. Servers: {configured}")
        else:
            self.job.logger.warning(
                f"{self.device} NTP NON-COMPLIANT. Configured: {configured}, Missing: {missing}"
            )

        return {
            "device": self.device.name,
            "compliant": compliant,
            "configured_servers": ";".join(sorted(configured)),
            "missing_servers": ";".join(sorted(missing)),
        }


register_jobs(NTPComplianceCheck)
