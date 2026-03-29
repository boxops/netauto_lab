"""Purpose: Validate AAA (TACACS+/RADIUS) configuration on devices against a defined policy."""

from netmiko import ConnectHandler

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, IntegerVar, StringVar

from custom_jobs.modules.tools import (
    get_device_connection_info,
    apply_device_filters,
    DeviceFormEntry,
    parallel_execution,
)

name = "Security"

SUPPORTED_PLATFORMS = [
    "cisco_ios",
    "cisco_xe",
    "cisco_xr",
    "cisco_nxos",
    "arista_eos",
    "fortinet",
]


class AAAComplianceCheck(Job, DeviceFormEntry):
    """
    SSH to devices and verify AAA configuration:
    - Authentication method (TACACS+ or RADIUS) is configured
    - Required AAA server IP is reachable in device config
    - AAA accounting and authorization are configured
    Exports a CSV compliance report.
    """

    aaa_protocol = StringVar(
        description="Required AAA protocol: tacacs or radius",
        default="tacacs",
        required=True,
    )
    required_server_ip = StringVar(
        description="Required AAA server IP address (partial match accepted)",
        required=False,
    )
    require_accounting = BooleanVar(
        description="Require AAA accounting to be configured",
        default=True,
        required=False,
    )
    require_authorization = BooleanVar(
        description="Require AAA authorization to be configured",
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
        name = "AAA Compliance Check"
        description = (
            "Verify TACACS+/RADIUS configuration, accounting, and authorization on devices. "
            f"Supported platforms: {SUPPORTED_PLATFORMS}"
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
        aaa_protocol="tacacs",
        required_server_ip="",
        require_accounting=True,
        require_authorization=True,
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
                task = AAAChecker(
                    job=self,
                    device=dev,
                    aaa_protocol=aaa_protocol,
                    required_server_ip=required_server_ip,
                    require_accounting=require_accounting,
                    require_authorization=require_authorization,
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
            f"AAA Compliance: {compliant}/{len(results)} devices compliant."
        )

        import csv
        import io

        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "device", "compliant", "aaa_new_model", "server_found",
                "has_accounting", "has_authorization", "issues",
            ],
        )
        writer.writeheader()
        writer.writerows(results)
        self.create_file("aaa_compliance_report.csv", output.getvalue())


class AAAChecker:
    AAA_COMMANDS = {
        "cisco_ios": "show run | section aaa",
        "cisco_xe": "show run | section aaa",
        "cisco_xr": "show run | include aaa|tacacs|radius",
        "cisco_nxos": "show run | section aaa",
        "arista_eos": "show run | section aaa",
        "fortinet": "show user tacacs",
    }

    def __init__(self, job, device, aaa_protocol, required_server_ip, require_accounting, require_authorization):
        self.job = job
        self.device = device
        self.aaa_protocol = aaa_protocol.lower()
        self.required_server_ip = required_server_ip
        self.require_accounting = require_accounting
        self.require_authorization = require_authorization

    def run(self):
        platform = self.device.platform.network_driver
        command = self.AAA_COMMANDS.get(platform, "show run | section aaa")

        device_info = get_device_connection_info(self.device)
        try:
            with ConnectHandler(**device_info) as session:
                session.enable()
                aaa_output = session.send_command(command)
        except Exception as exc:
            self.job.logger.error(f"{self.device} Connection error: {exc}")
            return {
                "device": self.device.name,
                "compliant": False,
                "aaa_new_model": False,
                "server_found": False,
                "has_accounting": False,
                "has_authorization": False,
                "issues": f"Connection error: {exc}",
            }

        output_lower = aaa_output.lower()
        issues = []

        aaa_new_model = "aaa new-model" in output_lower
        if not aaa_new_model:
            issues.append("AAA new-model not enabled")

        # Protocol server check
        server_found = self.aaa_protocol in output_lower
        if not server_found:
            issues.append(f"{self.aaa_protocol.upper()} server not configured")

        if self.required_server_ip and self.required_server_ip not in aaa_output:
            issues.append(f"Required AAA server {self.required_server_ip} not found")

        has_accounting = "aaa accounting" in output_lower
        if self.require_accounting and not has_accounting:
            issues.append("AAA accounting not configured")

        has_authorization = "aaa authorization" in output_lower
        if self.require_authorization and not has_authorization:
            issues.append("AAA authorization not configured")

        compliant = not issues

        if compliant:
            self.job.logger.info(f"{self.device} AAA COMPLIANT.")
        else:
            self.job.logger.warning(f"{self.device} AAA NON-COMPLIANT: {issues}")

        return {
            "device": self.device.name,
            "compliant": compliant,
            "aaa_new_model": aaa_new_model,
            "server_found": server_found,
            "has_accounting": has_accounting,
            "has_authorization": has_authorization,
            "issues": "; ".join(issues),
        }


register_jobs(AAAComplianceCheck)
