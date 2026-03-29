"""Purpose: Capture device configurations and save them as a backup in Nautobot."""

from datetime import datetime
from netmiko import ConnectHandler
from django.conf import settings
from ncclient import manager
import json
import os

from nautobot.apps.jobs import register_jobs, Job, IntegerVar, BooleanVar
from nautobot_golden_config.models import GoldenConfig

# Git operations are disabled — uncomment to re-enable repository sync.
# from custom_jobs.modules.git import gc_repos
from nautobot.extras.models.groups import DynamicGroup
from nautobot.core.utils.data import render_jinja2

# Default fallback directory used when no Golden Config backup repository is configured.
# Override by setting BACKUP_ROOT in nautobot_config.py, e.g.:
#   BACKUP_ROOT = "/opt/nautobot/backups"
DEFAULT_BACKUP_ROOT = getattr(settings, "BACKUP_ROOT", "/opt/nautobot/backups")

from custom_jobs.modules.tools import get_device_connection_info
from custom_jobs.modules.tools import xml_to_dict
from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import parallel_execution
from custom_jobs.modules.tools import JobLogBuffer
from custom_jobs.modules.tools import JobProxy
from custom_jobs.backends.tachyon import Tachyon

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
    "ubiquiti_airos",
    "ubiquiti_edge",
    "siklu_os",
    "fortinet",
    "tachyon_os",
    "arista_eos",
]


class CustomDeviceBackup(Job, DeviceFormEntry):
    """Job to backup device configurations to Nautobot."""

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
        name = "Backup Device Configurations"
        description = f"Supported platforms: {SUPPORTED_PLATFORMS}"
        has_sensitive_variables = False
        soft_time_limit = 1800  # 30 minutes
        time_limit = 2400  # 40 minutes
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
            "bulk",
        ]

    # @gc_repos  # Uncomment to re-enable Git repository sync pre/post backup.
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

        def _run_device(dev):
            """Back up one device, collecting all log output in a thread-local buffer."""
            buf = JobLogBuffer()
            try:
                if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    buf.info(
                        f"{dev} Platform {dev.platform.network_driver} is not supported. Skipping..."
                    )
                    return buf
                buf.info(f"{dev} Processing device...")
                DeviceBackup(job=JobProxy(buf), device=dev).backup_config()
            except Exception as e:
                buf.error(f"{dev} Error processing device: {e}")
            return buf

        if parallel_task:
            parallel_execution(_run_device, all_devices, max_workers=max_workers, job_logger=self.logger)
        else:
            for dev in all_devices:
                _run_device(dev).drain_to(self.logger)


