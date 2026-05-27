"""Purpose: Discover the live device model and correct DeviceType records in Nautobot."""

from netmiko import ConnectHandler
from django.conf import settings

from nautobot.dcim.models import DeviceType, Manufacturer
from nautobot.apps.jobs import register_jobs, Job, BooleanVar, IntegerVar

from custom_jobs.modules.tools import (
    apply_device_filters,
    convert_flat_config_to_dict,
    get_device_connection_info,
    parse_command_output,
    parallel_execution,
    JobLogBuffer,
    JobProxy,
    DeviceFormEntry,
)

name = "Inventory"

SUPPORTED_PLATFORMS = [
    "keymile_nos", "fiberstore_fsos", "mikrotik_routeros", "netonix_os",
    "cisco_ios", "cisco_xr", "cisco_xe", "cisco_nxos", "cisco_s300",
    "ubiquiti_airos", "ubiquiti_edge", "ubiquiti_edgeswitch",
    "ceragon_os", "siklu_os", "cambium_cnmatrix", "fortinet", "arista_eos",
]


class DeviceTypeOnboard(Job, DeviceFormEntry):
    """Discover live device models and correct DeviceType records in Nautobot when stale."""

    parallel_task = BooleanVar(
        description="Execute tasks in parallel",
        default=False,
        required=False,
    )
    max_workers = IntegerVar(
        description="Number of parallel workers",
        default=20,
        min_value=1,
        max_value=20,
        required=False,
    )

    class Meta:
        name = "Device Type Onboard"
        description = (
            "Discover live device models and correct DeviceType records in Nautobot when stale. "
            f"Supported platforms: {SUPPORTED_PLATFORMS}"
        )
        has_sensitive_variables = False
        hidden = True
        soft_time_limit = 1800
        time_limit = 2400
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
            "bulk",
        ]

    def run(self, **kwargs):
        parallel_task = kwargs.pop("parallel_task", False)
        max_workers = kwargs.pop("max_workers", 10)

        all_devices = apply_device_filters(set(), **kwargs)

        if not all_devices:
            self.logger.warning("No devices matched the selected filters.")
            return

        def process_device(dev):
            buf = JobLogBuffer()
            proxy = JobProxy(buf)
            driver = dev.platform.network_driver if dev.platform else None
            if driver not in SUPPORTED_PLATFORMS:
                buf.warning(f"{dev} Platform {driver} not supported for device type onboarding, skipping.")
                return buf
            buf.info(f"{dev} Checking device type against live inventory.")
            OnboardDeviceType(proxy, dev).onboard()
            return buf

        if parallel_task:
            parallel_execution(process_device, all_devices, max_workers, job_logger=self.logger)
        else:
            for dev in all_devices:
                process_device(dev).drain_to(self.logger)


