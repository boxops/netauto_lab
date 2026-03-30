"""Purpose: Generate/update Prometheus static_configs from Nautobot device inventory."""

import os
import yaml

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, StringVar
from nautobot.dcim.models import Device

from custom_jobs.modules.tools import apply_device_filters, DeviceFormEntry

name = "Monitoring"

DEFAULT_OUTPUT_PATH = "/opt/nautobot/prometheus_file_sd/nautobot_targets.yml"


class PrometheusTargetSync(Job, DeviceFormEntry):
    """
    Query active Nautobot devices, build a Prometheus file_sd targets YAML file,
    and write it to disk. Prometheus polls this file to discover scrape targets
    without requiring a restart.

    Labels exported per target:
    - job: device role slug
    - platform: device platform slug
    - location: device location name
    - device: device name
    """

    output_path = StringVar(
        description="Absolute path to write the Prometheus targets YAML file",
        default=DEFAULT_OUTPUT_PATH,
        required=True,
    )
    snmp_port = StringVar(
        description="SNMP exporter port (added as a target label)",
        default="9116",
        required=False,
    )
    dry_run = BooleanVar(
        description="Preview the generated YAML without writing to disk",
        default=True,
        required=False,
    )

    class Meta:
        name = "Prometheus Target Sync"
        description = (
            "Generate or update a Prometheus file_sd targets YAML file from Nautobot device inventory. "
            "Prometheus must be configured with file_sd_configs pointing to the output path."
        )
        has_sensitive_variables = False
        soft_time_limit = 300
        time_limit = 600
        task_queues = ["default", "priority"]

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
        output_path=DEFAULT_OUTPUT_PATH,
        snmp_port="9116",
        dry_run=True,
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

        # If no filter given, fall back to all active devices with a primary IP
        if not all_devices:
            active_status = None
            try:
                from nautobot.extras.models import Status
                active_status = Status.objects.get(name="Active")
            except Exception:
                pass
            query = Device.objects.filter(primary_ip4__isnull=False)
            if active_status:
                query = query.filter(status=active_status)
            all_devices = list(query.iterator(chunk_size=200))

        targets_config = []
        skipped = 0

        for dev in all_devices:
            if not dev.primary_ip4:
                self.logger.warning(f"{dev} has no primary IPv4, skipping.")
                skipped += 1
                continue

            ip = dev.primary_ip4.host
            target = f"{ip}:{snmp_port}"

            labels = {
                "device": dev.name,
                "job": dev.role.name.lower().replace(" ", "-") if dev.role else "unknown",
                "platform": dev.platform.network_driver if dev.platform else "unknown",
                "location": dev.location.name if dev.location else "unknown",
            }

            targets_config.append({"targets": [target], "labels": labels})

        self.logger.info(
            f"Built {len(targets_config)} Prometheus targets ({skipped} devices skipped - no primary IP)."
        )

        yaml_output = yaml.dump(targets_config, default_flow_style=False, sort_keys=True)

        if dry_run:
            self.logger.info(f"DRY RUN: Would write to {output_path}:\n{yaml_output[:2000]}")
            self.create_file("prometheus_targets_preview.yml", yaml_output)
            return

        try:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "w") as f:
                f.write(yaml_output)
            self.logger.info(f"Prometheus targets written to {output_path} ({len(targets_config)} targets).")
        except Exception as exc:
            self.logger.error(f"Failed to write targets file: {exc}")

        self.create_file("prometheus_targets.yml", yaml_output)


register_jobs(PrometheusTargetSync)
