"""
Purpose:
Check the package compliance on Cisco NCS routers.
"""

from django.conf import settings
import textfsm
from io import StringIO
from netmiko import ConnectHandler

from nautobot.apps.jobs import register_jobs, Job, IntegerVar, BooleanVar

from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import parallel_execution
from custom_jobs.modules.tools import get_device_connection_info

name = "Custom Reporting"

SUPPORTED_PLATFORMS = [
    "cisco_xr",
]

SHOW_LICENSE_STATUS_TEMPLATE = """Value REG_STATUS (REGISTERED|UNREGISTERED)\n
Start
  ^Registration:
  ^\s+Status: ${REG_STATUS}
"""
SHOW_LICENSE_PLATFORM_SUMMARY_TEMPLATE = """Value SIA_STATUS (.*)\n
Start
  ^\s*SIA Status: ${SIA_STATUS}
"""
SHOW_INSTALL_COMMITTED_SUMMARY_TEMPLATE = """Value COMMITTED_FIXES (.*)\n
Start
  ^\s*Committed Fixes ${COMMITTED_FIXES}
"""


class CiscoPackageCompliance(Job, DeviceFormEntry):
    """Job to check the package compliance on Cisco NCS routers."""

    parallel_task = BooleanVar(
        description="Execute backup tasks in parallel",
        default=False,
        required=False,
    )
    max_workers = IntegerVar(
        description="Number of workers to use for parallel execution",
        default=10,
        min_value=1,
        max_value=10,
        required=False,
    )

    class Meta:
        name = "Cisco NCS Package Compliance Report"
        description = "Check the package compliance on Cisco NCS routers."
        has_sensitive_variables = False
        soft_time_limit = 3600
        time_limit = 4800
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
            "bulk",
        ]

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

        self.logger.info(
            "Results Header: ['REG_STATUS', 'SIA_STATUS', 'COMMITTED_FIXES']"
        )

        def run_compliance(device):
            try:
                if device.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{device} Platform {device.platform.network_driver} is not supported. Skipping..."
                    )
                    return
                # self.logger.info(f"{device} Processing device...")
                task = Compliance(job=self, device=device)
                results = task.run()
                self.logger.info(f"{device} Results: {results}")
            except Exception as e:
                self.logger.error(f"{device} Error processing device: {e}")

        if parallel_task:
            parallel_execution(run_compliance, all_devices, max_workers=max_workers)
        else:
            for device in all_devices:
                run_compliance(device)


class Compliance:

    def __init__(self, job, device):
        self.job = job
        self.device = device

    def parse_command_output_from_string(
        self, command_output: str, template_string: str
    ):
        template = textfsm.TextFSM(StringIO(template_string))
        parsed_output = template.ParseText(command_output)
        headers = template.header
        return [dict(zip(headers, row)) for row in parsed_output]

    def run(self):
        command_to_template_mapper = [
            {
                "command": "show license status",
                "template": SHOW_LICENSE_STATUS_TEMPLATE,
                "captured": "REG_STATUS",
            },
            {
                "command": "show license platform summary",
                "template": SHOW_LICENSE_PLATFORM_SUMMARY_TEMPLATE,
                "captured": "SIA_STATUS",
            },
            {
                "command": "show install committed summary",
                "template": SHOW_INSTALL_COMMITTED_SUMMARY_TEMPLATE,
                "captured": "COMMITTED_FIXES",
            },
        ]
        try:
            device_info = get_device_connection_info(self.device)
            with ConnectHandler(**device_info) as session:
                session.enable()
                results = []
                for command_map in command_to_template_mapper:
                    output = session.send_command(command_map["command"])
                    parsed_output = self.parse_command_output_from_string(
                        output, command_map["template"]
                    )
                    if len(parsed_output) > 0:
                        results.append(parsed_output[0][command_map["captured"]])
                    else:
                        results.append("")
                return results
        except Exception as e:
            self.job.logger.error(f"{self.device} Error processing device: {e}")
            return


register_jobs(CiscoPackageCompliance)
