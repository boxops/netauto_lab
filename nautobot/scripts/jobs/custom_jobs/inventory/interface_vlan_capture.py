"""Purpose: Capture interface and VLAN state from network devices and sync to Nautobot."""

import ipaddress

from netmiko import ConnectHandler
from netaddr import EUI
from netutils.ip import is_ip
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError

from nautobot.dcim.models import Interface
from nautobot.dcim.choices import InterfaceTypeChoices
from nautobot.ipam.models import IPAddress, Prefix, Namespace, VLAN
from nautobot.extras.models import Status
from nautobot.apps.jobs import register_jobs, Job, BooleanVar, IntegerVar

from custom_jobs.modules.tools import (
    apply_device_filters,
    get_device_connection_info,
    parse_command_output,
    parallel_execution,
    JobLogBuffer,
    JobProxy,
    DeviceFormEntry,
)

name = "Inventory"

SUPPORTED_PLATFORMS = ["cisco_xr", "arista_eos", "fiberstore_fsos", "keymile_nos", "mikrotik_routeros"]

# Per-platform CLI command, TextFSM template, and normalised field-name mapping.
# An empty string value means the field is unavailable from that platform's output.
PLATFORM_CONFIG = {
    "fiberstore_fsos": {
        "command": "show interface",
        "template": "fiberstore_fsos_show_interface.textfsm",
        "field_map": {
            "name":        "INTERFACE",
            "link_status": "LINK_STATUS",
            "description": "DESCRIPTION",
            "mac_address": "MAC_ADDRESS",
            "mtu":         "",
            "ip_address":  "IP_ADDRESS",
            "speed":       "SPEED",
            "duplex":      "DUPLEX",
        },
        "vlan": {
            "command":  "show vlan brief",
            "template": "fiberstore_fsos_show_vlan_brief.textfsm",
        },
    },
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
    "keymile_nos": {
        "command": "show port",
        "template": "keymile_nos_show_port.textfsm",
        # Port numbers (1-48) are prefixed to produce names like 'port1', 'port23'.
        "name_field": "PORT_NUMBER",
        "name_prefix": "port",
        "type_field": "TYPE",
        "type_map": {
            "GPON":     InterfaceTypeChoices.TYPE_OTHER,
            "Ethernet": InterfaceTypeChoices.TYPE_OTHER,
        },
        "field_map": {
            "name":        "PORT_NUMBER",
            "link_status": "OPER_STATUS",
            "description": "",
            "mac_address": "",
            "mtu":         "",
            "ip_address":  "",
            "speed":       "SPEED",
            "duplex":      "",
        },
        "vlan": {
            "command":  "show vlan",
            "template": "keymile_nos_show_vlan.textfsm",
        },
    },
    "mikrotik_routeros": {
        "command":    "interface print detail",
        "template":   "mikrotik_routeros_interface_print_detail.textfsm",
        "use_timing": True,
        # TYPE field from the template drives interface type selection.
        "type_field": "TYPE",
        "type_map": {
            "ether":  InterfaceTypeChoices.TYPE_1GE_FIXED,
            "bridge": InterfaceTypeChoices.TYPE_VIRTUAL,
            "vlan":   InterfaceTypeChoices.TYPE_VIRTUAL,
        },
        # pppoe-in entries are transient customer sessions — skip them.
        "skip_types": ["pppoe-in"],
        # FLAGS field: 'R' means running (up). No 'R' or flag 'X' means down.
        "running_flag": "R",
        "field_map": {
            "name":        "NAME",
            "link_status": "FLAGS",
            "description": "DESCRIPTION",
            "mac_address": "MAC_ADDRESS",
            "mtu":         "MTU",
            "ip_address":  "IP_ADDRESS",
            "speed":       "",
            "duplex":      "",
        },
        # A second command fetches IP addresses and merges them by interface name.
        "ip_command": {
            "command":         "ip address print",
            "template":        "mikrotik_routeros_ip_address_print.textfsm",
            "interface_field": "INTERFACE",
            "ip_field":        "IP",
            "subnet_field":    "SUBNET",
            "target_field":    "IP_ADDRESS",
        },
    },
}


