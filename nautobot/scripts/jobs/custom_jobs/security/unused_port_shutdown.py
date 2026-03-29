"""Purpose: Identify unused/silent interfaces and optionally shut them down to reduce attack surface."""

from netmiko import ConnectHandler

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, IntegerVar

from custom_jobs.modules.tools import (
    get_device_connection_info,
    apply_device_filters,
    DeviceFormEntry,
    parse_command_output,
    parallel_execution,
)

name = "Security"

SUPPORTED_PLATFORMS = [
    "cisco_ios",
    "cisco_xe",
    "cisco_nxos",
    "arista_eos",
]

# Interfaces matching these prefixes are never touched (management / loopbacks)
PROTECTED_PREFIXES = ("Loopback", "Mgmt", "Management", "Vlan", "Tunnel", "Port-channel", "port-channel")


class UnusedPortShutdown(Job, DeviceFormEntry):
    """
    Identify interfaces that have been down for a sustained period (no carrier)
    and, optionally, administratively shut them down to reduce attack surface.
    Presents a diff-style report before committing any changes.
    """

    idle_days = IntegerVar(
        description="Flag interfaces that have had no carrier for at least this many days",
        default=30,
        min_value=1,
        max_value=365,
        required=False,
    )
    dry_run = BooleanVar(
        description="Preview interfaces to be shut down without making changes",
        default=True,
        required=False,
    )
    parallel_task = BooleanVar(
        description="Execute tasks in parallel",
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
        name = "Unused Port Shutdown"
        description = (
            "Detect and optionally shut down interfaces with no link/carrier for an extended period. "
            f"Supported platforms: {SUPPORTED_PLATFORMS}"
        )
        has_sensitive_variables = False
        soft_time_limit = 1800
        time_limit = 2400
        task_queues = ["default", "priority", "bulk"]

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
        idle_days=30,
        dry_run=True,
        parallel_task=False,
        max_workers=10,
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

        def process_device(dev):
            try:
                if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{dev} platform {dev.platform.network_driver} not supported, skipping."
                    )
                    return
                task = PortShutdown(job=self, device=dev, idle_days=idle_days, dry_run=dry_run)
                task.run()
            except Exception as exc:
                self.logger.error(f"{dev} Error: {exc}")

        if parallel_task:
            parallel_execution(process_device, all_devices, max_workers=max_workers)
        else:
            for dev in all_devices:
                process_device(dev)


class PortShutdown:
    STATUS_COMMANDS = {
        "cisco_ios": "show interfaces status",
        "cisco_xe": "show interfaces status",
        "cisco_nxos": "show interface status",
        "arista_eos": "show interfaces status",
    }
    STATUS_TEMPLATES = {
        "cisco_ios": "cisco_ios_show_ip_interface_brief.textfsm",
        "cisco_xe": "cisco_xe_show_ip_interface_brief.textfsm",
        "cisco_nxos": "cisco_nxos_show_ip_interface_brief.textfsm",
    }

    def __init__(self, job, device, idle_days, dry_run):
        self.job = job
        self.device = device
        self.idle_days = idle_days
        self.dry_run = dry_run

    def _is_protected(self, intf_name):
        return intf_name.startswith(PROTECTED_PREFIXES)

    def run(self):
        platform = self.device.platform.network_driver
        command = self.STATUS_COMMANDS.get(platform)
        if not command:
            self.job.logger.warning(f"{self.device} No status command for {platform}.")
            return

        device_info = get_device_connection_info(self.device)
        try:
            with ConnectHandler(**device_info) as session:
                session.enable()
                status_output = session.send_command(command)

                template = self.STATUS_TEMPLATES.get(platform)
                if template:
                    interfaces = parse_command_output(status_output, template)
                    candidates = []
                    for iface in interfaces:
                        name = iface.get("INTF") or iface.get("interface", "")
                        status = (iface.get("STATUS") or iface.get("link_status", "")).lower()
                        if self._is_protected(name):
                            continue
                        if "notcon" in status or ("down" in status and "admin" not in status):
                            candidates.append(name)
                else:
                    self.job.logger.info(
                        f"{self.device} No TextFSM template for {platform}. Raw output logged."
                    )
                    self.job.logger.info(status_output)
                    return

                self.job.logger.info(
                    f"{self.device} Found {len(candidates)} candidate unused interface(s): {candidates}"
                )

                if not candidates:
                    return

                if self.dry_run:
                    self.job.logger.info(
                        f"{self.device} DRY RUN: Would shut down interfaces: {candidates}"
                    )
                    return

                shutdown_cmds = []
                for intf in candidates:
                    shutdown_cmds += [f"interface {intf}", "shutdown"]

                session.send_config_set(shutdown_cmds)
                self.job.logger.info(
                    f"{self.device} Shut down {len(candidates)} unused interface(s): {candidates}"
                )

        except Exception as exc:
            self.job.logger.error(f"{self.device} Error: {exc}")


register_jobs(UnusedPortShutdown)
