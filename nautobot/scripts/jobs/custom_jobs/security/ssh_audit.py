"""Purpose: Audit SSH configuration on devices - detect weak ciphers, old key algorithms, and Telnet."""

from netmiko import ConnectHandler

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, IntegerVar

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

# Ciphers/algorithms considered weak
WEAK_CIPHERS = [
    "des-cbc",
    "3des-cbc",
    "arcfour",
    "arcfour128",
    "arcfour256",
    "blowfish-cbc",
    "cast128-cbc",
    "aes128-cbc",
    "aes192-cbc",
    "aes256-cbc",
]

WEAK_MACS = [
    "hmac-md5",
    "hmac-md5-96",
    "hmac-sha1-96",
]

WEAK_KEY_EXCHANGES = [
    "diffie-hellman-group1-sha1",
    "diffie-hellman-group14-sha1",
]


class SSHAudit(Job, DeviceFormEntry):
    """
    SSH to devices and check for:
    - Telnet being enabled
    - Weak SSH ciphers, MACs or key-exchange algorithms
    - SSHv1 being enabled
    Reports issues and writes a CSV summary.
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
        name = "SSH Configuration Audit"
        description = (
            "Detect weak SSH ciphers, SSHv1, and enabled Telnet on devices. "
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

        results = []

        def audit_device(dev):
            try:
                if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{dev} platform {dev.platform.network_driver} not supported, skipping."
                    )
                    return
                task = SSHAuditor(job=self, device=dev)
                result = task.run()
                results.append(result)
            except Exception as exc:
                self.logger.error(f"{dev} Error: {exc}")

        if parallel_task:
            parallel_execution(audit_device, all_devices, max_workers=max_workers)
        else:
            for dev in all_devices:
                audit_device(dev)

        at_risk = sum(1 for r in results if not r.get("secure"))
        self.logger.info(
            f"SSH Audit: {at_risk}/{len(results)} devices have SSH security issues."
        )

        import csv
        import io

        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=["device", "secure", "telnet_enabled", "sshv1_enabled", "weak_algorithms", "issues"],
        )
        writer.writeheader()
        writer.writerows(results)
        self.create_file("ssh_audit_report.csv", output.getvalue())


class SSHAuditor:
    SSH_COMMANDS = {
        "cisco_ios": "show run | include ip ssh|transport input",
        "cisco_xe": "show run | include ip ssh|transport input",
        "cisco_xr": "show run | include ssh",
        "cisco_nxos": "show run | include ssh",
        "arista_eos": "show run | grep ssh",
        "fortinet": "show system global | grep admintimeout",
    }

    def __init__(self, job, device):
        self.job = job
        self.device = device

    def run(self):
        platform = self.device.platform.network_driver
        command = self.SSH_COMMANDS.get(platform, "show run | include ssh")

        device_info = get_device_connection_info(self.device)
        try:
            with ConnectHandler(**device_info) as session:
                session.enable()
                ssh_output = session.send_command(command)
        except Exception as exc:
            self.job.logger.error(f"{self.device} Connection error: {exc}")
            return {
                "device": self.device.name,
                "secure": False,
                "telnet_enabled": "unknown",
                "sshv1_enabled": "unknown",
                "weak_algorithms": "",
                "issues": f"Connection error: {exc}",
            }

        output_lower = ssh_output.lower()
        issues = []

        # Check for Telnet
        telnet_enabled = "transport input telnet" in output_lower or (
            "transport input" in output_lower and "ssh" not in output_lower
        )
        if telnet_enabled:
            issues.append("Telnet is enabled on VTY lines")

        # Check for SSHv1
        sshv1_enabled = "ip ssh version 1" in output_lower or (
            "ip ssh version" not in output_lower  # no explicit version = may default to v1 on old IOS
        )
        if "ip ssh version 2" in output_lower:
            sshv1_enabled = False
        if sshv1_enabled:
            issues.append("SSHv2 not explicitly enforced (possible SSHv1 fallback)")

        # Check for weak ciphers/algorithms in config
        found_weak = [alg for alg in WEAK_CIPHERS + WEAK_MACS + WEAK_KEY_EXCHANGES if alg in output_lower]
        if found_weak:
            issues.append(f"Weak algorithms: {found_weak}")

        secure = not issues

        if secure:
            self.job.logger.info(f"{self.device} SSH configuration is SECURE.")
        else:
            self.job.logger.warning(f"{self.device} SSH ISSUES found: {issues}")

        return {
            "device": self.device.name,
            "secure": secure,
            "telnet_enabled": telnet_enabled,
            "sshv1_enabled": sshv1_enabled,
            "weak_algorithms": ";".join(found_weak),
            "issues": "; ".join(issues),
        }


register_jobs(SSHAudit)