class InterfaceVLANCapture(Job, DeviceFormEntry):
    """Capture interface and VLAN state from devices and sync to Nautobot."""

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
        name = "Interface and VLAN Capture"
        description = (
            "Capture interface state and VLANs from live devices and sync to Nautobot. "
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
                buf.warning(f"{dev} Platform {driver} not supported for interface/VLAN capture, skipping.")
                return buf
            buf.info(f"{dev} Starting interface and VLAN capture.")
            CaptureDeviceData(proxy, dev).execute()
            return buf

        if parallel_task:
            parallel_execution(process_device, all_devices, max_workers, job_logger=self.logger)
        else:
            for dev in all_devices:
                process_device(dev).drain_to(self.logger)


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
            self.captured_data.pop("interfaces", None)
            self._capture_vlans(session)
            self._import_vlans()
            self.captured_data.pop("vlans", None)
        except Exception as exc:
            self.job.logger.error(f"{self.device} Error: {exc}")
        finally:
            if own_session and session:
                session.disconnect()

    def _capture_interfaces(self, session):
        platform = self.device.platform.network_driver
        config = PLATFORM_CONFIG[platform]
        use_timing = config.get("use_timing", False)
        raw = (session.send_command_timing(config["command"]) if use_timing
               else session.send_command(config["command"]))
        parsed = parse_command_output(raw, config["template"])

        # For platforms that need a separate command to fetch IP addresses.
        if "ip_command" in config:
            ip_cfg = config["ip_command"]
            ip_raw = (session.send_command_timing(ip_cfg["command"]) if use_timing
                      else session.send_command(ip_cfg["command"]))
            ip_rows = parse_command_output(ip_raw, ip_cfg["template"])
            ip_by_intf = {}
            for row in ip_rows:
                # Skip dynamic addresses (PPPoE leases, etc.)
                if "D" in row.get("FLAGS", ""):
                    continue
                raw_intf = row.get(ip_cfg["interface_field"], "").strip("<>")
                ip = row.get(ip_cfg["ip_field"], "")
                subnet = row.get(ip_cfg.get("subnet_field", ""), "")
                if raw_intf and ip:
                    cidr = f"{ip}/{subnet}" if subnet else ip
                    ip_by_intf.setdefault(raw_intf, cidr)  # first static IP wins
            target = ip_cfg.get("target_field", ip_cfg["ip_field"])
            for item in parsed:
                name = item.get("NAME", "")
                if name in ip_by_intf:
                    item[target] = ip_by_intf[name]

        self.captured_data["interfaces"] = parsed
        self.job.logger.info(f"{self.device} Captured {len(parsed)} interface entries.")

    def _import_interfaces(self):
        platform = self.device.platform.network_driver
        config = PLATFORM_CONFIG[platform]
        field_map = config["field_map"]
        active_status = Status.objects.get(name="Active")

        for item in self.captured_data.get("interfaces", []):
            # Support platforms (e.g. keymile_nos) where the name field must be prefixed.
            name_field = config.get("name_field") or field_map.get("name", "")
            name_prefix = config.get("name_prefix", "")
            intf_name = name_prefix + item.get(name_field, "") if name_field else ""
            if not intf_name:
                continue

            def _get(key):
                k = field_map.get(key, "")
                return item.get(k, "") if k else ""

            # Determine interface type from config type_map, or fall back to 1GE fixed.
            type_field = config.get("type_field", "")
            raw_type = item.get(type_field, "") if type_field else ""

            # Skip interface types excluded by this platform's config (e.g. pppoe-in).
            skip_types = config.get("skip_types", [])
            if raw_type in skip_types:
                continue

            if type_field:
                type_map = config.get("type_map", {})
                if raw_type.startswith("TrkGrp"):
                    iface_type = InterfaceTypeChoices.TYPE_LAG
                else:
                    iface_type = type_map.get(raw_type, InterfaceTypeChoices.TYPE_OTHER)
            else:
                iface_type = InterfaceTypeChoices.TYPE_1GE_FIXED

            interface = self._get_or_create_interface(
                intf_name, self.device, iface_type, active_status
            )

            link_status = _get("link_status")
            description = _get("description")
            mac_address = _get("mac_address")
            mtu = _get("mtu")
            ip_address = _get("ip_address")

            running_flag = config.get("running_flag")
            if running_flag:
                interface.enabled = running_flag in (link_status or "")
            elif link_status:
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

            if ip_address and ip_address not in ("", "unassigned", "Unknown"):
                try:
                    self._assign_ip(interface, ip_address)
                except Exception as exc:
                    self.job.logger.error(f"{self.device} Failed to set IP on {intf_name}: {exc}")

            self.job.logger.debug(f"{self.device} Updated interface {intf_name}.")

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
        # Normalise to CIDR notation — some platforms (e.g. FSOS) return bare IPs without prefix.
        if "/" not in ip_address:
            ip_address = f"{ip_address}/32"
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


register_jobs(InterfaceVLANCapture)
