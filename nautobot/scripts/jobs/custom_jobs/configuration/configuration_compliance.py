"""Purpose: Generate device compliance with Nautobot."""

from datetime import datetime
from django.conf import settings
from django.core.exceptions import ValidationError
from collections import defaultdict

import os

from nautobot_golden_config.choices import (
    ComplianceRuleConfigTypeChoice,
    RemediationTypeChoice,
)
from nautobot.apps.jobs import register_jobs, Job, IntegerVar, BooleanVar
from nautobot.extras.models.groups import DynamicGroup
from nautobot.core.utils.data import render_jinja2

from nautobot_golden_config.models import (
    ComplianceRule,
    GoldenConfig,
    ConfigCompliance,
    _verify_get_custom_compliance_data,
    _get_json_compliance,
    _get_xml_compliance,
    _get_hierconfig_remediation,
)

from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import parallel_execution
from custom_jobs.modules.tools import diff_files
from custom_jobs.modules.tools import _open_file_config
from custom_jobs.modules.tools import JobLogBuffer
from custom_jobs.modules.tools import JobProxy
from custom_jobs.configuration.custom_netutils.compliance import (
    section_config,
    _check_configs_differences,
    feature_compliance,
)

name = "Configuration"

# Fallback directories — must match the values used by the backup/intended jobs.
DEFAULT_BACKUP_ROOT   = getattr(settings, "BACKUP_ROOT",   "/opt/nautobot/backups")
DEFAULT_INTENDED_ROOT = getattr(settings, "INTENDED_ROOT", "/opt/nautobot/intended")

SUPPORTED_PLATFORMS = [
    "keymile_nos",
    "fiberstore_fsos",
    "mikrotik_routeros",
    # "netonix_os",
    "cisco_ios",
    "cisco_xr",
    # "cisco_xe",
    # "cisco_nxos",
    # "cisco_s300",
    # "ubiquiti_airos",
    # "siklu_os",
    "arista_eos",
]


def _null_to_empty(val):
    """Convert to empty string if the value is currently null."""
    if not val:
        return ""
    return val


def _get_custom_cli_compliance(obj):
    """This function performs the actual compliance for cli configuration."""
    feature = {
        "ordered": obj.rule.config_ordered,
        "name": obj.rule,
    }
    feature.update({"section": obj.rule.match_config.splitlines()})
    value = feature_compliance(
        feature,
        obj.actual,
        obj.intended,
        obj.device.platform.network_driver,
    )
    compliance = value["compliant"]
    if compliance:
        compliance_int = 1
        ordered = value["ordered_compliant"]
    else:
        compliance_int = 0
        ordered = value["ordered_compliant"]
    missing = _null_to_empty(value["missing"])
    extra = _null_to_empty(value["extra"])
    return {
        "compliance": compliance,
        "compliance_int": compliance_int,
        "ordered": ordered,
        "missing": missing,
        "extra": extra,
    }


LOCAL_FUNC_MAPPER = {
    ComplianceRuleConfigTypeChoice.TYPE_CLI: _get_custom_cli_compliance,
    ComplianceRuleConfigTypeChoice.TYPE_JSON: _get_json_compliance,
    ComplianceRuleConfigTypeChoice.TYPE_XML: _get_xml_compliance,
    RemediationTypeChoice.TYPE_HIERCONFIG: _get_hierconfig_remediation,
}


# Monkey patch the ConfigCompliance class
ConfigCompliance.FUNC_MAPPER = LOCAL_FUNC_MAPPER


def custom_compliance_on_save(self):
    """Use local compliance logic."""
    if self.rule.custom_compliance:
        if not self.FUNC_MAPPER.get("custom"):
            raise ValidationError("Custom compliance type not configured locally.")
        compliance_details = self.FUNC_MAPPER["custom"](obj=self)
        _verify_get_custom_compliance_data(compliance_details)
    else:
        compliance_details = self.FUNC_MAPPER[self.rule.config_type](obj=self)

    self.compliance = compliance_details["compliance"]
    self.compliance_int = compliance_details["compliance_int"]
    self.ordered = compliance_details["ordered"]
    self.missing = compliance_details["missing"]
    self.extra = compliance_details["extra"]


# Monkey patch the method
ConfigCompliance.compliance_on_save = custom_compliance_on_save


def get_rules():
    """A serializer of sorts to return rule mappings as a dictionary."""
    rules = defaultdict(list)
    for compliance_rule in ComplianceRule.objects.all():
        platform = str(compliance_rule.platform.network_driver)
        rules[platform].append(
            {
                "ordered": compliance_rule.config_ordered,
                "obj": compliance_rule,
                "section": compliance_rule.match_config.splitlines(),
            }
        )
    return rules