class OnboardDeviceType:
    """Discover the live device model and correct the Nautobot DeviceType when stale."""

    # Tuple: (command, template, field, manufacturer_name, post_command)
    # field=None means the platform needs special extraction logic.
    # post_command is sent after the main command to dismiss pagers, or None.
    PLATFORM_CONFIG = {
        "keymile_nos":         ("show system",                           "keymile_nos_show_system.textfsm",                    "MODEL",       "Keymile",  None),
        "fiberstore_fsos":     ("show version",                          "fiberstore_fsos_show_version.textfsm",               None,          "FS.Com",   None),
        "mikrotik_routeros":   ("/system routerboard print",             "mikrotik_routeros_system_routerboard_print.textfsm", "MODEL",       "MikroTik", None),
        "netonix_os":          ("show status",                           "netonix_os_show_status.textfsm",                     "MODEL",       "Netonix",  None),
        "cisco_ios":           ("show inventory",                        "cisco_ios_show_inventory.textfsm",                   "PID",         "Cisco",    None),
        "cisco_xr":            ("show inventory",                        "cisco_xr_show_inventory.textfsm",                    "PID",         "Cisco",    None),
        "cisco_xe":            ("show inventory",                        "cisco_xe_show_inventory.textfsm",                    "PID",         "Cisco",    None),
        "cisco_nxos":          ("show inventory",                        "cisco_nxos_show_inventory.textfsm",                  "PID",         "Cisco",    None),
        "cisco_s300":          ("show inventory",                        "cisco_s300_show_inventory.textfsm",                  "PID",         "Cisco",    None),
        "ubiquiti_edge":       ("show version",                          "ubiquiti_edge_show_version.textfsm",                 "MODEL",       "Ubiquiti", None),
        "ubiquiti_edgeswitch": ("show version",                          "ubiquiti_edgeswitch_show_version.textfsm",           "MODEL",       "Ubiquiti", None),
        "ceragon_os":          ("platform management unit-status",       "ceragon_os_show_status.textfsm",                     "UNIT_TYPE",   "Ceragon",  "quit"),
        "siklu_os":            ("show system state product",             "siklu_os_show_state.textfsm",                        "DEVICE_TYPE", "Siklu",    "quit"),
        "cambium_cnmatrix":    ("show system information",               "cambium_cnmatrix_show_system.textfsm",               "MODEL_NAME",  "Cambium",  "q"),
        "fortinet":            ("get system status",                     "fortinet_get_system_status.textfsm",                 None,          "Fortinet", None),
        "arista_eos":          ("show version",                          "arista_eos_show_version.textfsm",                    "MODEL",       "Arista",   None),
    }

    def __init__(self, job, device):
        self.job = job
        self.device = device

    def onboard(self, session=None):
        own_session = session is None
        try:
            if own_session:
                session = ConnectHandler(**get_device_connection_info(self.device))
                session.enable()
            platform = self.device.platform.network_driver
            try:
                model, manufacturer_name = self._get_model(session, platform)
            except Exception as exc:
                self.job.logger.error(f"{self.device} Error fetching device model: {exc}")
                return

            if not model:
                self.job.logger.warning(f"{self.device} Could not extract device model, skipping DeviceType update.")
                return

            current_model = self.device.device_type.model if self.device.device_type else None
            if current_model == model:
                self.job.logger.debug(f"{self.device} DeviceType '{model}' is already correct.")
                return

            try:
                mfr, _ = Manufacturer.objects.get_or_create(name=manufacturer_name)
                dt, dt_created = DeviceType.objects.get_or_create(manufacturer=mfr, model=model)
                if dt_created:
                    self.job.logger.info(f"{self.device} Created DeviceType: {manufacturer_name} / {model}")
                self.device.device_type = dt
                self.device.validated_save()
                self.job.logger.info(
                    f"{self.device} Updated DeviceType: '{current_model}' → '{model}'"
                )
            except Exception as exc:
                self.job.logger.error(f"{self.device} Error updating DeviceType: {exc}")
        except Exception as exc:
            self.job.logger.error(f"{self.device} Error onboarding device type: {exc}")
        finally:
            if own_session and session:
                session.disconnect()

    def _get_model(self, session, platform):
        if platform == "ubiquiti_airos":
            return self._ubiquiti_airos(session)

        cfg = self.PLATFORM_CONFIG.get(platform)
        if not cfg:
            raise ValueError(f"Platform '{platform}' not supported for device type discovery.")

        command, template, field, manufacturer, post_command = cfg
        output = session.send_command_timing(command)
        if post_command:
            session.send_command_timing(post_command)

        parsed = parse_command_output(output, template)
        if not parsed:
            return None, manufacturer

        row = parsed[0]
        if platform == "fiberstore_fsos":
            model = f"{row.get('HARDWARE_TYPE', '')}-{row.get('MODEL', '')}".strip("-")
        elif platform == "fortinet":
            raw = row.get("VERSION", "")
            model = raw.split(" ")[0] if raw else None
        else:
            model = row.get(field)
            if isinstance(model, list):
                model = model[0] if model else None

        return model or None, manufacturer

    def _ubiquiti_airos(self, session):
        board_raw = session.send_command_timing("cat /etc/board.info")
        board = convert_flat_config_to_dict(board_raw)
        return board.get("board.name") or None, "Ubiquiti"


register_jobs(DeviceTypeOnboard)
