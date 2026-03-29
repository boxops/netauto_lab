"""Purpose: Discover LLDP/CDP neighbors from live devices and sync topology links in Nautobot."""

from netmiko import ConnectHandler

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, IntegerVar
from nautobot.dcim.models import Cable, Interface
from nautobot.extras.models import Status

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
]


class LLDPNeighborDiscovery(Job, DeviceFormEntry):
    """SSH to devices, parse LLDP neighbor tables, and create/update Cable records in Nautobot."""

    dry_run = BooleanVar(
        description="Preview discovered neighbors without writing to Nautobot",
        default=False,
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
        name = "LLDP Neighbor Discovery"
        description = (
            "Discover LLDP/CDP neighbors from live devices and sync topology links "
            f"in Nautobot. Supported platforms: {SUPPORTED_PLATFORMS}"
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

        def discover_neighbors(dev):
            try:
                if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{dev} platform {dev.platform.network_driver} not supported, skipping."
                    )
                    return
                self.logger.info(f"{dev} Discovering LLDP neighbors...")
                task = LLDPDiscovery(job=self, device=dev, dry_run=dry_run)
                task.run()
            except Exception as exc:
                self.logger.error(f"{dev} Error: {exc}")

        if parallel_task:
            parallel_execution(discover_neighbors, all_devices, max_workers=max_workers)
        else:
            for dev in all_devices:
                discover_neighbors(dev)


class LLDPDiscovery:
    PLATFORM_COMMANDS = {
        "cisco_ios": "show lldp neighbors detail",
        "cisco_xe": "show lldp neighbors detail",
        "cisco_xr": "show lldp neighbors detail",
        "cisco_nxos": "show lldp neighbors detail",
        "arista_eos": "show lldp neighbors detail",
    }

    TEMPLATE_MAP = {
        "cisco_ios": "cisco_ios_show_lldp_neighbors_detail.textfsm",
        "cisco_xe": "cisco_ios_show_lldp_neighbors_detail.textfsm",
        "cisco_nxos": "cisco_nxos_show_lldp_neighbors_detail.textfsm",
        "arista_eos": "arista_eos_show_lldp_neighbors_detail.textfsm",
    }

    def __init__(self, job, device, dry_run=True):
        self.job = job
        self.device = device
        self.dry_run = dry_run

    def run(self):
        platform = self.device.platform.network_driver
        command = self.PLATFORM_COMMANDS.get(platform)
        if not command:
            self.job.logger.warning(f"{self.device} No LLDP command defined for {platform}.")
            return

        device_info = get_device_connection_info(self.device)
        try:
            with ConnectHandler(**device_info) as session:
                session.enable()
                output = session.send_command(command)

            template = self.TEMPLATE_MAP.get(platform)
            if template:
                neighbors = parse_command_output(output, template)
            else:
                # Fall back to raw logging when no TextFSM template exists
                self.job.logger.info(f"{self.device} Raw LLDP output:\n{output}")
                return

            self.job.logger.info(
                f"{self.device} Found {len(neighbors)} LLDP neighbor(s)."
            )
            for neighbor in neighbors:
                self._process_neighbor(neighbor)
        except Exception as exc:
            self.job.logger.error(f"{self.device} Connection error: {exc}")

    def _process_neighbor(self, neighbor):
        local_port_name = neighbor.get("LOCAL_INTERFACE") or neighbor.get("local_port", "")
        remote_device_name = neighbor.get("NEIGHBOR_NAME") or neighbor.get("NEIGHBOR") or neighbor.get("neighbor", "")
        remote_port_name = neighbor.get("NEIGHBOR_INTERFACE") or neighbor.get("neighbor_port", "")

        self.job.logger.info(
            f"{self.device} Neighbor: {remote_device_name} "
            f"(local {local_port_name} <-> remote {remote_port_name})"
        )

        if self.dry_run:
            return

        try:
            local_iface = Interface.objects.filter(
                device=self.device, name=local_port_name
            ).first()
            if not local_iface:
                self.job.logger.warning(
                    f"{self.device} Local interface '{local_port_name}' not found in Nautobot."
                )
                return

            from nautobot.dcim.models import Device as NautobotDevice
            remote_device = NautobotDevice.objects.filter(name=remote_device_name).first()
            if not remote_device:
                self.job.logger.warning(
                    f"{self.device} Remote device '{remote_device_name}' not found in Nautobot."
                )
                return

            remote_iface = Interface.objects.filter(
                device=remote_device, name=remote_port_name
            ).first()
            if not remote_iface:
                self.job.logger.warning(
                    f"{self.device} Remote interface '{remote_port_name}' not found in Nautobot."
                )
                return

            # Skip virtual/loopback/management interfaces — they cannot be cabled
            UNCABLEABLE_TYPES = {"virtual", "lag", "loopback", "bridge", "tunnel"}
            if local_iface.type in UNCABLEABLE_TYPES:
                self.job.logger.info(
                    f"{self.device} Skipping {local_port_name} (type={local_iface.type}, not cableable)."
                )
                return
            if remote_iface.type in UNCABLEABLE_TYPES:
                self.job.logger.info(
                    f"{self.device} Skipping {remote_port_name} on {remote_device_name} (type={remote_iface.type}, not cableable)."
                )
                return

            existing_cable = Cable.objects.filter(
                termination_a_id=local_iface.pk
            ).first() or Cable.objects.filter(termination_b_id=local_iface.pk).first()

            if existing_cable:
                self.job.logger.info(
                    f"{self.device} Cable already exists for {local_port_name}, skipping."
                )
                return

            connected_status = Status.objects.get_for_model(Cable).get(name="Connected")
            cable = Cable(
                termination_a=local_iface,
                termination_b=remote_iface,
                status=connected_status,
            )
            cable.validated_save()
            self.job.logger.info(
                f"{self.device} Created cable: {local_iface} <-> {remote_iface}"
            )
        except Exception as exc:
            self.job.logger.error(f"{self.device} Failed to create cable: {exc}")


register_jobs(LLDPNeighborDiscovery)
