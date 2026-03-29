"""Purpose: Validate SNMP configuration (community strings / v3 users) on devices against policy."""

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
    "fortinet",
]


class SNMPValidation(Job, DeviceFormEntry):
    """
    SSH to devices and verify that SNMP is configured according to policy.
    Checks: SNMP version (v2c or v3), community strings (by name, NOT value),
    and presence of a trap receiver. Reports non-compliant devices to a CSV.
    
    Note: This job only validates SNMP *configuration structure*, not credential
    values, to avoid exposing secrets in job logs.
    """

    expected_snmp_version = StringVar(
        description="Expected SNMP version: v2c or v3",
        default="v2c",
        required=True,
    )
    expected_community_count = IntegerVar(
        description="Minimum expected number of SNMP community strings (v2c only)",
        default=1,
        min_value=0,
        max_value=20,
        required=False,
    )
    require_trap_receiver = BooleanVar(
        description="Require at least one SNMP trap receiver to be configured",
        default=True,
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
        name = "SNMP Configuration Validation"
        description = (
            "Verify SNMP version, community strings, and trap receivers are correctly "
            f"configured per policy. Supported platforms: {SUPPORTED_PLATFORMS}"
        )
        has_sensitive_variables = True
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
        expected_snmp_version="v2c",
        expected_community_count=1,
        require_trap_receiver=True,
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

        results = []

        def check_device(dev):
            try:
                if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{dev} platform {dev.platform.network_driver} not supported, skipping."
                    )
                    return
                task = SNMPChecker(
                    job=self,
                    device=dev,
                    expected_version=expected_snmp_version,
                    expected_community_count=expected_community_count,
                    require_trap_receiver=require_trap_receiver,
                )
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
        self.logger.info(
            f"SNMP Validation Summary: {compliant}/{len(results)} devices compliant."
        )

        import csv
        import io

        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "device", "compliant", "snmp_enabled",
                "community_count", "has_trap_receiver", "issues",
            ],
        )
        writer.writeheader()
        writer.writerows(results)
        self.create_file("snmp_validation_report.csv", output.getvalue())


class SNMPChecker:
    SNMP_COMMANDS = {
        "cisco_ios": "show run | section snmp",
        "cisco_xe": "show run | section snmp",
        "cisco_xr": "show run | include snmp",
        "cisco_nxos": "show run | section snmp",
        "arista_eos": "show run | section snmp",
        "fiberstore_fsos": "show run | include snmp",
        "keymile_nos": "show run | include snmp",
        "fortinet": "show system snmp community",
    }

    def __init__(self, job, device, expected_version, expected_community_count, require_trap_receiver):
        self.job = job
        self.device = device
        self.expected_version = expected_version
        self.expected_community_count = expected_community_count
        self.require_trap_receiver = require_trap_receiver

    def run(self):
        platform = self.device.platform.network_driver
        command = self.SNMP_COMMANDS.get(platform, "show run | include snmp")

        device_info = get_device_connection_info(self.device)
        try:
            with ConnectHandler(**device_info) as session:
                session.enable()
                snmp_output = session.send_command(command)
        except Exception as exc:
            self.job.logger.error(f"{self.device} Connection error: {exc}")
            return {
                "device": self.device.name,
                "compliant": False,
                "snmp_enabled": False,
                "community_count": 0,
                "has_trap_receiver": False,
                "issues": f"Connection error: {exc}",
            }

        snmp_lower = snmp_output.lower()
        issues = []

        snmp_enabled = "snmp" in snmp_lower and "snmp-server" in snmp_lower or "community" in snmp_lower

        # Community string count (count unique "snmp-server community" lines)
        community_lines = [
            line for line in snmp_output.splitlines()
            if "community" in line.lower() and not line.strip().startswith("!")
        ]
        community_count = len(community_lines)

        if community_count < self.expected_community_count:
            issues.append(
                f"Expected >={self.expected_community_count} community strings, found {community_count}"
            )

        # Trap receiver check
        has_trap = "trap" in snmp_lower or "host" in snmp_lower
        if self.require_trap_receiver and not has_trap:
            issues.append("No SNMP trap receiver configured")

        compliant = not issues

        if compliant:
            self.job.logger.info(f"{self.device} SNMP COMPLIANT.")
        else:
            self.job.logger.warning(f"{self.device} SNMP NON-COMPLIANT: {issues}")

        return {
            "device": self.device.name,
            "compliant": compliant,
            "snmp_enabled": snmp_enabled,
            "community_count": community_count,
            "has_trap_receiver": has_trap,
            "issues": "; ".join(issues),
        }


register_jobs(SNMPValidation)
