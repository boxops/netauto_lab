"""Purpose: Orchestrate a multi-step change window: pre-checks, backup, deploy, post-validate, notify."""

from datetime import datetime

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, ObjectVar, TextVar, StringVar
from nautobot.dcim.models import Device

from custom_jobs.modules.tools import apply_device_filters, DeviceFormEntry, get_device_connection_info
from custom_jobs.configuration.backup_configurations import DeviceBackup

name = "Orchestration"


class ChangeWindowOrchestrator(Job, DeviceFormEntry):
    """
    Full change-window orchestration workflow:

    1. Pre-checks  — run validation commands and capture pre-change state
    2. Backup      — take a config backup for all selected devices
    3. Deploy      — push the supplied configuration changes
    4. Post-checks — re-run validation commands and diff against pre-change state
    5. Report      — log summary, export diff files, optionally send email notification

    Supports dry-run mode which runs pre-checks and backup only (no deploy).
    """

    change_commands = TextVar(
        description="Configuration commands to deploy (one per line)",
        required=True,
    )
    validation_commands = TextVar(
        description="Show commands to run before and after the change for comparison (one per line)",
        required=False,
    )
    change_ticket = StringVar(
        description="Change ticket / reference number",
        required=False,
    )
    dry_run = BooleanVar(
        description="Run pre-checks and backup only; do not push configuration",
        default=True,
        required=False,
    )

    class Meta:
        name = "Change Window Orchestrator"
        description = (
            "Full change window lifecycle: pre-checks, backup, deploy config, "
            "post-checks, diff report. Supports dry-run."
        )
        has_sensitive_variables = True
        soft_time_limit = 3600
        time_limit = 7200
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
        change_commands="",
        validation_commands="",
        change_ticket="",
        dry_run=True,
    ):
        from netmiko import ConnectHandler

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
        commands = [c.strip() for c in change_commands.splitlines() if c.strip()]
        val_commands = [c.strip() for c in validation_commands.splitlines() if c.strip()]

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
            self.logger.error("No devices selected.")
            return

        self.logger.info(
            f"Change window started | Ticket: {change_ticket or 'N/A'} | "
            f"Devices: {len(all_devices)} | Dry run: {dry_run} | {timestamp}"
        )

        summary = []

        for dev in all_devices:
            if not dev.primary_ip4:
                self.logger.warning(f"{dev} No primary IP, skipping.")
                continue

            dev_result = {"device": dev.name, "backup": False, "pre_check": False, "deployed": False, "post_check": False}

            # --- Step 1: Pre-checks ---
            pre_outputs = {}
            if val_commands:
                try:
                    device_info = get_device_connection_info(dev)
                    with ConnectHandler(**device_info) as session:
                        session.enable()
                        for cmd in val_commands:
                            out = session.send_command(cmd)
                            pre_outputs[cmd] = out
                            self.create_file(
                                f"{dev.name}_pre_{cmd.replace(' ', '_')}.txt", out
                            )
                    dev_result["pre_check"] = True
                    self.logger.info(f"{dev} Pre-checks complete.")
                except Exception as exc:
                    self.logger.error(f"{dev} Pre-check error: {exc}")

            # --- Step 2: Backup ---
            try:
                backup_task = DeviceBackup(job=self, device=dev)
                backup_task.backup_config()
                dev_result["backup"] = True
                self.logger.info(f"{dev} Backup complete.")
            except Exception as exc:
                self.logger.warning(f"{dev} Backup error: {exc}")

            if dry_run:
                self.logger.info(f"{dev} DRY RUN: Skipping deploy and post-checks.")
                summary.append(dev_result)
                continue

            # --- Step 3: Deploy ---
            try:
                device_info = get_device_connection_info(dev)
                with ConnectHandler(**device_info) as session:
                    session.enable()
                    output = session.send_config_set(commands)
                    session.save_config()
                self.create_file(f"{dev.name}_deploy_output.txt", output)
                dev_result["deployed"] = True
                self.logger.info(f"{dev} Configuration deployed.")
            except Exception as exc:
                self.logger.error(f"{dev} Deploy error: {exc}")
                summary.append(dev_result)
                continue

            # --- Step 4: Post-checks and diff ---
            if val_commands:
                try:
                    device_info = get_device_connection_info(dev)
                    with ConnectHandler(**device_info) as session:
                        session.enable()
                        for cmd in val_commands:
                            post_out = session.send_command(cmd)
                            self.create_file(
                                f"{dev.name}_post_{cmd.replace(' ', '_')}.txt", post_out
                            )
                            pre_out = pre_outputs.get(cmd, "")
                            if pre_out != post_out:
                                import difflib
                                diff = "\n".join(
                                    difflib.unified_diff(
                                        pre_out.splitlines(),
                                        post_out.splitlines(),
                                        fromfile=f"pre_{cmd}",
                                        tofile=f"post_{cmd}",
                                    )
                                )
                                self.create_file(
                                    f"{dev.name}_diff_{cmd.replace(' ', '_')}.txt", diff
                                )
                                self.logger.info(f"{dev} Diff for '{cmd}' created.")
                    dev_result["post_check"] = True
                    self.logger.info(f"{dev} Post-checks complete.")
                except Exception as exc:
                    self.logger.error(f"{dev} Post-check error: {exc}")

            summary.append(dev_result)

        # --- Step 5: Summary report ---
        successful = sum(1 for r in summary if r.get("deployed") or dry_run)
        self.logger.info(
            f"Change window complete | Ticket: {change_ticket or 'N/A'} | "
            f"{successful}/{len(summary)} devices processed | "
            f"Dry run: {dry_run}"
        )


register_jobs(ChangeWindowOrchestrator)
