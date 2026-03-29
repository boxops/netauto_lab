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
from custom_jobs.configuration.custom_netutils.compliance import (
    section_config,
    _check_configs_differences,
    feature_compliance,
)
from custom_jobs.modules.git import gc_repos

name = "Configuration"

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

    @gc_repos
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

        def compliance_config(device):
            try:
                if device.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{device} Platform {device.platform.network_driver} is not supported. Skipping..."
                    )
                    return
                self.logger.info(f"{device} Processing device...")
                task = DeviceCompliance(job=self, device=device)
                task.run_compliance()
            except Exception as e:
                self.logger.error(f"{device} Error processing device: {e}")

        if parallel_task:
            parallel_execution(compliance_config, all_devices, max_workers=max_workers)
        else:
            for device in all_devices:
                compliance_config(device)


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

    def run_compliance(self):
        """Generate compliance configuration for the device."""

        compliance_obj = GoldenConfig.objects.filter(device=self.device).first()
        self.job.logger.info(f"{self.device} Compliance object: {compliance_obj}")

        dynamic_groups = DynamicGroup.objects.exclude(
            golden_config_setting__isnull=True
        )
        dynamic_group = dynamic_groups[0]

        intended_directory = (
            dynamic_group.golden_config_setting.intended_repository.filesystem_path
        )
        intended_path_template_obj = render_jinja2(
            template_code=dynamic_group.golden_config_setting.intended_path_template,
            context={"obj": self.device},
        )
        intended_file = os.path.join(intended_directory, intended_path_template_obj)

        if not os.path.exists(intended_file):
            self.job.logger.error(
                f"{self.device} Intended file not found: {intended_file}"
            )
            return

        backup_directory = (
            dynamic_group.golden_config_setting.backup_repository.filesystem_path
        )
        backup_path_template_obj = render_jinja2(
            template_code=dynamic_group.golden_config_setting.backup_path_template,
            context={"obj": self.device},
        )
        backup_file = os.path.join(backup_directory, backup_path_template_obj)

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

        rules.get(platform)

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
