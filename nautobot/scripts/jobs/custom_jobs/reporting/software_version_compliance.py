"""Purpose: Generate a software version compliance report - show which devices are running approved golden images."""

from nautobot.apps.jobs import Job, register_jobs, BooleanVar
from nautobot.dcim.models import Device, Platform

from custom_jobs.modules.tools import apply_device_filters, DeviceFormEntry

name = "Reporting"


class SoftwareVersionComplianceReport(Job, DeviceFormEntry):
    """
    Query Nautobot Device Lifecycle Management for each device's running software version
    vs. the intended (golden) version. Generates a per-platform compliance summary and
    device-level CSV export.

    Requires: nautobot_device_lifecycle_mgmt app.
    """

    class Meta:
        name = "Software Version Compliance Report"
        description = (
            "Compare running software versions against approved golden images per platform. "
            "Requires nautobot_device_lifecycle_mgmt with SoftwareLCM / DeviceSoftwareValidationResult records."
        )
        has_sensitive_variables = False
        soft_time_limit = 900
        time_limit = 1800
        task_queues = ["default", "bulk"]

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
            all_devices = list(Device.objects.all().iterator(chunk_size=500))

        rows = []
        platform_summary = {}  # platform -> {"compliant": int, "total": int}

        for dev in all_devices:
            try:
                from nautobot_device_lifecycle_mgmt.models import DeviceSoftwareValidationResult

                result = DeviceSoftwareValidationResult.objects.filter(device=dev).first()
                platform_name = dev.platform.name if dev.platform else "Unknown"

                if not result:
                    rows.append({
                        "device": dev.name,
                        "platform": platform_name,
                        "running_version": "N/A",
                        "intended_version": "N/A",
                        "compliant": "No data",
                    })
                    continue

                running = str(result.software.version) if result.software else "N/A"
                intended = str(result.software_target.version) if result.software_target else "N/A"
                compliant = result.is_validated

                if platform_name not in platform_summary:
                    platform_summary[platform_name] = {"compliant": 0, "total": 0}
                platform_summary[platform_name]["total"] += 1
                if compliant:
                    platform_summary[platform_name]["compliant"] += 1

                rows.append({
                    "device": dev.name,
                    "platform": platform_name,
                    "running_version": running,
                    "intended_version": intended,
                    "compliant": compliant,
                })

            except Exception as exc:
                self.logger.error(f"{dev} Error: {exc}")

        # Log per-platform summary
        for plat, counts in sorted(platform_summary.items()):
            pct = round(100 * counts["compliant"] / counts["total"], 1) if counts["total"] else 0
            self.logger.info(
                f"Platform {plat}: {counts['compliant']}/{counts['total']} compliant ({pct}%)"
            )

        non_compliant = sum(1 for r in rows if r.get("compliant") is False)
        self.logger.info(
            f"Total: {len(rows)} devices checked, {non_compliant} non-compliant."
        )

        import csv
        import io

        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=["device", "platform", "running_version", "intended_version", "compliant"],
        )
        writer.writeheader()
        writer.writerows(rows)
        self.create_file("software_version_compliance_report.csv", output.getvalue())


register_jobs(SoftwareVersionComplianceReport)
