"""Purpose: Audit interface capacity per device - compare provisioned vs physical ports and flag unused interfaces."""

from netmiko import ConnectHandler

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, IntegerVar
from nautobot.dcim.models import Interface

from custom_jobs.modules.tools import (
    get_device_connection_info,
    apply_device_filters,
    DeviceFormEntry,
    parse_command_output,
    parallel_execution,
)

name = "Inventory"

SUPPORTED_PLATFORMS = [
    "cisco_ios",
    "cisco_xe",
    "cisco_xr",
    "cisco_nxos",
    "arista_eos",
    "fiberstore_fsos",
    "keymile_nos",
]


class InterfaceCapacityAudit(Job, DeviceFormEntry):
    """Compare live interface state to Nautobot records and flag unused or unmanaged ports."""

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
        name = "Interface Capacity Audit"
        description = (
            "Compare provisioned interfaces in Nautobot against physical ports on devices. "
            f"Reports unused and unmanaged ports. Supported platforms: {SUPPORTED_PLATFORMS}"
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

        def audit_device(dev):
            try:
                if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{dev} platform {dev.platform.network_driver} not supported, skipping."
                    )
                    return
                self.logger.info(f"{dev} Auditing interface capacity...")
                task = CapacityAuditor(job=self, device=dev)
                task.run()
            except Exception as exc:
                self.logger.error(f"{dev} Error: {exc}")

        if parallel_task:
            parallel_execution(audit_device, all_devices, max_workers=max_workers)
        else:
            for dev in all_devices:
                audit_device(dev)


class CapacityAuditor:
    COMMANDS = {
        "cisco_ios": "show interfaces status",
        "cisco_xe": "show interfaces status",
        "cisco_xr": "show interfaces brief",
        "cisco_nxos": "show interface status",
        "arista_eos": "show ip interface brief",
        "fiberstore_fsos": "show interface brief",
        "keymile_nos": "show ip interface brief",
    }
    TEMPLATES = {
        "cisco_ios": "cisco_ios_show_ip_interface_brief.textfsm",
        "cisco_xe": "cisco_xe_show_ip_interface_brief.textfsm",
        "cisco_xr": "cisco_xr_show_ip_interface_brief.textfsm",
        "cisco_nxos": "cisco_nxos_show_ip_interface_brief.textfsm",
        "arista_eos": "arista_eos_show_ip_interface_brief.textfsm",
        "fiberstore_fsos": "fiberstore_fsos_show_interface.textfsm",
        "keymile_nos": "keymile_nos_show_ip_interface_brief.textfsm",
    }

    def __init__(self, job, device):
        self.job = job
        self.device = device

    def run(self):
        platform = self.device.platform.network_driver
        command = self.COMMANDS.get(platform)
        template = self.TEMPLATES.get(platform)

        if not command:
            self.job.logger.warning(f"{self.device} No interface command for {platform}.")
            return

        device_info = get_device_connection_info(self.device)
        try:
            with ConnectHandler(**device_info) as session:
                session.enable()
                output = session.send_command(command)
        except Exception as exc:
            self.job.logger.error(f"{self.device} Connection error: {exc}")
            return

        if template:
            live_interfaces = parse_command_output(output, template)
            live_names = {entry.get("INTERFACE", "") for entry in live_interfaces}
        else:
            self.job.logger.info(f"{self.device} Raw interface output:\n{output}")
            return

        nautobot_interfaces = Interface.objects.filter(device=self.device)
        nautobot_names = {iface.name for iface in nautobot_interfaces}

        in_nautobot_not_live = nautobot_names - live_names
        in_live_not_nautobot = live_names - nautobot_names

        self.job.logger.info(
            f"{self.device} Physical ports: {len(live_names)}, "
            f"Nautobot records: {len(nautobot_names)}"
        )

        if in_nautobot_not_live:
            self.job.logger.warning(
                f"{self.device} Interfaces in Nautobot but NOT on device "
                f"(stale records): {sorted(in_nautobot_not_live)}"
            )

        if in_live_not_nautobot:
            self.job.logger.warning(
                f"{self.device} Interfaces on device but NOT in Nautobot "
                f"(unmanaged ports): {sorted(in_live_not_nautobot)}"
            )

        if not in_nautobot_not_live and not in_live_not_nautobot:
            self.job.logger.info(f"{self.device} Interface records are in sync.")

        # Report down/unused interfaces
        for entry in live_interfaces:
            status_field = (entry.get("STATUS", "")).lower()
            intf_name = entry.get("INTERFACE", "")
            if "down" in status_field or "notcon" in status_field:
                self.job.logger.info(
                    f"{self.device} Unused/down interface: {intf_name} (status: {status_field})"
                )

        # Export summary file
        summary = (
            f"Device: {self.device.name}\n"
            f"Physical ports: {len(live_names)}\n"
            f"Nautobot records: {len(nautobot_names)}\n"
            f"Stale Nautobot records: {sorted(in_nautobot_not_live)}\n"
            f"Unmanaged ports: {sorted(in_live_not_nautobot)}\n"
        )
        self.job.create_file(f"{self.device.name}_interface_capacity_audit.txt", summary)


register_jobs(InterfaceCapacityAudit)
