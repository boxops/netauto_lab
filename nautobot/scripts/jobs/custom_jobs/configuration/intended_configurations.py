"""Purpose: Generate intended device configurations with Nautobot."""

from datetime import datetime
from django.conf import settings
import os

from nautobot.apps.jobs import register_jobs, Job, IntegerVar, BooleanVar
from nautobot_golden_config.models import GoldenConfig
from nautobot.extras.models.groups import DynamicGroup
from nautobot.core.utils.data import render_jinja2
from nautobot_golden_config.utilities.graphql import graph_ql_query

from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import parallel_execution
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


class CustomDeviceIntended(Job, DeviceFormEntry):
    """Job to generate intended device configurations with Nautobot."""

    parallel_task = BooleanVar(
        description="Execute intended tasks in parallel",
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
        name = "Generate Intended Device Configurations"
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

        def intended_config(device):
            try:
                if device.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{device} Platform {device.platform.network_driver} is not supported. Skipping..."
                    )
                    return
                self.logger.info(f"{device} Processing device...")
                task = DeviceIntent(job=self, device=device)
                task.generate_config()
            except Exception as e:
                self.logger.error(f"{device} Error processing device: {e}")

        if parallel_task:
            parallel_execution(intended_config, all_devices, max_workers=max_workers)
        else:
            for device in all_devices:
                intended_config(device)


class DeviceIntent:
    def __init__(self, job, device):
        self.job = job
        self.device = device

    def generate_config(self):
        """Generate intended configuration for the device."""

        intended_obj = GoldenConfig.objects.filter(device=self.device).first()
        if not intended_obj:
            intended_obj = GoldenConfig.objects.create(device=self.device)

        intended_obj.intended_last_attempt_date = datetime.now()
        intended_obj.save()

        intended_dynamic_groups = DynamicGroup.objects.exclude(
            golden_config_setting__isnull=True
        )
        intended_dynamic_group = intended_dynamic_groups[0]
        intended_directory = (
            intended_dynamic_group.golden_config_setting.intended_repository.filesystem_path
        )

        intended_path_template_obj = render_jinja2(
            template_code=intended_dynamic_group.golden_config_setting.intended_path_template,
            context={"obj": self.device},
        )
        intended_file = os.path.join(intended_directory, intended_path_template_obj)

        # Create intended_directory if it does not exist
        if not os.path.exists(os.path.dirname(intended_file)):
            os.makedirs(os.path.dirname(intended_file))

        self.job.logger.info(f"{self.device} Intent file: {intended_file}")
        # TODO: Commit and push local Git repository to remote repository

        jinja_template = render_jinja2(
            template_code=intended_dynamic_group.golden_config_setting.jinja_path_template,
            context={"obj": self.device},
        )
        jinja_directory = (
            intended_dynamic_group.golden_config_setting.jinja_repository.filesystem_path
        )
        jinja_file = os.path.join(jinja_directory, jinja_template)

        self.job.request.user = self.job.user
        status, device_data = graph_ql_query(
            self.job.request,
            self.device,
            intended_dynamic_group.golden_config_setting.sot_agg_query.query,
        )

        # self.job.logger.info(f"Jinja Template: {jinja_template}")
        # self.job.logger.info(f"SOT Agg Query Status: {status}")
        # self.job.logger.info(f"Device Data: {device_data}")

        try:
            with open(jinja_file) as file:
                jinja_template_contents = file.read()

            rendered_config = render_jinja2(
                template_code=jinja_template_contents,
                context=device_data,
            )
        except Exception as e:
            self.job.logger.error(f"Failed to render Jinja template: {e}")
            return

        # self.job.logger.info(f"Rendered Config: {rendered_config}")

        # Save the rendered configuration to disk
        with open(intended_file, "w") as file:
            file.write(rendered_config)

        self.job.logger.info(
            f"{self.device} Saved intended configuration to file {intended_file}"
        )

        intended_obj.intended_last_success_date = datetime.now()
        intended_obj.intended_config = rendered_config
        intended_obj.save()

        self.job.logger.info(
            f"{self.device} Successfully generated intended configuration."
        )


register_jobs(CustomDeviceIntended)
