"""
Purpose: Deploy configurations to devices.
"""

from django.conf import settings
from netmiko import ConnectHandler
from deepdiff import DeepDiff

from nautobot.apps.jobs import Job, TextVar, BooleanVar, IntegerVar, MultiObjectVar, register_jobs
from nautobot_golden_config.models import ComplianceFeature, ConfigCompliance, GoldenConfig

from custom_jobs.modules.tools import get_device_connection_info
from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import parallel_execution
from custom_jobs.modules.tools import JobLogBuffer
from custom_jobs.modules.tools import JobProxy

from custom_jobs.configuration.backup_configurations import DeviceBackup
from custom_jobs.configuration.intended_configurations import DeviceIntent
from custom_jobs.configuration.configuration_compliance import DeviceCompliance

name = "Configuration"

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
    "arista_eos",
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

        def deploy_configuration(dev):
            buf = JobLogBuffer()
            try:
                if not hasattr(dev, "platform") or not dev.platform:
                    buf.error(f"{dev} Device has no platform assigned")
                    return buf

                if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    buf.warning(
                        f"{dev} Platform {dev.platform.network_driver} is not supported."
                    )
                    return buf

                buf.info(f"{dev} Processing device...")
                task = Deploy(JobProxy(buf), dev, configuration, pre_and_post_checks)
                task.configure()
            except Exception as e:
                buf.error(f"{dev} Error processing device: {e}")
            return buf

        if parallel_task:
            parallel_execution(
                deploy_configuration, all_devices, max_workers=max_workers, job_logger=self.logger
            )
        else:
            for dev in all_devices:
                deploy_configuration(dev).drain_to(self.logger)


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


class RemediateCompliance(Job, DeviceFormEntry):
    """Push remediation commands derived from ConfigCompliance deviations.

    Intent-driven flow:
        Backup (running) ──┐
                           ├──► Compliance diff ──► missing / extra lines
        Intended (template)┘                              │
                                                          ▼
                                               Remediate (push missing → device)

    'missing' lines (in intended, absent in running) are safe to push — they
    bring the device closer to the desired state.

    'extra' lines (in running, absent in intended) require explicit opt-in via
    include_removals because removing config lines is destructive.
    """

    dry_run = BooleanVar(
        description="Preview remediation commands without pushing them to the device",
        label="Dry run (preview only)",
        default=True,
    )
    include_removals = BooleanVar(
        description=(
            "Also generate 'no <line>' commands for extra lines found in the running config "
            "but absent from the intended config. DESTRUCTIVE — review carefully."
        ),
        label="Include config removals (destructive)",
        default=False,
        required=False,
    )
    refresh_compliance = BooleanVar(
        description="Re-run backup → intended → compliance after a successful push",
        label="Refresh compliance after push",
        default=True,
        required=False,
    )
    rules_filter = MultiObjectVar(
        model=ComplianceFeature,
        description="Select one or more compliance rules to remediate. Leave blank to remediate all non-compliant rules.",
        label="Limit to rules (optional)",
        required=False,
    )
    parallel_task = BooleanVar(
        description="Remediate devices in parallel",
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
        name = "Remediate Configuration Compliance"
        description = (
            "Push missing configuration lines (and optionally remove extra lines) "
            f"derived from ConfigCompliance deviations. Supported platforms: {SUPPORTED_PLATFORMS}"
        )
        has_sensitive_variables = False
        soft_time_limit = 1800
        time_limit = 2400
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
        dry_run=True,
        include_removals=False,
        refresh_compliance=True,
        rules_filter=None,
        parallel_task=False,
        max_workers=10,
    ):
        # Capture selected rule features once so the closure can reference them
        selected_features = rules_filter if rules_filter is not None else None
        if selected_features is not None and selected_features.exists():
            self.logger.info(f"Limiting remediation to rules: {sorted(selected_features.values_list('slug', flat=True))}")

        all_devices = apply_device_filters(
            set(),
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
            self.logger.warning("No devices matched the selected filters.")
            return

        def remediate_device(dev):
            buf = JobLogBuffer()
            try:
                if not dev.platform or dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    drv = dev.platform.network_driver if dev.platform else "none"
                    buf.warning(f"{dev} Platform '{drv}' not supported, skipping.")
                    return buf

                non_compliant = ConfigCompliance.objects.filter(device=dev, compliance=False)
                if selected_features is not None and selected_features.exists():
                    non_compliant = non_compliant.filter(rule__feature__in=selected_features)
                if not non_compliant.exists():
                    buf.info(f"{dev} All selected rules compliant — nothing to remediate.")
                    return buf

                commands = []
                for comp in non_compliant.order_by("rule__feature__slug"):
                    buf.info(f"{dev} Non-compliant rule: {comp.rule.feature.slug}")
                    if comp.missing:
                        for raw in comp.missing.splitlines():
                            line = raw.strip()
                            if line:
                                commands.append(line)
                    if include_removals and comp.extra:
                        for raw in comp.extra.splitlines():
                            line = raw.strip()
                            # Skip comment/blank lines and interface/section headers
                            # already handled by a 'no' at the parent level
                            if line and not line.startswith("!") and not line.startswith("interface "):
                                commands.append(f"no {line}")

                if not commands:
                    buf.info(f"{dev} No remediation commands generated.")
                    return buf

                # Attach commands as a file regardless of dry_run so they can be reviewed
                from datetime import datetime
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"remediation_{dev.name}_{ts}.txt"
                cmd_text = "\n".join(commands)

                buf.info(f"{dev} Remediation commands ({len(commands)}):")
                for cmd in commands:
                    buf.info(f"  {cmd}")

                if dry_run:
                    buf.warning(f"{dev} Dry run — commands NOT pushed to device.")
                    return buf

                # Push to device
                device_info = get_device_connection_info(dev)
                with ConnectHandler(**device_info) as session:
                    session.enable()
                    result = session.send_config_set(commands)
                    buf.info(f"{dev} Push result:\n{result}")

                buf.info(f"{dev} Remediation complete.")

                # Optionally refresh backup → intended → compliance
                if refresh_compliance:
                    buf.info(f"{dev} Refreshing backup → intended → compliance...")
                    proxy = JobProxy(buf)
                    DeviceBackup(job=proxy, device=dev).backup_config()
                    DeviceIntent(job=proxy, device=dev).generate_config()
                    DeviceCompliance(job=proxy, device=dev).run_compliance()

            except Exception as exc:
                buf.error(f"{dev} Remediation failed: {exc}")
            return buf

        if parallel_task:
            parallel_execution(
                remediate_device, all_devices,
                max_workers=max_workers,
                job_logger=self.logger,
            )
        else:
            for dev in all_devices:
                remediate_device(dev).drain_to(self.logger)


register_jobs(RemediateCompliance)
