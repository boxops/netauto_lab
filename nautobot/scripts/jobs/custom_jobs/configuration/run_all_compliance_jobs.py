"""Purpose: Run all device compliance jobs with Nautobot."""

from django.conf import settings

from nautobot.apps.jobs import register_jobs, Job, IntegerVar, BooleanVar
from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import parallel_execution

from custom_jobs.configuration.backup_configurations import DeviceBackup
from custom_jobs.configuration.intended_configurations import DeviceIntent
from custom_jobs.configuration.configuration_compliance import (
    DeviceCompliance,
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
]


class RunAllConfigComplianceJobs(Job, DeviceFormEntry):
    """Job to run all device compliance jobs with Nautobot."""

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
        name = "Generate All Device Configuration Compliance Jobs"
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
        if device:
            all_devices.update(device)

        def compliance_config(device):
            try:
                if device.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{device} Platform {device.platform.network_driver} is not supported. Skipping..."
                    )
                    return
                self.logger.info(f"{device} Processing device...")
                task = AllConfigComplianceJobs(job=self, device=device)
                task.run_jobs()
            except Exception as e:
                self.logger.error(f"{device} Error processing device: {e}")

        if parallel_task:
            parallel_execution(compliance_config, all_devices, max_workers=max_workers)
        else:
            for device in all_devices:
                compliance_config(device)


class AllConfigComplianceJobs:
    def __init__(self, job, device):
        self.job = job
        self.device = device

    def run_jobs(self):
        backup = DeviceBackup(job=self.job, device=self.device)
        backup.backup_config()

        intent = DeviceIntent(job=self.job, device=self.device)
        intent.generate_config()

        compliance = DeviceCompliance(job=self.job, device=self.device)
        compliance.run_compliance()


register_jobs(RunAllConfigComplianceJobs)
