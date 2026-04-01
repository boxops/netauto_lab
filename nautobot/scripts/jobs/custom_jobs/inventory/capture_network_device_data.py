"""Purpose: Capture all network device state and synchronise it into Nautobot.

A single SSH session per device is opened once and shared across all operations:
  - Interface and VLAN capture
  - Serial number onboarding
  - Software version onboarding
  - LLDP neighbor discovery and Cable creation
  - ARP table sync (IP-to-MAC mapping)
"""

import ipaddress

from netmiko import ConnectHandler
from netaddr import EUI
from netutils.ip import is_ip
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError

from nautobot.dcim.models import Cable, Interface, SoftwareVersion
from nautobot.dcim.choices import InterfaceTypeChoices
from nautobot.ipam.models import IPAddress, Prefix, Namespace, VLAN
from nautobot.extras.models import Status
from nautobot.apps.jobs import register_jobs, Job, IntegerVar, BooleanVar

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

# Platforms supported per operation.
SUPPORTED_PLATFORMS_INTERFACES = ["cisco_xr", "arista_eos"]

SUPPORTED_PLATFORMS_SERIAL = [
    "keymile_nos", "fiberstore_fsos", "mikrotik_routeros",
    "cisco_ios", "cisco_xr", "cisco_xe", "cisco_nxos", "cisco_s300",
    "ubiquiti_airos", "ubiquiti_edge", "ubiquiti_edgeswitch",
    "ceragon_os", "siklu_os", "cambium_cnmatrix", "arista_eos",
]

SUPPORTED_PLATFORMS_VERSION = [
    "keymile_nos", "fiberstore_fsos", "mikrotik_routeros", "netonix_os",
    "cisco_ios", "cisco_xr", "cisco_xe", "cisco_nxos", "cisco_s300",
    "ubiquiti_airos", "ubiquiti_edge", "ubiquiti_edgeswitch",
    "ceragon_os", "siklu_os", "cambium_cnmatrix", "arista_eos",
]

SUPPORTED_PLATFORMS_LLDP = [
    "cisco_ios", "cisco_xe", "cisco_xr", "cisco_nxos", "arista_eos",
]

SUPPORTED_PLATFORMS_ARP = [
    "cisco_ios", "cisco_xe", "cisco_xr", "cisco_nxos", "arista_eos",
]

# Per-platform CLI command, TextFSM template, and normalised field-name mapping.
# An empty string value means the field is unavailable from that platform's output.
PLATFORM_CONFIG = {
    "cisco_xr": {
        "command": "show interfaces",
        "template": "cisco_xr_show_interfaces.textfsm",
        "field_map": {
            "name":        "INTERFACE",
            "link_status": "LINK_STATUS",
            "description": "DESCRIPTION",
            "mac_address": "MAC_ADDRESS",
            "mtu":         "MTU",
            "ip_address":  "IP_ADDRESS",
            "speed":       "SPEED",
            "duplex":      "DUPLEX",
        },
    },
    "arista_eos": {
        "command": "show interfaces",
        "template": "arista_eos_show_interfaces.textfsm",
        "field_map": {
            "name":        "INTERFACE",
            "link_status": "LINK_STATUS",
            "description": "DESCRIPTION",
            "mac_address": "MAC_ADDRESS",
            "mtu":         "MTU",
            "ip_address":  "IP_ADDRESS",
            "speed":       "",
            "duplex":      "",
        },
        "vlan": {
            "command":  "show vlan",
            "template": "arista_eos_show_vlan.textfsm",
        },
    },
}


