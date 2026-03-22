"""Purpose: Capture device configurations and save them as a backup in Nautobot."""

from datetime import datetime
from netmiko import ConnectHandler
from django.conf import settings
from ncclient import manager
import json
import os

from nautobot.apps.jobs import register_jobs, Job, IntegerVar, BooleanVar
from nautobot_golden_config.models import GoldenConfig

# from nautobot_golden_config.jobs import gc_repos
from custom_jobs.modules.git import gc_repos
from nautobot_golden_config.utilities.helper import get_job_filter
from nautobot.extras.models.groups import DynamicGroup
from nautobot.core.utils.data import render_jinja2

from custom_jobs.modules.tools import get_device_connection_info
from custom_jobs.modules.tools import xml_to_dict
from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import parallel_execution
from custom_jobs.backends.tachyon import Tachyon

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
    "ubiquiti_airos",
    "ubiquiti_edge",
    "siklu_os",
    "fortinet",
    "tachyon_os",
    "arista_eos",
    # TODO: Add more platforms as needed
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

    @gc_repos
    def run(
        self,
        # *args,
        # **data,
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

        def backup_device(device):
            try:
                if device.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{device} Platform {device.platform.network_driver} is not supported. Skipping..."
                    )
                    return
                self.logger.info(f"{device} Processing device...")
                task = DeviceBackup(job=self, device=device)
                task.backup_config()
            except Exception as e:
                self.logger.error(f"{device} Error processing device: {e}")

        if parallel_task:
            parallel_execution(backup_device, all_devices, max_workers=max_workers)
        else:
            for device in all_devices:
                backup_device(device)

        # self.logger.info(f"args: {args}")
        # self.logger.info(f"data: {data}")

        # filtered_devices = get_job_filter(data)
        # self.logger.info(f"Filtered devices: {filtered_devices}")

        # for device in filtered_devices:
        #     self.logger.info(f"Device: {device}")
        #     self.logger.info(f"Device platform: {device.platform.network_driver}")

        # if self.parallel_task:
        #     parallel_execution(
        #         backup_device, filtered_devices, max_workers=self.max_workers
        #     )
        # else:
        #     for device in filtered_devices:
        #         backup_device(device)


class DeviceBackup:
    def __init__(self, job, device):
        self.job = job
        self.device = device

    def netconf_backup_config(self):
        device_info = get_device_connection_info(self.device)
        device_netconf_info = {
            "host": device_info["host"],
            "port": device_info["port"],
            "username": device_info["username"],
            "password": device_info["password"],
            "hostkey_verify": False,
        }

        try:
            with manager.connect(**device_netconf_info) as m:
                self.device.cf["can_connect"] = True

                # print("Server Capabilities:")
                # for capability in m.server_capabilities:
                #     print(capability)

                config = m.get_config(source="running").data_xml
                # print("\nRaw Config XML:\n", config)  # Debugging

                config_dict = xml_to_dict(config, strip_namespaces=True)
                return config_dict
        except Exception as e:
            print(f"Error connecting to device: {e}")
            self.device.cf["can_connect"] = False
            return None
        finally:
            self.device.validated_save()

    def _tachyon_backup_config(self):
        """Backup configuration for Tachyon OS devices using REST API."""
        device_info = get_device_connection_info(self.device)
        device = Tachyon(
            job=self.job,
            ip=device_info["ip"],
            username=device_info["username"],
            password=device_info["password"],
            verbose=True,
            use_https=True,
            verify_ssl=False,
        )
        try:
            device.login()
            self.device.cf["can_connect"] = True
            config = device.get_config()
            device.logout()
            return config
        except Exception as e:
            self.job.logger.error(
                f"{self.device} Error connecting to Tachyon device: {e}"
            )
            self.device.cf["can_connect"] = False
            return None
        finally:
            self.device.validated_save()

    def backup_config(self):
        """Backup configurations to disk using Netmiko."""

        backup_obj = GoldenConfig.objects.filter(device=self.device).first()
        if not backup_obj:
            backup_obj = GoldenConfig.objects.create(device=self.device)

        backup_obj.backup_last_attempt_date = datetime.now()
        backup_obj.save()

        backup_dynamic_groups = DynamicGroup.objects.exclude(
            golden_config_setting__isnull=True
        )
        backup_dynamic_group = backup_dynamic_groups[0]
        backup_directory = (
            backup_dynamic_group.golden_config_setting.backup_repository.filesystem_path
        )
        backup_path_template_obj = render_jinja2(
            template_code=backup_dynamic_group.golden_config_setting.backup_path_template,
            context={"obj": self.device},
        )
        backup_file = os.path.join(backup_directory, backup_path_template_obj)

        # Create backup_directory if it does not exist
        if not os.path.exists(os.path.dirname(backup_file)):
            os.makedirs(os.path.dirname(backup_file))

        self.job.logger.info(f"{self.device} Backup file: {backup_file}")
        # TODO: Commit and push local Git repository to remote repository

        platform_commands = {
            "keymile_nos": "show run",
            "fiberstore_fsos": "show run",
            "mikrotik_routeros": "/export",
            "netonix_os": "show config",
            "cisco_ios": "show run",
            "cisco_xr": "show run",
            "cisco_xe": "show run",
            "cisco_nxos": "show run",
            "cisco_s300": "show run",
            "ubiquiti_airos": "cat /tmp/system.cfg",
            "ubiquiti_edge": "show configuration all | no-more",
            "fortinet": "show full-configuration",
        }

        try:
            running_config = None
            self.job.logger.info(
                f"{self.device} Platform: {self.device.platform.network_driver}"
            )

            if self.device.platform.network_driver == "siklu_os":
                running_config = self.netconf_backup_config()
            elif self.device.platform.network_driver == "tachyon_os":
                running_config = self._tachyon_backup_config()
            else:
                device_info = get_device_connection_info(self.device)
                device_info["disable_sha2_fix"] = True
                self.job.logger.info(f"{self.device} Connecting to device...")
                with ConnectHandler(**device_info) as session:
                    session.enable()
                    self.device.cf["can_connect"] = True
                    command = platform_commands.get(self.device.platform.network_driver)
                    self.job.logger.info(f"{self.device} Using command: {command}")
                    if command:
                        if self.device.platform.network_driver in [
                            "fiberstore_fsos",
                            "netonix_os",
                        ]:
                            session.send_command_timing("terminal length 0")
                            running_config = session.send_command_timing(command)
                        else:
                            # For all other platforms, use the standard send_command method
                            running_config = session.send_command(command)
                    else:
                        self.job.logger.error(
                            f"{self.device} No command defined for platform {self.device.platform.network_driver}"
                        )

            if running_config:
                self.job.logger.info(
                    f"{self.device} Configuration retrieved successfully, length: {len(str(running_config))}"
                )
                if isinstance(running_config, dict):
                    running_config = json.dumps(running_config, indent=4)
                with open(backup_file, "w") as f:
                    f.write(running_config)
                backup_obj.backup_last_success_date = datetime.now()
                backup_obj.backup_config = running_config
                backup_obj.save()
                self.job.logger.info(
                    f"{self.device} Successfully backed up configuration."
                )
            else:
                self.job.logger.error(
                    f"{self.device} Failed to backup configuration - running_config is empty or None."
                )
        except Exception as e:
            self.job.logger.error(
                f"{self.device} Exception raised to backup configuration: {e}"
            )
            self.device.cf["can_connect"] = False
        finally:
            self.device.validated_save()


register_jobs(CustomDeviceBackup)
