"""Purpose: Validate device login banners against a required policy and flag non-compliant devices."""

import re
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
    "fortinet",
]


class BannerComplianceCheck(Job, DeviceFormEntry):
    """
    SSH to devices, retrieve the login/MOTD banner, and verify it contains
    required policy keywords. Non-compliant devices are logged and exported to CSV.
    """

    required_keywords = StringVar(
        description="Comma-separated keywords that MUST appear in the banner (case-insensitive)",
        required=True,
        default="authorized,unauthorized access,prohibited",
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
        name = "Banner Compliance Check"
        description = (
            "Verify that login/MOTD banners contain required legal/security text. "
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
        required_keywords="authorized,unauthorized access,prohibited",
        parallel_task=False,
        max_workers=10,
    ):
        keywords = [k.strip().lower() for k in required_keywords.split(",") if k.strip()]
        if not keywords:
            self.logger.error("No required keywords specified.")
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
                task = BannerChecker(job=self, device=dev, required_keywords=keywords)
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
            f"Banner Compliance: {compliant}/{len(results)} devices compliant."
        )

        import csv
        import io

        output = io.StringIO()
        writer = csv.DictWriter(
            output, fieldnames=["device", "compliant", "missing_keywords", "banner_snippet"]
        )
        writer.writeheader()
        writer.writerows(results)
        self.create_file("banner_compliance_report.csv", output.getvalue())


class BannerChecker:
    BANNER_COMMANDS = {
        "cisco_ios": "show run | section banner",
        "cisco_xe": "show run | section banner",
        "cisco_xr": "show run | include banner",
        "cisco_nxos": "show run | section banner",
        "arista_eos": "show run | section banner",
        "fortinet": "show system global | grep admintimeout",
    }

    def __init__(self, job, device, required_keywords):
        self.job = job
        self.device = device
        self.required_keywords = required_keywords

    def run(self):
        platform = self.device.platform.network_driver
        command = self.BANNER_COMMANDS.get(platform, "show run | section banner")

        device_info = get_device_connection_info(self.device)
        try:
            with ConnectHandler(**device_info) as session:
                session.enable()
                banner_output = session.send_command(command)
        except Exception as exc:
            self.job.logger.error(f"{self.device} Connection error: {exc}")
            return {
                "device": self.device.name,
                "compliant": False,
                "missing_keywords": str(self.required_keywords),
                "banner_snippet": "",
            }

        banner_lower = banner_output.lower()
        missing = [kw for kw in self.required_keywords if kw not in banner_lower]
        compliant = not missing

        snippet = banner_output[:200].replace("\n", " ")

        if compliant:
            self.job.logger.info(f"{self.device} Banner COMPLIANT.")
        else:
            self.job.logger.warning(
                f"{self.device} Banner NON-COMPLIANT. Missing keywords: {missing}"
            )

        return {
            "device": self.device.name,
            "compliant": compliant,
            "missing_keywords": ";".join(missing),
            "banner_snippet": snippet,
        }


register_jobs(BannerComplianceCheck)
