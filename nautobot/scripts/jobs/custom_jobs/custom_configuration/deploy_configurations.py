"""
Purpose: Deploy configurations to devices.
"""

from django.conf import settings
from netmiko import ConnectHandler
from deepdiff import DeepDiff

from nautobot.apps.jobs import Job, TextVar, BooleanVar, IntegerVar, register_jobs

from custom_jobs.modules.tools import get_device_connection_info
from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import parallel_execution

name = "Custom Configuration"

SUPPORTED_PLATFORMS = [
    "keymile_nos",
    "fiberstore_fsos",
    "mikrotik_routeros",
    "netonix_os",
    "cisco_ios",
    "cisco_xr",
    "cisco_xe",
    "cisco_nxos",
    "cisco_s300",
]


class DeployConfigurations(Job, DeviceFormEntry):
    configuration = TextVar(description="Configuration to deploy")
    pre_and_post_checks = TextVar(
        description="Pre and post check commands",
        required=False,
    )
    parallel_task = BooleanVar(
        description="Execute tasks in parallel",
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
        name = "Deploy Device Configurations"
        description = f"Supported platforms: {SUPPORTED_PLATFORMS}"
        has_sensitive_variables = False
        soft_time_limit = 1800  # 30 minutes
        time_limit = 2400  # 40 minutes
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
        configuration=None,
        pre_and_post_checks=None,
        parallel_task=None,
        max_workers=None,
    ):
        self.logger.info(f"Starting job with device: {device}")

        # Validate configuration input
        if not configuration or not configuration.strip():
            self.logger.error("No configuration provided or configuration is empty")
            return

        self.logger.info(f"Configuration to deploy: {configuration}")

        all_devices = set()

        # If specific devices are selected, add them to the set
        if device:
            self.logger.info(f"Adding {len(device)} specifically selected devices")
            all_devices.update(device)

        # Apply additional filters if any are provided
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

        self.logger.info(f"Found {len(all_devices)} devices after filtering")

        if not all_devices:
            self.logger.warning("No devices found matching the criteria")
            return

        # for device in all_devices:
        #     try:
        #         if device.platform.network_driver not in SUPPORTED_PLATFORMS:
        #             self.logger.info(
        #                 f"Platform {device.platform.network_driver} is not supported. Skipping..."
        #             )
        #             continue
        #         self.logger.info(f"Processing device: {device}")
        #         task = Deploy(self, device, configuration, pre_and_post_checks)
        #         task.configure()
        #     except Exception as e:
        #         self.logger.error(f"Error processing device {device}: {e}")

        def deploy_configuration(device):
            try:
                self.logger.info(f"{device} Checking platform compatibility...")

                if not hasattr(device, "platform") or not device.platform:
                    self.logger.error(f"{device} Device has no platform assigned")
                    return

                if not hasattr(device.platform, "network_driver"):
                    self.logger.error(
                        f"{device} Platform {device.platform} has no network_driver"
                    )
                    return

                if device.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.warning(
                        f"{device} Platform {device.platform.network_driver} is not supported. Supported: {SUPPORTED_PLATFORMS}"
                    )
                    return

                self.logger.info(
                    f"{device} Platform {device.platform.network_driver} is supported"
                )
                self.logger.info(f"{device} Processing device...")
                task = Deploy(self, device, configuration, pre_and_post_checks)
                task.configure()
            except Exception as e:
                self.logger.error(f"{device} Error processing device: {e}")

        if parallel_task:
            parallel_execution(
                deploy_configuration, all_devices, max_workers=max_workers
            )
        else:
            for device in all_devices:
                deploy_configuration(device)


class Deploy:
    def __init__(self, job, device, configuration, pre_and_post_checks):
        self.job = job
        self.device = device
        self.configuration = configuration
        self.pre_and_post_checks = pre_and_post_checks

    def configure(self):
        try:
            self.job.logger.info(f"{self.device} Getting connection info...")
            device_info = get_device_connection_info(self.device)
            # device_info["session_log"] = "netmiko.log"
            self.job.logger.info(f"{self.device} Attempting connection...")
            with ConnectHandler(**device_info) as session:
                self.job.logger.info(f"{self.device} Connected successfully")
                session.enable()

                if self.pre_and_post_checks:
                    precheck_results = self.run_checks(session, "precheck")

                commands_list = [command for command in self.configuration.splitlines()]
                self.job.logger.info(f"{self.device} Commands: {commands_list}")
                result = session.send_config_set(commands_list)
                self.job.logger.info(f"{self.device} Configuration result:")
                self.job.logger.info(result)

                if self.pre_and_post_checks:
                    postcheck_results = self.run_checks(session, "postcheck")

                    if precheck_results and postcheck_results:
                        self.diff_results(
                            precheck_results,
                            postcheck_results,
                            self.pre_and_post_checks,
                        )
        except Exception as e:
            self.job.logger.error(f"{self.device} Error: {e}")

    def run_checks(self, session, check_type):
        check_results = []
        for line in self.pre_and_post_checks.splitlines():
            check_result = session.send_command_timing(line)
            check_results.append(check_result)
            self.job.logger.info(f"{self.device} Command: {line} Result:")
            self.job.logger.info(check_result)
            self.job.create_file(
                f"{self.device}-{line.replace(' ', '_')}-{check_type}.txt",
                check_result,
            )
        return check_results

    def diff_results(self, precheck_results, postcheck_results, checks):
        for precheck, postcheck, check in zip(
            precheck_results, postcheck_results, checks.splitlines()
        ):
            diff = DeepDiff(precheck, postcheck)
            # {"values_changed":
            #     {"root":
            #         {
            #             "new_value": " show onu info\n----------------------------------------------------------------------------------\n    OLT    | ONU |  STATUS  |  Serial No.  | Distance |  Rx Power  |    Profile\n----------------------------------------------------------------------------------\n         1 |   1 |   Active | GNXS04b96a40 |      3m  | -  9.2 dBm | FiberTwist_FIBRE_900 \n         2 |   1 |   Active | GNXS06048390 |      3m  | - 12.2 dBm | Test_1.2G ', 'old_value': '----------------------------------------------------------------------------------\n    OLT    | ONU |  STATUS  |  Serial No.  | Distance |  Rx Power  |    Profile\n----------------------------------------------------------------------------------\n         1 |   1 |   Active | GNXS04b96a40 |      3m  | -  9.2 dBm | FiberTwist_FIBRE_900 \n         2 |   1 |   Active | GNXS06048390 |      3m  | - 12.2 dBm | Test_1.2G ",
            #             "diff": "--- \n+++ \n@@ -1,3 +1,4 @@\n+ show onu info\n ----------------------------------------------------------------------------------\n     OLT    | ONU |  STATUS  |  Serial No.  | Distance |  Rx Power  |    Profile\n ----------------------------------------------------------------------------------"}
            #     }
            # }

            self.job.logger.info(f"{self.device} Diff for check: {check}")
            if diff.get("values_changed"):
                self.job.logger.info(diff.get("values_changed").get("root").get("diff"))
                self.job.create_file(
                    f"{self.device}-{check.replace(' ', '_')}-diff.txt",
                    diff.get("values_changed").get("root").get("diff"),
                )
            else:
                self.job.logger.info("No differences found.")


register_jobs(DeployConfigurations)