class CustomDeviceCompliance(Job, DeviceFormEntry):
    """Job to generate device compliance with Nautobot."""

    parallel_task = BooleanVar(
        description="Execute compliance tasks in parallel",
        default=False,
        required=False,
    )
    max_workers = IntegerVar(
        description="Number of workers to use for parallel execution",
        default=20,
        min_value=1,
        max_value=20,
        required=False,
    )

    class Meta:
        name = "Generate Device Configuration Compliance"
        description = f"Supported platforms: {SUPPORTED_PLATFORMS}"
        has_sensitive_variables = False
        soft_time_limit = 1800  # 30 minutes
        time_limit = 2400  # 40 minutes
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
            "bulk",
        ]

    # @gc_repos  # Uncomment to re-enable Git repository sync once repos are configured.
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
        parallel_task=True,
        max_workers=None,
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

        def compliance_config(dev):
            buf = JobLogBuffer()
            try:
                if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    buf.info(
                        f"{dev} Platform {dev.platform.network_driver} is not supported. Skipping..."
                    )
                    return buf
                buf.info(f"{dev} Processing device...")
                task = DeviceCompliance(job=JobProxy(buf), device=dev)
                task.run_compliance()
            except Exception as e:
                buf.error(f"{dev} Error processing device: {e}")
            return buf

        if parallel_task:
            parallel_execution(compliance_config, all_devices, max_workers=max_workers, job_logger=self.logger)
        else:
            for dev in all_devices:
                compliance_config(dev).drain_to(self.logger)


class DeviceCompliance:
    def __init__(self, job, device):
        self.job = job
        self.device = device

    def sanitize_config(self, config):
        """Sanitize configuration data.
        Column aliases cannot contain whitespace characters, quotation marks, semicolons, or SQL comments.
        """
        config = config.replace('"', "'")
        config = config.replace(";", "")
        config = config.replace("--", "")
        return config

    def _resolve_file_path(self, repo_attr, template_attr, fallback_root, setting):
        """Resolve a config file path from GoldenConfig setting or fall back to a local dir."""
        if (
            setting is not None
            and getattr(setting, repo_attr, None) is not None
            and getattr(setting, template_attr, "")
        ):
            directory = getattr(setting, repo_attr).filesystem_path
            relative = render_jinja2(
                template_code=getattr(setting, template_attr),
                context={"obj": self.device},
            )
            return os.path.join(directory, relative)
        return os.path.join(fallback_root, f"{self.device.name}.txt")

    def run_compliance(self):
        """Generate compliance configuration for the device."""

        compliance_obj = GoldenConfig.objects.filter(device=self.device).first()

        dynamic_groups = DynamicGroup.objects.exclude(
            golden_config_setting__isnull=True
        )
        setting = dynamic_groups[0].golden_config_setting if dynamic_groups.exists() else None

        intended_file = self._resolve_file_path(
            "intended_repository", "intended_path_template", DEFAULT_INTENDED_ROOT, setting
        )

        if not os.path.exists(intended_file):
            self.job.logger.error(
                f"{self.device} Intended file not found: {intended_file}. "
                f"Run the Generate Intended Configurations job first."
            )
            return

        backup_file = self._resolve_file_path(
            "backup_repository", "backup_path_template", DEFAULT_BACKUP_ROOT, setting
        )

        if not os.path.exists(backup_file):
            self.job.logger.error(f"{self.device} Backup file not found: {backup_file}")
            return

        platform = self.device.platform.network_driver

        rules = get_rules()
        if not rules.get(platform):
            self.job.logger.error(
                f"{self.device} No compliance rules found for {platform}"
            )
            return

        backup_cfg = _open_file_config(backup_file)
        intended_cfg = _open_file_config(intended_file)

        # self.job.logger.info(f"{self.device} Backup config: {backup_cfg}")
        # self.job.logger.info(f"{self.device} Intended config: {intended_cfg}")

        for rule in rules[platform]:
            # TODO: Implement get_config_element to also work with JSON and XML
            # _actual = get_config_element(rule, backup_cfg, obj, logger)
            # _intended = get_config_element(rule, intended_cfg, obj, logger)

            # This is for CLI configs only
            _actual = section_config(rule, backup_cfg, platform)
            _intended = section_config(rule, intended_cfg, platform)

            _actual = self.sanitize_config(_actual)
            _intended = self.sanitize_config(_intended)

            diffs = _check_configs_differences(_intended, _actual, platform)

            # self.job.logger.info(f"{self.device} Compliance rule: {rule}")
            # self.job.logger.info(f"{self.device} Compliance actual: {_actual}")
            # self.job.logger.info(f"{self.device} Compliance intended: {_intended}")
            # self.job.logger.info(f"{self.device} Compliance diffs: {diffs}")

            ConfigCompliance.objects.update_or_create(
                device=self.device,
                rule=rule["obj"],
                defaults={
                    "actual": _actual,
                    "intended": _intended,
                    "missing": diffs["missing"],
                    "extra": diffs["extra"],
                    # "missing": "",
                    # "extra": "",
                },
            )

        compliance_obj.compliance_last_success_date = datetime.now()
        compliance_obj.compliance_config = "\n".join(
            diff_files(backup_file, intended_file)
        )
        compliance_obj.save()

        self.job.logger.info(
            f"{self.device} Successfully ran compliance job.",
            extra={"object": self.device},
        )


register_jobs(CustomDeviceCompliance)