class DeviceBackup:

    # Mapping of platform driver → CLI command to retrieve running config.
    BACKUP_COMMANDS = {
        "keymile_nos":      "show run",
        "fiberstore_fsos":  "show run",
        "mikrotik_routeros": "/export",
        "netonix_os":       "show config",
        "cisco_ios":        "show run",
        "cisco_xr":         "show run",
        "cisco_xe":         "show run",
        "cisco_nxos":       "show run",
        "cisco_s300":       "show run",
        "ubiquiti_airos":   "cat /tmp/system.cfg",
        "ubiquiti_edge":    "show configuration all | no-more",
        "fortinet":         "show full-configuration",
        "arista_eos":       "show run",
    }

    # Platforms that require `terminal length 0` before issuing the command.
    PAGED_PLATFORMS = {"fiberstore_fsos", "netonix_os"}

    def __init__(self, job, device):
        self.job = job
        self.device = device

    # ------------------------------------------------------------------
    # Protocol-specific config retrieval
    # ------------------------------------------------------------------

    def _netconf_backup(self):
        """Retrieve running config via NETCONF (e.g. Siklu OS)."""
        device_info = get_device_connection_info(self.device)
        netconf_params = {
            "host":             device_info["host"],
            "port":             device_info["port"],
            "username":         device_info["username"],
            "password":         device_info["password"],
            "hostkey_verify":   False,
        }
        try:
            with manager.connect(**netconf_params) as m:
                self.device.cf["can_connect"] = True
                raw_xml = m.get_config(source="running").data_xml
                return xml_to_dict(raw_xml, strip_namespaces=True)
        except Exception as e:
            self.job.logger.error(f"{self.device} NETCONF error: {e}")
            self.device.cf["can_connect"] = False
            return None
        finally:
            self.device.validated_save()

    def _ssh_backup(self):
        """Retrieve running config via SSH using Netmiko."""
        driver = self.device.platform.network_driver
        command = self.BACKUP_COMMANDS.get(driver)
        if not command:
            self.job.logger.error(
                f"{self.device} No backup command defined for platform '{driver}'"
            )
            return None

        device_info = get_device_connection_info(self.device)
        device_info["disable_sha2_fix"] = True

        self.job.logger.info(f"{self.device} Connecting via SSH (command: {command})...")
        try:
            with ConnectHandler(**device_info) as session:
                session.enable()
                self.device.cf["can_connect"] = True
                if driver in self.PAGED_PLATFORMS:
                    session.send_command_timing("terminal length 0")
                    return session.send_command_timing(command)
                return session.send_command(command)
        except Exception as e:
            self.job.logger.error(f"{self.device} SSH error: {e}")
            self.device.cf["can_connect"] = False
            return None
        finally:
            self.device.validated_save()

    # ------------------------------------------------------------------
    # Backup orchestration
    # ------------------------------------------------------------------

    def _resolve_backup_path(self):
        """Return the full filesystem path for this device's backup file.

        Prefers the path template from the Golden Config setting when a backup
        repository is configured. Falls back to DEFAULT_BACKUP_ROOT/<hostname>.txt
        when no repository is set up (e.g. in a lab environment without Git).
        """
        groups = DynamicGroup.objects.exclude(golden_config_setting__isnull=True)
        if groups.exists():
            setting = groups[0].golden_config_setting
            if setting.backup_repository is not None:
                directory = setting.backup_repository.filesystem_path
                relative_path = render_jinja2(
                    template_code=setting.backup_path_template,
                    context={"obj": self.device},
                )
                return os.path.join(directory, relative_path)

        # No Git-backed repository configured — write to the local fallback directory.
        self.job.logger.warning(
            f"{self.device} No Golden Config backup repository configured. "
            f"Writing to fallback directory: {DEFAULT_BACKUP_ROOT}"
        )
        return os.path.join(DEFAULT_BACKUP_ROOT, f"{self.device.name}.txt")

    def backup_config(self):
        """Orchestrate the backup: fetch config, write to disk, update GoldenConfig record."""
        # Ensure a GoldenConfig tracking record exists for this device.
        backup_obj, _ = GoldenConfig.objects.get_or_create(device=self.device)
        backup_obj.backup_last_attempt_date = datetime.now()
        backup_obj.save()

        try:
            backup_file = self._resolve_backup_path()
        except Exception as e:
            self.job.logger.error(f"{self.device} Could not resolve backup path: {e}")
            return

        os.makedirs(os.path.dirname(backup_file), exist_ok=True)
        self.job.logger.info(f"{self.device} Backup target: {backup_file}")

        # Dispatch to the correct retrieval method.
        driver = self.device.platform.network_driver
        running_config = self._ssh_backup()

        if not running_config:
            self.job.logger.error(
                f"{self.device} Backup failed — no configuration retrieved."
            )
            return

        if isinstance(running_config, dict):
            running_config = json.dumps(running_config, indent=4)

        self.job.logger.info(
            f"{self.device} Config retrieved ({len(running_config)} chars). Writing to disk..."
        )
        with open(backup_file, "w") as f:
            f.write(running_config)

        backup_obj.backup_last_success_date = datetime.now()
        backup_obj.backup_config = running_config
        backup_obj.save()
        self.job.logger.info(f"{self.device} Backup complete.")
        # TODO: Commit and push local Git repository to remote repository.


register_jobs(CustomDeviceBackup)