class CustomCaptureDeviceData(Job, DeviceFormEntry):
    """Capture all live device state and synchronise it into Nautobot via a single SSH session."""

    onboard_serial_numbers = BooleanVar(
        description="Onboard device serial numbers",
        default=True,
        required=False,
    )
    onboard_software_version = BooleanVar(
        description="Onboard device software versions",
        default=True,
        required=False,
    )
    discover_lldp_neighbors = BooleanVar(
        description="Discover LLDP neighbors and create Cable records in Nautobot",
        default=True,
        required=False,
    )
    sync_arp_mac = BooleanVar(
        description="Sync ARP table entries (IP-to-MAC) into Nautobot IP address records",
        default=True,
        required=False,
    )
    dry_run = BooleanVar(
        description="Preview LLDP and ARP changes without writing to Nautobot",
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
        default=20,
        min_value=1,
        max_value=20,
        required=False,
    )

    class Meta:
        name = "Capture Network Device Data"
        description = (
            "Capture interfaces, VLANs, serial numbers, software versions, "
            "LLDP neighbors, and ARP/MAC tables using a single SSH session per device."
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
        onboard_serial_numbers=True,
        onboard_software_version=True,
        discover_lldp_neighbors=True,
        sync_arp_mac=True,
        dry_run=False,
        parallel_task=False,
        max_workers=20,
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

        if not all_devices:
            self.logger.warning("No devices matched the selected filters.")
            return

        def process_device(dev):
            buf = JobLogBuffer()
            proxy = JobProxy(buf)
            driver = dev.platform.network_driver if dev.platform else None
            try:
                with ConnectHandler(**get_device_connection_info(dev)) as session:
                    session.enable()

                    if driver in SUPPORTED_PLATFORMS_INTERFACES:
                        buf.info(f"{dev} Starting interface and VLAN capture.")
                        CaptureDeviceData(proxy, dev).execute(session)
                    else:
                        buf.warning(f"{dev} Platform {driver} not supported for interface/VLAN capture, skipping.")

                    if onboard_serial_numbers:
                        if driver in SUPPORTED_PLATFORMS_SERIAL:
                            buf.info(f"{dev} Onboarding serial number.")
                            OnboardSerial(proxy, dev).onboard(session)
                        else:
                            buf.warning(f"{dev} Platform {driver} not supported for serial onboarding, skipping.")

                    if onboard_software_version:
                        if driver in SUPPORTED_PLATFORMS_VERSION:
                            buf.info(f"{dev} Onboarding software version.")
                            OnboardVersion(proxy, dev).onboard(session)
                        else:
                            buf.warning(f"{dev} Platform {driver} not supported for version onboarding, skipping.")

                    if discover_lldp_neighbors:
                        if driver in SUPPORTED_PLATFORMS_LLDP:
                            buf.info(f"{dev} Discovering LLDP neighbors.")
                            LLDPDiscovery(proxy, dev, dry_run=dry_run).run(session)
                        else:
                            buf.warning(f"{dev} Platform {driver} not supported for LLDP discovery, skipping.")

                    if sync_arp_mac:
                        if driver in SUPPORTED_PLATFORMS_ARP:
                            buf.info(f"{dev} Syncing ARP/MAC table.")
                            ARPMACCollector(proxy, dev, dry_run=dry_run).run(session)
                        else:
                            buf.warning(f"{dev} Platform {driver} not supported for ARP/MAC sync, skipping.")

            except Exception as exc:
                buf.error(f"{dev} Error: {exc}")
            return buf

        if parallel_task:
            parallel_execution(process_device, all_devices, max_workers, job_logger=self.logger)
        else:
            for dev in all_devices:
                process_device(dev).drain_to(self.logger)


# ── Interface and VLAN capture ────────────────────────────────────────────────

class CaptureDeviceData:
    """Capture interface and VLAN state from a device and sync to Nautobot."""

    def __init__(self, job, device):
        self.job = job
        self.device = device
        self.captured_data = {}

    def execute(self, session=None):
        """Capture interface and VLAN data and sync to Nautobot.

        Accepts an existing Netmiko session for efficiency. If none is provided
        (e.g. when called from onboard_device.py after the main session is closed),
        a new session is opened and closed automatically.
        """
        own_session = session is None
        try:
            if own_session:
                session = ConnectHandler(**get_device_connection_info(self.device))
                session.enable()
            self._capture_interfaces(session)
            self._import_interfaces()
            self._capture_vlans(session)
            self._import_vlans()
        except Exception as exc:
            self.job.logger.error(f"{self.device} Error: {exc}")
        finally:
            if own_session and session:
                session.disconnect()

    def _capture_interfaces(self, session):
        platform = self.device.platform.network_driver
        config = PLATFORM_CONFIG[platform]
        parsed = parse_command_output(session.send_command(config["command"]), config["template"])
        self.captured_data["interfaces"] = parsed
        self.job.logger.info(f"{self.device} Captured {len(parsed)} interface entries.")

    def _import_interfaces(self):
        platform = self.device.platform.network_driver
        field_map = PLATFORM_CONFIG[platform]["field_map"]
        active_status = Status.objects.get(name="Active")

        for item in self.captured_data.get("interfaces", []):
            intf_name = item.get(field_map["name"], "")
            if not intf_name:
                continue

            def _get(key):
                k = field_map.get(key, "")
                return item.get(k, "") if k else ""

            interface = self._get_or_create_interface(
                intf_name, self.device, InterfaceTypeChoices.TYPE_1GE_FIXED, active_status
            )

            link_status = _get("link_status")
            description = _get("description")
            mac_address = _get("mac_address")
            mtu = _get("mtu")
            ip_address = _get("ip_address")

            if link_status:
                interface.enabled = "up" in link_status.lower()
            if description and interface.description != description:
                interface.description = description
            if mac_address:
                try:
                    converted = EUI(mac_address)
                    if interface.mac_address != converted:
                        interface.mac_address = converted
                except Exception:
                    pass
            if mtu and interface.mtu != mtu:
                interface.mtu = mtu

            interface.validated_save()

            if ip_address and ip_address not in ("", "unassigned"):
                try:
                    self._assign_ip(interface, ip_address)
                except Exception as exc:
                    self.job.logger.error(f"{self.device} Failed to set IP on {intf_name}: {exc}")

            self.job.logger.info(f"{self.device} Updated interface {intf_name}.")

    def _capture_vlans(self, session):
        vlan_cfg = PLATFORM_CONFIG.get(self.device.platform.network_driver, {}).get("vlan")
        if not vlan_cfg:
            return
        parsed = parse_command_output(session.send_command(vlan_cfg["command"]), vlan_cfg["template"])
        self.captured_data["vlans"] = parsed
        self.job.logger.info(f"{self.device} Captured {len(parsed)} VLAN entries.")

    def _import_vlans(self):
        vlans = self.captured_data.get("vlans")
        if not vlans:
            return
        active_status = Status.objects.get(name="Active")
        device_location = self.device.location
        created_count = updated_count = skipped_count = 0

        for item in vlans:
            try:
                vid = int(item.get("VLAN_ID", 0))
            except (ValueError, TypeError):
                continue
            if not vid:
                continue
            name = item.get("VLAN_NAME") or f"VLAN{vid}"

            vlan = None
            if device_location:
                vlan = VLAN.objects.filter(vid=vid, location=device_location).first()
            if vlan is None:
                vlan = VLAN.objects.filter(vid=vid, location__isnull=True).first()
            if vlan is None:
                vlan = self._create_vlan(vid, name, active_status, device_location)
                created_count += 1
                self.job.logger.info(
                    f"{self.device} Created VLAN {vid} ({name})"
                    + (f" at {vlan.location}." if vlan.location else " (no location — add ipam.vlan to Site LocationType content types).")
                )
                continue

            changed = False
            if vlan.name != name:
                vlan.name = name
                changed = True
            if device_location and vlan.location is None:
                vlan.location = device_location
                changed = True

            if changed:
                self._save_vlan(vlan, vid, name, device_location)
                updated_count += 1
                self.job.logger.info(f"{self.device} Updated VLAN {vid} ({name}).")
            else:
                skipped_count += 1
                self.job.logger.debug(f"{self.device} VLAN {vid} ({name}) already up-to-date.")

        self.job.logger.info(
            f"{self.device} VLANs: {created_count} created, "
            f"{updated_count} updated, {skipped_count} already up-to-date."
        )

    def _get_or_create_interface(self, name, device, iface_type, status):
        try:
            return Interface.objects.get(name=name, device=device)
        except ObjectDoesNotExist:
            return Interface(name=name, device=device, type=iface_type, status=status)

    def _assign_ip(self, interface, ip_address):
        if not is_ip(ip_address.split("/")[0]):
            self.job.logger.error(f"{self.device} Invalid IP: {ip_address}")
            return
        ip_network = str(ipaddress.ip_network(ip_address, strict=False))
        try:
            Prefix.objects.get(prefix=ip_network)
        except ObjectDoesNotExist:
            Prefix.objects.create(
                prefix=ip_network,
                status=Status.objects.get(name="Active"),
                namespace=Namespace.objects.get(name="Global"),
            )
            self.job.logger.info(f"{self.device} Created prefix {ip_network}.")
        try:
            ip_obj = IPAddress.objects.get(address=ip_address)
        except ObjectDoesNotExist:
            ip_obj = IPAddress.objects.create(
                address=ip_address,
                namespace=Namespace.objects.get(name="Global"),
                status=Status.objects.get(name="Active"),
            )
            self.job.logger.info(f"{self.device} Created IP {ip_address}.")
        if not interface.ip_addresses.filter(id=ip_obj.id).exists():
            interface.ip_addresses.add(ip_obj)
            self.job.logger.info(f"{self.device} Assigned {ip_address} to {interface}.")

    def _create_vlan(self, vid, name, status, location):
        try:
            return VLAN.objects.create(vid=vid, name=name, status=status, location=location)
        except (ValidationError, Exception) as exc:
            if location is not None and "may not associate to Locations" in str(exc):
                self.job.logger.warning(
                    f"{self.device} Site LocationType does not allow VLANs — "
                    "add 'ipam.vlan' to Site content types and re-run. "
                    f"Creating VLAN {vid} without location for now."
                )
                return VLAN.objects.create(vid=vid, name=name, status=status)
            raise

    def _save_vlan(self, vlan, vid, name, device_location):
        try:
            vlan.validated_save()
        except (ValidationError, Exception) as exc:
            if device_location is not None and "may not associate to Locations" in str(exc):
                self.job.logger.warning(
                    f"{self.device} Site LocationType does not allow VLANs — "
                    "add 'ipam.vlan' to Site content types and re-run. "
                    f"Saving VLAN {vid} without location for now."
                )
                vlan.location = None
                vlan.validated_save()
            else:
                raise


# ── Serial number helper ──────────────────────────────────────────────────────

class OnboardSerial:
    """Fetch and store the serial number for a single device."""

    PLATFORM_COMMANDS = {
        "keymile_nos":         ("show system",                              "keymile_nos_show_system.textfsm"),
        "fiberstore_fsos":     ("show version",                             "fiberstore_fsos_show_version.textfsm"),
        "mikrotik_routeros":   ("/system routerboard print",                "mikrotik_routeros_system_routerboard_print.textfsm"),
        "cisco_ios":           ("show version",                             "cisco_ios_show_version.textfsm"),
        "cisco_xr":            ("show inventory",                           "cisco_xr_show_inventory.textfsm"),
        "cisco_xe":            ("show inventory",                           "cisco_xe_show_inventory.textfsm"),
        "cisco_nxos":          ("show inventory",                           "cisco_nxos_show_inventory.textfsm"),
        "cisco_s300":          ("show inventory",                           "cisco_s300_show_inventory.textfsm"),
        "ubiquiti_edge":       ("show version",                             "ubiquiti_edge_show_version.textfsm"),
        "ubiquiti_edgeswitch": ("show version",                             "ubiquiti_edgeswitch_show_version.textfsm"),
        "ceragon_os":          ("platform management inventory show info",  "ceragon_os_show_info.textfsm"),
        "siklu_os":            ("show inventory component 1 serial-num",    "siklu_os_show_serial.textfsm"),
        "cambium_cnmatrix":    ("show system information",                  "cambium_cnmatrix_show_system.textfsm"),
        "arista_eos":          ("show version",                             "arista_eos_show_version.textfsm"),
    }

    def __init__(self, job, device):
        self.job = job
        self.device = device

    def onboard(self, session):
        platform = self.device.platform.network_driver
        try:
            if platform == "ubiquiti_airos":
                serial = self._ubiquiti_airos(session)
            elif platform in self.PLATFORM_COMMANDS:
                command, template = self.PLATFORM_COMMANDS[platform]
                serial = self._get_serial_number(session, command, template)
            else:
                raise ValueError(f"Platform '{platform}' is not supported for serial onboarding.")

            if serial:
                self.device.serial = serial
                self.device.validated_save()
                self.job.logger.info(f"{self.device} Assigned serial number: {serial}")
            else:
                self.job.logger.info(f"{self.device} Serial number was not extracted.")
        except Exception as exc:
            self.job.logger.error(f"{self.device} Error onboarding serial: {exc}")

    def _get_serial_number(self, session, command, template):
        output = session.send_command_timing(command)
        if self.device.platform.network_driver == "cambium_cnmatrix":
            session.send_command_timing("q")
        parsed = parse_command_output(output, template)
        if not parsed:
            self.job.logger.warning(f"{self.device} No serial number data found.")
            return None
        if "SERIAL_NUMBER" not in parsed[0]:
            self.job.logger.warning(f"{self.device} SERIAL_NUMBER field not found in parsed output.")
            return None
        value = parsed[0]["SERIAL_NUMBER"]
        if isinstance(value, list):
            return value[0] if value else None
        return value

    def _ubiquiti_airos(self, session):
        parsed = convert_flat_config_to_dict(session.send_command_timing("cat /etc/board.info"))
        if "board.hwaddr" not in parsed:
            self.job.logger.warning(f"{self.device} board.hwaddr not found.")
            return None
        return parsed["board.hwaddr"]


# ── Software version helper ───────────────────────────────────────────────────

class OnboardVersion:
    """Fetch and store the software version for a single device."""

    PLATFORM_COMMANDS = {
        "keymile_nos":         ("show system",                              "keymile_nos_show_system.textfsm"),
        "fiberstore_fsos":     ("show version",                             "fiberstore_fsos_show_version.textfsm"),
        "mikrotik_routeros":   ("/system routerboard print",                "mikrotik_routeros_system_routerboard_print.textfsm"),
        "netonix_os":          ("show status",                              "netonix_os_show_status.textfsm"),
        "cisco_ios":           ("show version",                             "cisco_ios_show_version.textfsm"),
        "cisco_xr":            ("show version",                             "cisco_xr_show_version.textfsm"),
        "cisco_xe":            ("show version",                             "cisco_xe_show_version.textfsm"),
        "cisco_nxos":          ("show version",                             "cisco_nxos_show_version.textfsm"),
        "cisco_s300":          ("show version",                             "cisco_s300_show_version.textfsm"),
        "ubiquiti_airos":      ("cat /etc/version",                         "ubiquiti_airos_show_version.textfsm"),
        "ubiquiti_edge":       ("show version",                             "ubiquiti_edge_show_version.textfsm"),
        "ubiquiti_edgeswitch": ("show version",                             "ubiquiti_edgeswitch_show_version.textfsm"),
        "ceragon_os":          ("platform software show versions",          "ceragon_os_show_versions.textfsm"),
        "siklu_os":            ("show inventory component 1 software-rev",  "siklu_os_show_version.textfsm"),
        "cambium_cnmatrix":    ("show system information",                  "cambium_cnmatrix_show_system.textfsm"),
        "arista_eos":          ("show version",                             "arista_eos_show_version.textfsm"),
    }

    def __init__(self, job, device):
        self.job = job
        self.device = device

    def onboard(self, session):
        platform = self.device.platform.network_driver
        try:
            command, template = self.PLATFORM_COMMANDS[platform]
            self.job.logger.info(f"{self.device} Sending command: {command}")
            output = session.send_command_timing(command)
            if platform == "cambium_cnmatrix":
                session.send_command_timing("q")
            parsed = parse_command_output(output, template)
            version = parsed[0]["VERSION"]
            if isinstance(version, list):
                version = version[0]
            if not version:
                self.job.logger.info(f"{self.device} Software version not found. Skipping.")
                return
            self.job.logger.info(f"{self.device} Software version: {version}")
            sw, created = SoftwareVersion.objects.get_or_create(
                version=version,
                platform=self.device.platform,
                defaults={"status": Status.objects.get(name="Active")},
            )
            self.job.logger.info(
                f"{self.device} {'Created' if created else 'Found existing'} software version {sw}."
            )
            self.device.software_version = sw
            self.device.validated_save()
            self.job.logger.info(f"{self.device} Assigned software version {sw}.")
        except Exception as exc:
            self.job.logger.error(f"{self.device} Error onboarding version: {exc}")


# ── LLDP neighbor discovery ───────────────────────────────────────────────────

class LLDPDiscovery:
    """Discover LLDP neighbors and create Cable records in Nautobot."""

    PLATFORM_COMMANDS = {
        "cisco_ios":  "show lldp neighbors detail",
        "cisco_xe":   "show lldp neighbors detail",
        "cisco_xr":   "show lldp neighbors detail",
        "cisco_nxos": "show lldp neighbors detail",
        "arista_eos": "show lldp neighbors detail",
    }
    TEMPLATE_MAP = {
        "cisco_ios":  "cisco_ios_show_lldp_neighbors_detail.textfsm",
        "cisco_xe":   "cisco_ios_show_lldp_neighbors_detail.textfsm",
        "cisco_nxos": "cisco_nxos_show_lldp_neighbors_detail.textfsm",
        "arista_eos": "arista_eos_show_lldp_neighbors_detail.textfsm",
    }
    UNCABLEABLE_TYPES = {"virtual", "lag", "loopback", "bridge", "tunnel"}

    def __init__(self, job, device, dry_run=False):
        self.job = job
        self.device = device
        self.dry_run = dry_run

    def run(self, session):
        platform = self.device.platform.network_driver
        command = self.PLATFORM_COMMANDS.get(platform)
        if not command:
            self.job.logger.warning(f"{self.device} No LLDP command defined for {platform}.")
            return
        output = session.send_command(command)
        template = self.TEMPLATE_MAP.get(platform)
        if not template:
            self.job.logger.info(f"{self.device} Raw LLDP output:\n{output}")
            return
        neighbors = parse_command_output(output, template)
        self.job.logger.info(f"{self.device} Found {len(neighbors)} LLDP neighbor(s).")
        for neighbor in neighbors:
            self._process_neighbor(neighbor)

    def _process_neighbor(self, neighbor):
        local_port = neighbor.get("LOCAL_INTERFACE") or neighbor.get("local_port", "")
        remote_name = (
            neighbor.get("NEIGHBOR_NAME") or neighbor.get("NEIGHBOR") or neighbor.get("neighbor", "")
        )
        remote_port = neighbor.get("NEIGHBOR_INTERFACE") or neighbor.get("neighbor_port", "")
        self.job.logger.info(
            f"{self.device} Neighbor: {remote_name} (local {local_port} <-> remote {remote_port})"
        )
        if self.dry_run:
            return
        try:
            local_iface = Interface.objects.filter(device=self.device, name=local_port).first()
            if not local_iface:
                self.job.logger.warning(f"{self.device} Local interface '{local_port}' not found.")
                return

            from nautobot.dcim.models import Device as NautobotDevice
            remote_device = NautobotDevice.objects.filter(name=remote_name).first()
            if not remote_device:
                self.job.logger.warning(f"{self.device} Remote device '{remote_name}' not found.")
                return

            remote_iface = Interface.objects.filter(device=remote_device, name=remote_port).first()
            if not remote_iface:
                self.job.logger.warning(f"{self.device} Remote interface '{remote_port}' not found.")
                return

            for iface, label in ((local_iface, local_port), (remote_iface, remote_port)):
                if iface.type in self.UNCABLEABLE_TYPES:
                    self.job.logger.info(
                        f"{self.device} Skipping {label} (type={iface.type}, not cableable)."
                    )
                    return

            if (
                Cable.objects.filter(termination_a_id=local_iface.pk).exists()
                or Cable.objects.filter(termination_b_id=local_iface.pk).exists()
            ):
                self.job.logger.info(f"{self.device} Cable already exists for {local_port}, skipping.")
                return

            cable = Cable(
                termination_a=local_iface,
                termination_b=remote_iface,
                status=Status.objects.get_for_model(Cable).get(name="Connected"),
            )
            cable.validated_save()
            self.job.logger.info(f"{self.device} Created cable: {local_iface} <-> {remote_iface}")
        except Exception as exc:
            self.job.logger.error(f"{self.device} Failed to create cable: {exc}")


# ── ARP and MAC table sync ────────────────────────────────────────────────────

class ARPMACCollector:
    """Collect ARP table from a device and update MAC fields on Nautobot IP address records."""

    ARP_COMMANDS = {
        "cisco_ios":  "show ip arp",
        "cisco_xe":   "show ip arp",
        "cisco_xr":   "show arp",
        "cisco_nxos": "show ip arp",
        "arista_eos": "show ip arp",
    }
    ARP_TEMPLATES = {
        "cisco_ios":  "cisco_ios_show_ip_arp.textfsm",
        "cisco_xe":   "cisco_ios_show_ip_arp.textfsm",
        "cisco_nxos": "cisco_nxos_show_ip_arp.textfsm",
        "arista_eos": "arista_eos_show_ip_arp.textfsm",
    }
    TEMPLATE_FIELD_MAP = {
        "arista_eos_show_ip_arp.textfsm": ("IPV4_ADDRESS", "HARDWARE_ADDR", "INTERFACE"),
        "cisco_ios_show_ip_arp.textfsm":  ("ADDRESS",      "MAC",           "INTERFACE"),
        "cisco_nxos_show_ip_arp.textfsm": ("IP_ADDRESS",   "MAC",           "INTERFACE"),
    }

    def __init__(self, job, device, dry_run=False):
        self.job = job
        self.device = device
        self.dry_run = dry_run

    def run(self, session):
        platform = self.device.platform.network_driver
        command = self.ARP_COMMANDS.get(platform)
        template = self.ARP_TEMPLATES.get(platform)
        if not command:
            self.job.logger.warning(f"{self.device} No ARP command defined for {platform}.")
            return
        if template and template not in self.TEMPLATE_FIELD_MAP:
            self.job.logger.warning(
                f"{self.device} No field map defined for template '{template}'. "
                "Add an entry to TEMPLATE_FIELD_MAP."
            )
            return
        arp_output = session.send_command(command)
        if not template:
            self.job.logger.info(f"{self.device} Raw ARP output:\n{arp_output}")
            return
        try:
            entries = parse_command_output(arp_output, template)
        except FileNotFoundError:
            self.job.logger.warning(f"{self.device} TextFSM template '{template}' not found.")
            return
        ip_key, mac_key, intf_key = self.TEMPLATE_FIELD_MAP[template]
        self.job.logger.info(f"{self.device} Parsed {len(entries)} ARP entries.")
        for entry in entries:
            self._process_entry(entry, ip_key, mac_key, intf_key)

    @staticmethod
    def _normalise_mac(raw_mac):
        """Normalise any common MAC notation to colon-separated lowercase."""
        digits = raw_mac.replace(":", "").replace("-", "").replace(".", "").lower()
        if len(digits) != 12:
            return raw_mac
        return ":".join(digits[i:i + 2] for i in range(0, 12, 2))

    def _process_entry(self, entry, ip_key, mac_key, intf_key):
        ip_str  = entry.get(ip_key, "").strip()
        mac_raw = entry.get(mac_key, "").strip()
        intf    = entry.get(intf_key, "").strip()
        if not ip_str or not mac_raw:
            return
        mac_str = self._normalise_mac(mac_raw)
        self.job.logger.info(f"{self.device} ARP: IP={ip_str} MAC={mac_str} Interface={intf}")
        if self.dry_run:
            return
        try:
            ip_obj = IPAddress.objects.filter(host=ip_str).first()
            if ip_obj:
                ip_obj.cf["mac_address"] = mac_str
                ip_obj.cf["arp_source_device"] = self.device.name
                ip_obj.validated_save()
                self.job.logger.info(f"{self.device} Updated IP {ip_str} with MAC {mac_str}.")
            else:
                self.job.logger.warning(f"{self.device} IP {ip_str} not found in Nautobot, skipping.")
        except Exception as exc:
            self.job.logger.error(f"{self.device} Failed to update IP {ip_str}: {exc}")


register_jobs(CustomCaptureDeviceData)
